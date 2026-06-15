"""Pipeline d'indexation monitoré (jalon 1), exécuté dans un thread de fond.

Étapes : parse .eml -> classement affaire -> extraction PJ faciles -> chunks ->
embeddings -> upsert ChromaDB. Progression et journal écrits dans data/app.sqlite
(lus par /admin via polling). Idempotence par content_hash : un document inchangé
et déjà indexé est skippé.

Lancement : via l'app (POST /api/index/start) ou en CLI (`python indexer.py`).
"""
from __future__ import annotations

import hashlib
import threading
import time

import graph_state
import state
from config import cfg
from parsing import attachments, classifier, enrich
from parsing.cleaner import clean_body
from parsing.eml_parser import Email, parse_all
from parsing.threading_ import build_threads
from rag.chunker import chunk_document
from rag.index import (
    existing_ids,
    get_collections,
    upsert_chunks,
    upsert_documents,
)
from rag.providers import get_embedder

_lock = threading.Lock()
_thread: threading.Thread | None = None


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def _doc_summary(kind: str, header: str, body: str) -> str:
    """Texte du niveau 1 (document) : en-tête + début du contenu."""
    snippet = body.strip().replace("\n", " ")[:600]
    return f"{header}\n{snippet}"


def _enrichment_chunk_text(header: str, enr: dict) -> str:
    """Texte du chunk d'enrichissement, cherchable sémantiquement."""
    parts = [header]
    for key, label in (("intention", "Intention"), ("objectif", "Objectif"),
                       ("resume", "Résumé"), ("description", "Description")):
        val = enr.get(key)
        if val:
            parts.append(f"{label} : {val}")
    faits = enr.get("faits") or enr.get("facts") or []
    if isinstance(faits, list) and faits:
        parts.append("Faits marquants : " + " ; ".join(str(f) for f in faits))
    # Dates d'événements (ex : "signature acte : 2014-09-10")
    dates = enr.get("dates_evenements") or []
    if isinstance(dates, list) and dates:
        d_parts = [f"{d.get('label', '')} : {d.get('date', '')}" for d in dates if d.get("date")]
        if d_parts:
            parts.append("Dates : " + " ; ".join(d_parts))
    # Parties (ex : "émetteur Maître Dubois (avocat) ; destinataire Maître Leroy (avocat)")
    parties = enr.get("parties") or []
    if isinstance(parties, list) and parties:
        p_parts = [
            f"{p.get('role', '')} {p.get('nom', '')} ({p.get('qualite', '')})"
            for p in parties if p.get("nom")
        ]
        if p_parts:
            parts.append("Parties : " + " ; ".join(p_parts))
    # Déclarations / dires
    decls = enr.get("declarations") or []
    if isinstance(decls, list) and decls:
        d_parts = [
            f"{d.get('auteur', '')} : {d.get('contenu', '')}"
            for d in decls if d.get("contenu")
        ]
        if d_parts:
            parts.append("Déclarations : " + " ; ".join(d_parts))
    # Codes structurés (tous types)
    codes = enr.get("codes") or {}
    if isinstance(codes, dict):
        all_codes = []
        for sub in codes.values():
            if isinstance(sub, list):
                all_codes.extend(v for v in sub if v)
        if all_codes:
            parts.append("Références : " + " ; ".join(all_codes))
    return "\n".join(parts)


def _persist_enrichment(doc_id: str, enr: dict | None) -> None:
    """Stocke l'enrichissement et les entités/codes dans le graphe."""
    if not enr:
        return
    graph_state.upsert_enrichment(doc_id, enr)
    graph_state.add_entities(
        doc_id,
        entities=enr.get("entites") or enr.get("entities") or [],
        codes=enr.get("codes") or [],
    )


_ALL_AFFAIRES = ("succession", "dette", "pro", "autre")


def _affaire_meta(affaires: list[str]) -> dict:
    """Métadonnées Chroma pour le filtrage multi-affaires.

    Chroma ne stocke pas de liste -> on pose un booléen par affaire
    (`aff_dette=True`...) qui permet de filtrer « contient cette affaire »,
    plus une chaîne CSV lisible `affaires`.
    """
    meta = {f"aff_{a}": (a in affaires) for a in _ALL_AFFAIRES}
    meta["affaires"] = ",".join(affaires)
    return meta


def _build_thread_edges(emails: list[Email], threads: dict[str, str]) -> None:
    """Crée les arêtes reply_to (direct) et same_thread (même fil).

    reply_to : relie un mail à celui qu'il cite (In-Reply-To), si présent dans le corpus.
    same_thread : relie chaque mail au mail précédent du même fil (chaîne chronologique).
    """
    by_mid: dict[str, str] = {e.message_id: e.doc_id for e in emails if e.message_id}

    for em in emails:
        # reply_to : lien direct vers le mail cité
        if em.in_reply_to and em.in_reply_to in by_mid:
            graph_state.add_edge(em.doc_id, by_mid[em.in_reply_to], "reply_to", source="header")

    # same_thread : chaîne chronologique au sein de chaque fil
    by_thread: dict[str, list[Email]] = {}
    for em in emails:
        by_thread.setdefault(threads.get(em.doc_id, em.doc_id), []).append(em)
    for fil in by_thread.values():
        if len(fil) < 2:
            continue
        fil.sort(key=lambda e: e.date)
        for prev, cur in zip(fil, fil[1:]):
            graph_state.add_edge(cur.doc_id, prev.doc_id, "same_thread", source="header")


def is_running() -> bool:
    return state.is_running()


def start_async(enrich_provider: str | None = None, enrich_model: str | None = None) -> bool:
    """Démarre l'indexation en arrière-plan. False si déjà en cours."""
    global _thread
    with _lock:
        if state.is_running():
            return False
        state.init_db()
        _thread = threading.Thread(
            target=_run_safe,
            kwargs={"enrich_provider": enrich_provider, "enrich_model": enrich_model},
            name="indexer",
            daemon=True,
        )
        _thread.start()
        return True


def _run_safe(enrich_provider: str | None = None, enrich_model: str | None = None) -> None:
    try:
        run(enrich_provider=enrich_provider, enrich_model=enrich_model)
    except Exception as exc:  # pragma: no cover
        import traceback
        tb = traceback.format_exc()
        print(f"[indexer] ERREUR fatale : {exc}\n{tb}", flush=True)
        state.log(f"ERREUR fatale : {type(exc).__name__}: {exc}", level="error")
        state.log(tb[:800], level="error")
        state.finish_progress(phase="échec")


def run(force: bool = False, enrich_provider: str | None = None, enrich_model: str | None = None) -> dict:
    """Exécute le pipeline. `force=True` réindexe même si le hash est inchangé."""
    state.init_db()
    state.log("Parsing des emails…")
    state.update_progress(phase="parsing")

    emails: list[Email] = parse_all()
    total = len(emails)
    state.start_progress(total=total, enrich_provider=enrich_provider, enrich_model=enrich_model)
    state.log(f"{total} emails à indexer (après déduplication).")

    # Fils de discussion (déterministe, via In-Reply-To / References).
    threads = build_threads(emails)
    state.log(f"{len(set(threads.values()))} fils de discussion reconstruits.")

    # Client LLM pour l'enrichissement. Instancié une fois.
    enrich_chat = enrich.get_enrich_chat(provider=enrich_provider, model=enrich_model)
    _ep = enrich_provider or "mistral"
    _em = enrich_model or (cfg.mistral_chat_model if _ep == "mistral" else "auto")
    state.log(f"Enrichissement via {_ep} ({_em}).")

    embedder = get_embedder()
    docs_col, chunks_col = get_collections()

    n_docs = n_chunks = n_skipped = n_ocr = 0

    for i, em in enumerate(emails, 1):
        state.update_progress(
            phase="indexation",
            current=i,
            current_doc=f"[{em.correspondent}] {em.subject[:50]}",
        )

        # --- Classement (multi-affaires) + nettoyage ----------------------
        em.affaires = classifier.classify_all(em.subject, em.body_text)
        em.affaire = classifier.classify(em.subject, em.body_text)  # principale
        cleaned = clean_body(em.body_text)
        mail_text = cleaned["new"] or em.body_text

        # --- Idempotence (mail) -------------------------------------------
        content_hash = _hash(em.subject + "\n" + mail_text)
        if not force and state.get_document_hash(em.doc_id) == content_hash:
            n_skipped += 1
            state.log(f"[{i}/{total}] ↷ skippé (inchangé) : {em.subject[:60]}")
            # Mail inchangé : Chroma et l'enrichissement sont déjà à jour -> skip.
            # On traite quand même les PJ (idempotence gérée par PJ).
            _index_attachments(
                em, embedder, docs_col, chunks_col, force, enrich_chat, counters=None
            )
            continue

        header = (
            f"[MAIL] {em.date_iso[:10]} | {em.correspondent} | {em.affaire} | "
            f"De: {em.from_} | Objet: {em.subject}"
        )
        base_meta = {
            "type": "mail",
            "doc_id": em.doc_id,
            "email_id": em.doc_id,
            "correspondent": em.correspondent,
            "date": em.date_iso,
            "subject": em.subject,
            "title": em.subject,
            "affaire": em.affaire,
            "source_path": em.source_path,
            **_affaire_meta(em.affaires),
        }

        # Niveau 1 : document
        upsert_documents(
            docs_col,
            embedder,
            [{"id": em.doc_id, "text": _doc_summary("mail", header, mail_text), "metadata": base_meta}],
        )
        # Niveau 2 : chunks
        chunks = chunk_document(em.doc_id, f"{header}\n\n{mail_text}", base_meta)
        if chunks:
            upsert_chunks(
                chunks_col,
                embedder,
                [{"id": c.chunk_id, "text": c.text, "metadata": c.metadata} for c in chunks],
            )
        n_docs += 1
        n_chunks += len(chunks)

        # --- Enrichissement LLM (intention, objectif, faits, entités, codes) ---
        state.update_progress(phase="enrichissement")
        state.log(f"[{i}/{total}] → enrichissement LLM : {em.subject[:60]}")
        _t0 = time.time()
        enr = enrich.enrich_email(em.subject, mail_text, enrich_chat, log=state.log)
        _dt = time.time() - _t0
        _persist_enrichment(em.doc_id, enr)
        if enr:
            state.log(
                f"[{i}/{total}] ✓ enrichi en {_dt:.1f}s — "
                f"{len(enr.get('faits') or [])} faits, "
                f"{len(enr.get('parties') or [])} parties, "
                f"{len(enr.get('dates_evenements') or [])} dates"
            )
        else:
            state.log(f"[{i}/{total}] ✗ enrichissement échoué ({_dt:.1f}s)", level="error")
            # Chunk d'enrichissement cherchable (résumé/intention/faits).
            enr_meta = {**base_meta, "kind": "enrichment", "chunk_index": -1}
            upsert_chunks(
                chunks_col,
                embedder,
                [{
                    "id": f"{em.doc_id}#enrich",
                    "text": _enrichment_chunk_text(header, enr),
                    "metadata": enr_meta,
                }],
            )
            n_chunks += 1

        # Registre SQLite (pour la timeline / navigation)
        thread_id = threads.get(em.doc_id)
        state.upsert_document(
            {
                "doc_id": em.doc_id,
                "type": "mail",
                "correspondent": em.correspondent,
                "date": em.date_iso,
                "subject": em.subject,
                "affaire": em.affaire,
                "affaires": ",".join(em.affaires),
                "email_id": em.doc_id,
                "source_path": em.source_path,
                "n_chunks": len(chunks),
                "needs_ocr": 0,
                "content_hash": content_hash,
                "thread_id": thread_id,
                "in_reply_to": em.in_reply_to,
                "references": ",".join(em.references or []),
            }
        )

        # --- Pièces jointes -----------------------------------------------
        counters = {"docs": 0, "chunks": 0, "ocr": 0}
        _index_attachments(em, embedder, docs_col, chunks_col, force, enrich_chat, counters)
        n_docs += counters["docs"]
        n_chunks += counters["chunks"]
        n_ocr += counters["ocr"]

        # Pause légère pour ménager ChromaDB (200 ms par document)
        time.sleep(0.2)

    # --- Construction des arêtes du graphe (après indexation de tous les docs) ---
    state.update_progress(phase="graphe")
    _build_thread_edges(emails, threads)
    n_code_edges = graph_state.build_shared_code_edges()
    state.log(f"Graphe : arêtes de fil créées + {n_code_edges} liens par code partagé.")

    state.log(
        f"Indexation terminée : {n_docs} documents, {n_chunks} chunks, "
        f"{n_skipped} inchangés (skippés), {n_ocr} PJ en attente d'OCR (jalon 2)."
    )
    state.finish_progress(phase="terminé")
    return {
        "documents": n_docs,
        "chunks": n_chunks,
        "skipped": n_skipped,
        "needs_ocr": n_ocr,
    }


def _index_attachments(
    em: Email,
    embedder,
    docs_col,
    chunks_col,
    force: bool,
    enrich_chat,
    counters: dict | None,
) -> None:
    # Brèves des PJ enrichies, pour l'inférence des liens en fin de fonction.
    att_briefs: list[dict] = []

    for att in em.attachments:
        # Toute PJ est rattachée à son mail porteur dans le graphe.
        graph_state.add_edge(att.doc_id, em.doc_id, "attached_to", source="header")

        _ocr_key = cfg.anthropic_api_key or None
        text_parts = attachments.extract_text_parts(
            att,
            api_key=_ocr_key,
            log=lambda msg, lvl="info": state.log(msg, level=lvl),
        )

        # text_parts is a list of (text, needs_ocr), one entry per 30-page chunk.
        # Single-entry list = normal case; multiple = long scanned PDF was split.
        first_text, first_needs_ocr = text_parts[0] if text_parts else ("", False)

        if _ocr_key and not first_needs_ocr and first_text and len(first_text) >= 40:
            n_parts = len(text_parts)
            label = f" ({n_parts} parties)" if n_parts > 1 else ""
            state.log(f"  ✓ OCR {att.filename[:40]}{label} : {sum(len(t) for t, _ in text_parts)} chars")

        # Affaires calculées sur le texte complet (toutes parties concaténées).
        full_text = "\n\n".join(t for t, _ in text_parts if t)
        att_affaires = sorted(
            set(em.affaires)
            | set(classifier.classify_all(att.filename, full_text))
            - ({"autre"} if len(em.affaires) > 0 and em.affaires != ["autre"] else set())
        ) or ["autre"]
        att_affaire = classifier.classify(att.filename, "") if not em.affaires else em.affaire

        if first_needs_ocr or not full_text.strip():
            # Enregistre la PJ au registre sans l'indexer (texte indisponible).
            state.upsert_document(
                {
                    "doc_id": att.doc_id,
                    "type": "attachment",
                    "correspondent": em.correspondent,
                    "date": em.date_iso,
                    "subject": att.filename,
                    "affaire": att_affaire,
                    "affaires": ",".join(att_affaires),
                    "email_id": em.doc_id,
                    "source_path": att.stored_path,
                    "n_chunks": 0,
                    "needs_ocr": 1 if first_needs_ocr else 0,
                    "content_hash": None,
                }
            )
            if first_needs_ocr and counters is not None:
                counters["ocr"] += 1
            continue

        # Index each part under its own doc_id (part 0 keeps the canonical id).
        part_doc_ids: list[str] = []
        for part_idx, (text, _) in enumerate(text_parts):
            if not text.strip():
                continue
            part_doc_id = att.doc_id if part_idx == 0 else f"{att.doc_id}_p{part_idx + 1}"
            part_label = f" (partie {part_idx + 1}/{len(text_parts)})" if len(text_parts) > 1 else ""

            content_hash = _hash(text)
            if not force and state.get_document_hash(part_doc_id) == content_hash:
                part_doc_ids.append(part_doc_id)
                continue

            header = (
                f"[PIECE JOINTE{part_label}] {em.date_iso[:10]} | {em.correspondent} | "
                f"{','.join(att_affaires)} | Fichier: {att.filename} | "
                f"(transmis par le mail: {em.subject})"
            )
            base_meta = {
                "type": "attachment",
                "doc_id": part_doc_id,
                "email_id": em.doc_id,
                "correspondent": em.correspondent,
                "date": em.date_iso,
                "subject": att.filename,
                "title": att.filename + (f" partie {part_idx + 1}" if len(text_parts) > 1 else ""),
                "affaire": att_affaire,
                "source_path": att.stored_path,
                **_affaire_meta(att_affaires),
            }
            upsert_documents(
                docs_col,
                embedder,
                [{"id": part_doc_id, "text": _doc_summary("att", header, text), "metadata": base_meta}],
            )
            chunks = chunk_document(part_doc_id, f"{header}\n\n{text}", base_meta)
            if chunks:
                upsert_chunks(
                    chunks_col,
                    embedder,
                    [{"id": c.chunk_id, "text": c.text, "metadata": c.metadata} for c in chunks],
                )
            state.upsert_document(
                {
                    "doc_id": part_doc_id,
                    "type": "attachment",
                    "correspondent": em.correspondent,
                    "date": em.date_iso,
                    "subject": att.filename,
                    "affaire": att_affaire,
                    "affaires": ",".join(att_affaires),
                    "email_id": em.doc_id,
                    "source_path": att.stored_path,
                    "n_chunks": len(chunks),
                    "needs_ocr": 0,
                    "content_hash": content_hash,
                }
            )

            # Link part to parent email and to previous part.
            graph_state.add_edge(part_doc_id, em.doc_id, "attached_to", source="header")
            if part_idx > 0:
                graph_state.add_edge(part_doc_id, att.doc_id, "annexe_of", source="header")

            if counters is not None:
                counters["docs"] += 1
                counters["chunks"] += len(chunks)

            part_doc_ids.append(part_doc_id)

        # Enrichissement LLM sur le texte complet (toutes parties) sous le doc_id canonique.
        text_for_enrich = full_text[:8000]  # garde-fou taille prompt
        state.log(f"  PJ → enrichissement LLM : {att.filename[:60]}")
        _t0_att = time.time()
        enr = enrich.enrich_attachment(att.filename, text_for_enrich, em.subject, enrich_chat, log=state.log)
        _dt_att = time.time() - _t0_att
        _persist_enrichment(att.doc_id, enr)
        if enr:
            state.log(f"  PJ ✓ enrichie en {_dt_att:.1f}s : {att.filename[:50]}")
            enr_meta = {
                **{
                    "type": "attachment",
                    "doc_id": att.doc_id,
                    "email_id": em.doc_id,
                    "correspondent": em.correspondent,
                    "date": em.date_iso,
                    "subject": att.filename,
                    "title": att.filename,
                    "affaire": att_affaire,
                    "source_path": att.stored_path,
                    **_affaire_meta(att_affaires),
                },
                "kind": "enrichment",
                "chunk_index": -1,
            }
            header_enrich = (
                f"[PIECE JOINTE] {em.date_iso[:10]} | {em.correspondent} | "
                f"{','.join(att_affaires)} | Fichier: {att.filename} | "
                f"(transmis par le mail: {em.subject})"
            )
            upsert_chunks(
                chunks_col,
                embedder,
                [{
                    "id": f"{att.doc_id}#enrich",
                    "text": _enrichment_chunk_text(header_enrich, enr),
                    "metadata": enr_meta,
                }],
            )
            if counters is not None:
                counters["chunks"] += 1
            att_briefs.append({
                "att_id": att.doc_id,
                "filename": att.filename,
                "description": enr.get("description") or enr.get("resume") or "",
            })

    # --- Inférence des liens entre PJ (document principal / annexes) ---------
    if att_briefs:
        state.log(f"  Inférence des liens PJ ({len(att_briefs)} pièce(s) jointe(s))…")
    links = enrich.infer_links(em.subject, att_briefs, enrich_chat, log=state.log)
    if links:
        principal = links.get("principal")
        valid_ids = {b["att_id"] for b in att_briefs}
        if principal in valid_ids:
            for annexe in links.get("annexes") or []:
                if annexe in valid_ids and annexe != principal:
                    graph_state.add_edge(annexe, principal, "annexe_of", source="llm")
            graph_state.add_edge(principal, em.doc_id, "principal", source="llm")
        for link in links.get("pj_links") or []:
            src, dst = link.get("src"), link.get("dst")
            if src in valid_ids and dst in valid_ids and src != dst:
                rel = link.get("relation") or "accompanies"
                graph_state.add_edge(src, dst, rel, source="llm")


if __name__ == "__main__":
    cfg.ensure_dirs()
    result = run()
    print("Résultat :", result)
