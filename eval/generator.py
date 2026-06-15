"""Génération du golden dataset via LLM.

Lance un thread de fond qui :
1. Échantillonne N chunks depuis ChromaDB
2. Pour chaque chunk, demande au LLM de produire une question + réponse de référence
3. Stocke les résultats dans eval_questions (SQLite)
"""
from __future__ import annotations

import dataclasses
import random
import threading
from typing import Any

import eval_state
from config import Config, cfg
from parsing.jsonparse import parse_json_response as _parse_json_response
from rag.index import get_client
from rag.providers import get_chat

_lock = threading.Lock()
_thread: threading.Thread | None = None
_cancel_flag = threading.Event()

CATEGORIES = ("factual", "procedural", "comparative", "temporal", "synthetic")
DIFFICULTIES = ("easy", "medium", "hard")

GENERATION_SYSTEM = """Tu es un expert en évaluation de systèmes RAG juridiques.
À partir du ou des extraits de documents fournis, génère UNE question précise et sa réponse attendue.
Retourne UNIQUEMENT un objet JSON valide, sans texte avant ni après, sans balise markdown :
{
  "question": "...",
  "expected_answer": "...",
  "category": "factual|procedural|comparative|temporal|synthetic",
  "difficulty": "easy|medium|hard",
  "notes": "..."
}
Règles :
- La question doit être répondable à partir des extraits fournis.
- La réponse attendue doit être précise, complète et factuelle.
- category : factual=faits/dates/montants, procedural=étapes légales, comparative=comparaison entre docs,
  temporal=chronologie, synthetic=synthèse multi-sources.
- difficulty : easy=information directe, medium=déduction simple, hard=synthèse ou inférence complexe.
- notes : brève justification du choix de catégorie/difficulté.

Contraintes impératives :
- Ne jamais écrire "le Document 1", "le Document 2" ni aucun label numéroté dans la question : ces repères sont internes et inconnus du système RAG. Référence les documents par leur date, leur auteur ou leur sujet (ex : "dans l'email du 14/03/2021 de Renard").
- Ne pas générer une question dont la réponse attendue est uniquement un code alphanumérique brut (code fiscal, numéro de pratique, timestamp ISO) sans contexte explicatif.
- La question doit être formulée en langage naturel, compréhensible par quelqu'un qui effectue une recherche dans les emails et documents du dossier.
- La réponse attendue doit être rédigée en prose française, pas uniquement une valeur brute."""


def start_async(
    provider: str,
    model: str,
    n_questions: int,
    categories: list[str],
    difficulties: list[str],
) -> bool:
    global _thread
    with _lock:
        if eval_state.is_gen_running():
            return False
        _cancel_flag.clear()
        _thread = threading.Thread(
            target=_run_safe,
            args=(provider, model, n_questions, categories, difficulties),
            name="eval-generator",
            daemon=True,
        )
        _thread.start()
        return True


def cancel() -> None:
    _cancel_flag.set()


def _run_safe(provider, model, n_questions, categories, difficulties) -> None:
    try:
        _run(provider, model, n_questions, categories, difficulties)
    except Exception as exc:
        eval_state.gen_log(f"ERREUR fatale : {exc}", level="error")
        eval_state.finish_gen_progress(phase="échec")


def _run(
    provider: str,
    model: str,
    n_questions: int,
    categories: list[str],
    difficulties: list[str],
) -> None:
    eval_state.start_gen_progress(n_questions)
    eval_state.gen_log(f"Génération de {n_questions} questions via {provider}/{model}…")

    config_override = _build_config_override(provider, model)
    chat = get_chat(config_override)

    # Pour les catégories comparatives/synthétiques on groupe 2-3 chunks ensemble.
    group_size = {
        "comparative": 3,
        "synthetic": 2,
    }

    chunks = _sample_chunks(n_questions * 3)  # sur-échantillonner pour avoir de la variété
    if not chunks:
        eval_state.gen_log("Aucun chunk disponible dans ChromaDB.", level="error")
        eval_state.finish_gen_progress(phase="échec — aucun chunk")
        return

    generated = 0
    for i in range(n_questions):
        if _cancel_flag.is_set():
            eval_state.gen_log("Génération annulée.")
            eval_state.finish_gen_progress(phase="annulé")
            return

        eval_state.update_gen_progress(current=i, phase=f"question {i+1}/{n_questions}")

        # Choisir une catégorie et une difficulté
        cat = categories[i % len(categories)] if categories else random.choice(CATEGORIES)
        diff = difficulties[i % len(difficulties)] if difficulties else random.choice(DIFFICULTIES)

        # Sélectionner le(s) chunk(s) pour cette question
        n_chunks_for_q = group_size.get(cat, 1)
        if cat in ("comparative", "synthetic") and n_chunks_for_q > 1:
            # Chunks du même volet thématique pour éviter les mélanges incohérents
            selected = _sample_coherent_chunks(chunks, n_chunks_for_q)
        else:
            start_idx = (i * 3) % max(1, len(chunks) - n_chunks_for_q)
            selected = chunks[start_idx : start_idx + n_chunks_for_q]
        if not selected:
            selected = chunks[:1]

        context = _format_chunks(selected)
        user_msg = (
            f"Catégorie souhaitée : {cat}\n"
            f"Difficulté souhaitée : {diff}\n\n"
            f"Extraits de documents :\n{context}"
        )

        try:
            resp = chat.complete(
                system=GENERATION_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
                tools=None,
            )
            parsed = _parse_json_response(resp.get("text", ""))
            if parsed and parsed.get("question") and parsed.get("expected_answer"):
                eval_state.insert_question({
                    "question": parsed["question"],
                    "expected": parsed["expected_answer"],
                    "category": parsed.get("category", cat),
                    "difficulty": parsed.get("difficulty", diff),
                    "notes": parsed.get("notes", ""),
                    "source_chunk": selected[0].get("id", "") if selected else "",
                })
                generated += 1
                eval_state.gen_log(f"[{i+1}] ✓ {parsed['question'][:80]}")
            else:
                eval_state.gen_log(f"[{i+1}] Réponse LLM non parseable.", level="error")
        except Exception as exc:
            eval_state.gen_log(f"[{i+1}] Erreur : {exc}", level="error")

    eval_state.update_gen_progress(current=n_questions)
    eval_state.finish_gen_progress(phase=f"terminé — {generated}/{n_questions} questions générées")
    eval_state.gen_log(f"Génération terminée : {generated} questions ajoutées.")


def _sample_chunks(n: int) -> list[dict]:
    """Récupère jusqu'à n chunks depuis ChromaDB (tirage pseudo-aléatoire)."""
    try:
        client = get_client()
        col = client.get_collection("chunks")
        total = col.count()
        if total == 0:
            return []
        limit = min(n, total)
        result = col.get(limit=limit, include=["documents", "metadatas"])
        items = []
        ids = result.get("ids", [])
        docs = result.get("documents", [])
        metas = result.get("metadatas", [])
        for chunk_id, doc, meta in zip(ids, docs, metas):
            items.append({"id": chunk_id, "text": doc, "metadata": meta or {}})
        random.shuffle(items)
        return items
    except Exception as exc:
        eval_state.gen_log(f"Erreur échantillonnage ChromaDB : {exc}", level="error")
        return []


def _sample_coherent_chunks(pool: list[dict], n: int) -> list[dict]:
    """Sélectionne n chunks thématiquement cohérents (même affaire) depuis le pool.

    Pour les catégories comparative/synthetic, regrouper des chunks sans rapport
    thématique produit des questions incohérentes. On groupe par affaire et on
    tire n chunks dans le même groupe.
    Retombe sur un tirage aléatoire si le pool est trop petit ou non structuré.
    """
    if len(pool) < n:
        return pool[:n] if pool else []

    # Grouper par affaire
    by_affaire: dict[str, list[dict]] = {}
    for c in pool:
        key = c.get("metadata", {}).get("affaire", "") or "autre"
        by_affaire.setdefault(key, []).append(c)

    # Garder uniquement les groupes assez grands
    eligible = [chunks for chunks in by_affaire.values() if len(chunks) >= n]
    if eligible:
        group = random.choice(eligible)
        return random.sample(group, n)

    # Fallback : tirage aléatoire depuis le pool complet
    return random.sample(pool, n)


def _build_config_override(provider: str, model: str) -> Config:
    overrides: dict[str, Any] = {"chat_provider": provider}
    if model and model.lower() != "auto":
        overrides["chat_model"] = model
    return dataclasses.replace(cfg, **overrides)


def _format_chunks(chunks: list[dict]) -> str:
    parts = []
    for i, c in enumerate(chunks, 1):
        meta = c.get("metadata", {})
        header = " — ".join(filter(None, [
            meta.get("date", "")[:10] if meta.get("date") else "",
            meta.get("correspondent", ""),
            meta.get("subject", ""),
        ]))
        # Utiliser des identifiants descriptifs (date/auteur/sujet) plutôt que
        # [Extrait N] pour que le LLM génère des questions référençant ces métadonnées
        # plutôt que des numéros d'extraits inconnus du RAG.
        label = f"[Document {i}]" + (f" — {header}" if header else "")
        parts.append(f"{label}\n{c.get('text', '')}")
    return "\n\n---\n\n".join(parts)


