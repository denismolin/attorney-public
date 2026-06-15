"""Évaluation LLM-as-judge du golden dataset.

Pour chaque question du dataset :
1. Obtient la réponse RAG réelle via rag.chat.answer()
2. Soumet (question, réponse attendue, réponse obtenue) au LLM juge
3. Parse le score JSON et stocke dans eval_results
"""
from __future__ import annotations

import dataclasses
import threading
import uuid
from typing import Any

import eval_state
from config import Config, cfg
from parsing.jsonparse import parse_json_response as _parse_json_response
from rag.providers import get_chat

_lock = threading.Lock()
_active_threads: dict[str, threading.Thread] = {}
_cancel_flags: dict[str, threading.Event] = {}

JUDGE_SYSTEM = """Tu es un juge LLM évaluant la qualité d'une réponse d'un système RAG juridique.
Tu reçois : la question, la réponse de référence (ground truth), la réponse produite par le RAG, et les passages sources que le RAG a récupérés.

Retourne UNIQUEMENT un objet JSON valide :
{
  "score": <entier 0-10>,
  "reasoning": "explication concise du score",
  "verdict": "pass|fail",
  "faithfulness": <entier 0-10>,
  "relevance": <entier 0-10>,
  "completeness": <entier 0-10>
}
Critères :
- faithfulness (0-10) : la réponse RAG est-elle fidèle aux passages sources récupérés ? Pénalise les affirmations qui vont au-delà ou contredisent les sources.
- relevance (0-10) : la réponse répond-elle directement à la question posée ?
- completeness (0-10) : la réponse couvre-t-elle tous les éléments essentiels de la réponse de référence ?
- score : moyenne pondérée (faithfulness×0.4 + relevance×0.3 + completeness×0.3).
- verdict : "pass" si score >= THRESHOLD (indiqué dans le message), sinon "fail".
Retourne UNIQUEMENT le JSON, sans texte avant ni après."""


def start_run(
    provider: str,
    model: str,
    question_ids: list[int] | None,
    threshold: float = 6.0,
) -> str:
    """Démarre un run d'évaluation. Retourne le run_id."""
    run_id = uuid.uuid4().hex
    questions = eval_state.list_questions()
    if question_ids:
        questions = [q for q in questions if q["id"] in question_ids]
    if not questions:
        raise ValueError("Aucune question à évaluer.")

    eval_state.create_run(run_id, provider, model, len(questions))

    cancel_event = threading.Event()
    with _lock:
        _cancel_flags[run_id] = cancel_event

    t = threading.Thread(
        target=_run_safe,
        args=(run_id, provider, model, questions, threshold, cancel_event),
        name=f"eval-judge-{run_id[:8]}",
        daemon=True,
    )
    with _lock:
        _active_threads[run_id] = t
    t.start()
    return run_id


def cancel_run(run_id: str) -> bool:
    with _lock:
        flag = _cancel_flags.get(run_id)
    if flag:
        flag.set()
        eval_state.cancel_run_db(run_id)
        return True
    return False


def _run_safe(run_id, provider, model, questions, threshold, cancel_event) -> None:
    try:
        _run(run_id, provider, model, questions, threshold, cancel_event)
    except Exception as exc:
        eval_state.update_run(run_id, phase=f"échec : {exc}", running=0)
    finally:
        with _lock:
            _active_threads.pop(run_id, None)
            _cancel_flags.pop(run_id, None)


def _run(
    run_id: str,
    provider: str,
    model: str,
    questions: list[dict],
    threshold: float,
    cancel_event: threading.Event,
) -> None:
    from rag.chat import answer as rag_answer

    config_override = _build_config_override(provider, model)
    judge_chat = get_chat(config_override)

    total = len(questions)
    errors = 0

    for i, q in enumerate(questions):
        if cancel_event.is_set():
            eval_state.cancel_run_db(run_id)
            return

        eval_state.update_run(run_id, current=i, phase=f"question {i+1}/{total}")

        # 1. Réponse RAG + chunks récupérés (avec leur texte)
        sources_meta: list[dict] = []
        context_chunks: list[str] = []
        try:
            from rag.retrieve import format_context, search
            hits = search(q["question"], top_k=8)
            context_text, sources_meta = format_context(hits)
            context_chunks = [h.text for h in hits]
            rag_result = rag_answer(q["question"], history=[])
            actual = rag_result.get("answer", "")
        except Exception as exc:
            actual = f"[Erreur RAG : {exc}]"
            errors += 1

        # 2. Jugement avec le contexte récupéré
        result = _judge_one(judge_chat, q["question"], q["expected"], actual, context_chunks, threshold)
        if result.get("error"):
            errors += 1

        eval_state.insert_result(run_id, q["id"], {**result, "actual": actual, "sources": sources_meta})
        eval_state.update_run(run_id, current=i + 1, errors=errors)

    eval_state.finish_run(run_id)


def _judge_one(
    chat,
    question: str,
    expected: str,
    actual: str,
    sources: list[dict],
    threshold: float,
) -> dict:
    # Formate les sources pour le juge (extrait + métadonnée sujet/date)
    sources_text = ""
    if sources:
        parts = []
        for i, chunk_text in enumerate(sources[:5], 1):  # max 5 chunks
            snippet = chunk_text[:500].strip()
            parts.append(f"[Chunk {i}]\n{snippet}")
        sources_text = "\n\n".join(parts)
    else:
        sources_text = "(aucune source récupérée)"

    user_msg = (
        f"Seuil de passage : {threshold}/10\n\n"
        f"Question : {question}\n\n"
        f"Réponse de référence (ground truth) :\n{expected}\n\n"
        f"Passages sources récupérés par le RAG :\n{sources_text}\n\n"
        f"Réponse produite par le RAG :\n{actual}"
    )
    try:
        resp = chat.complete(
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
            tools=None,
        )
        parsed = _parse_json_response(resp.get("text", ""))
        if parsed and "score" in parsed:
            # S'assurer que le verdict est cohérent avec le threshold
            score = float(parsed.get("score", 0))
            parsed["verdict"] = "pass" if score >= threshold else "fail"
            return parsed
        return {"score": 0, "reasoning": "Réponse juge non parseable.", "verdict": "fail",
                "faithfulness": 0, "relevance": 0, "completeness": 0, "error": True}
    except Exception as exc:
        return {"score": 0, "reasoning": f"Erreur juge : {exc}", "verdict": "fail",
                "faithfulness": 0, "relevance": 0, "completeness": 0, "error": True}


def _build_config_override(provider: str, model: str) -> Config:
    overrides: dict[str, Any] = {"chat_provider": provider}
    if model and model.lower() != "auto":
        overrides["chat_model"] = model
    return dataclasses.replace(cfg, **overrides)
