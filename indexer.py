"""Pipeline d'indexation monitoré (jalon 1), exécuté dans un thread de fond.

Étapes : parse .eml -> classement affaire -> extraction PJ faciles -> chunks ->
embeddings -> upsert ChromaDB. Progression et journal écrits dans data/generated/app.sqlite
(lus par /admin via polling). Idempotence par content_hash : un document inchangé
et déjà indexé est skippé.

Lancement : via l'app (POST /api/index/start) ou en CLI (`python indexer.py`).
"""
from __future__ import annotations

import hashlib
import threading
import time

import case_state
import graph_state
import state
from config import cfg
from parsing import attachments, classifier, enrich
from parsing.cleaner import clean_body
from pathlib import Path

from parsing.eml_parser import Email, parse_all, parse_located, _stable_doc_id
from parsing.threading_ import assign_thread, build_threads
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
    # Parties (ex : "émetteur Maître X (avocat) ; destinataire Maître Y (avocat)")
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
            str_codes = [v if isinstance(v, str) else str(v) for v in all_codes]
            parts.append("Références : " + " ; ".join(str_codes))
    return "\n".join(parts)


def _persist_enrichment(doc_id: str, enr: dict | None, content_hash: str | None = None) -> None:
    """Stocke l'enrichissement et les entités/codes dans le graphe."""
    if not enr:
        return
    graph_state.upsert_enrichment(doc_id, enr, content_hash=content_hash)
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


def start_async(
    enrich_provider: str | None = None,
    enrich_model: str | None = None,
    force: bool = False,
) -> bool:
    """Démarre l'indexation en arrière-plan. False si déjà en cours.

    `force=True` ré-exécute l'OCR/enrichissement même pour les documents
    inchangés (ex. pour ré-enrichir tout le fonds avec un nouveau modèle).
    """
    global _thread
    with _lock:
        state.init_db()
        # Le bail est pris ICI, de façon synchrone : deux requêtes simultanées (ou
        # deux workers) ne peuvent pas démarrer deux indexations.
        owner = state.new_lease_owner()
        if not state.acquire_lease(state.INDEX_LEASE, owner, kind="index"):
            return False
        try:
            _thread = threading.Thread(
                target=_run_safe,
                kwargs={"enrich_provider": enrich_provider, "enrich_model": enrich_model,
                        "force": force, "lease_owner": owner},
                name="indexer",
                daemon=True,
            )
            _thread.start()
        except Exception:
            state.release_lease(state.INDEX_LEASE, owner)
            raise
        return True


class IndexAlreadyRunning(RuntimeError):
    """Une autre indexation (autre thread, worker ou processus) tient le bail."""


def _run_safe(
    enrich_provider: str | None = None,
    enrich_model: str | None = None,
    force: bool = False,
    lease_owner: str | None = None,
) -> None:
    try:
        run(force=force, enrich_provider=enrich_provider, enrich_model=enrich_model,
            lease_owner=lease_owner)
    except Exception as exc:  # pragma: no cover
        import traceback
        tb = traceback.format_exc()
        print(f"[indexer] ERREUR fatale : {exc}\n{tb}", flush=True)
        state.log(f"ERREUR fatale : {type(exc).__name__}: {exc}", level="error")
        state.log(tb[:800], level="error")
        state.finish_progress(phase="échec")


def run(
    force: bool = False,
    enrich_provider: str | None = None,
    enrich_model: str | None = None,
    *,
    lease_owner: str | None = None,
    paths: list | None = None,
) -> dict:
    """Exécute le pipeline. `force=True` réindexe même si le hash est inchangé.

    Sans `paths` : parcours complet des sources (comportement d'origine). Avec
    `paths` : indexation incrémentale de ces seuls fichiers .eml, sans relire le
    reste du fonds (fils et arêtes mis à jour à partir du registre).

    Exclusif entre processus : prend le bail d'indexation (sauf si l'appelant l'a
    déjà pris et passe `lease_owner`) et lève `IndexAlreadyRunning` s'il est tenu.
    """
    state.init_db()
    owner = lease_owner or state.new_lease_owner()
    if lease_owner is None and not state.acquire_lease(state.INDEX_LEASE, owner, kind="index"):
        raise IndexAlreadyRunning("Une indexation est déjà en cours.")
    keeper = state.LeaseKeeper(state.INDEX_LEASE, owner).start()
    try:
        return _run_locked(force, enrich_provider, enrich_model, paths)
    except BaseException:
        # Y compris Ctrl-C en CLI : l'état ne doit pas rester « en cours ».
        state.finish_progress(phase="échec")
        raise
    finally:
        keeper.stop()
        state.release_lease(state.INDEX_LEASE, owner)


class _RegistryThreads:
    """Fils calculés à la demande depuis le registre (indexation incrémentale)."""

    def __init__(self, emails: list[Email]):
        self._by_id = {e.doc_id: e for e in emails}

    def get(self, doc_id: str, default=None):
        em = self._by_id.get(doc_id)
        return assign_thread(em) if em else default


def _parse_paths(paths: list) -> list[Email]:
    emails, seen = [], set()
    for p in paths:
        try:
            em = parse_located(Path(p))
        except Exception as exc:
            state.log(f"Lecture impossible : {Path(p).name} — {exc}", level="error")
            state.update_progress(inc_errors=1)
            continue
        if em.doc_id not in seen:
            seen.add(em.doc_id)
            emails.append(em)
    emails.sort(key=lambda e: e.date)
    return emails


def _build_incremental_edges(emails: list[Email]) -> None:
    """Fils et arêtes pour des emails indexés isolément, à partir du registre."""
    for em in emails:
        thread_id = assign_thread(em)          # second passage : relie les emails d'un même lot
        state.set_thread(em.doc_id, thread_id)
        if em.in_reply_to:
            parent = _stable_doc_id("eml", em.in_reply_to.strip())
            if state.get_document(parent):
                graph_state.add_edge(em.doc_id, parent, "reply_to", source="header")
        # Réponses arrivées avant le message auquel elles répondent.
        for child in state.replies_to(em.message_id):
            if child != em.doc_id:
                graph_state.add_edge(child, em.doc_id, "reply_to", source="header")
        fil = [d for d in state.get_thread_docs(thread_id) if d.get("type") == "mail"]
        for prev, cur in zip(fil, fil[1:]):
            if em.doc_id in (prev["doc_id"], cur["doc_id"]):
                graph_state.add_edge(cur["doc_id"], prev["doc_id"], "same_thread", source="header")


def _run_locked(force: bool, enrich_provider: str | None, enrich_model: str | None,
                paths: list | None = None) -> dict:
    incremental = paths is not None
    # Progression ouverte AVANT le parsing : l'UI voit le job dès son démarrage.
    # Une indexation incrémentale n'efface pas le journal de la dernière indexation complète.
    state.start_progress(total=0, enrich_provider=enrich_provider, enrich_model=enrich_model,
                         clear_log=not incremental)
    state.log("Parsing des emails…")
    state.update_progress(phase="parsing")

    if incremental:
        emails: list[Email] = _parse_paths(paths)
        total = len(emails)
        state.update_progress(total=total)
        state.log(f"Indexation incrémentale : {total} email(s).")
        threads = _RegistryThreads(emails)
    else:
        emails = parse_all()
        total = len(emails)
        state.update_progress(total=total)
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
        # Les prompts d'enrichissement et le classement dépendent du dossier de l'email.
        case_state.set_current(em.case_id)
        if em.case_id == case_state.DEFAULT:
            em.affaires = classifier.classify_all(em.subject, em.body_text)
            em.affaire = classifier.classify(em.subject, em.body_text)  # principale
        else:
            # Les mots-clés d'affaire sont propres au dossier d'origine.
            em.affaires, em.affaire = ["autre"], "autre"
        cleaned = clean_body(em.body_text)
        mail_text = cleaned["new"] or em.body_text

        # --- Idempotence (mail) -------------------------------------------
        content_hash = _hash(em.subject + "\n" + mail_text)
        if not force and state.get_document_hash(em.doc_id) == content_hash:
            n_skipped += 1
            state.log(f"[{i}/{total}] ↷ skippé (inchangé) : {em.subject[:60]}")
            # En-têtes absents des index antérieurs (métadonnée seule, aucun embedding).
            state.fill_email_headers(
                em.doc_id, message_id=em.message_id or None, from_addr=em.from_addr or None,
                to_addrs=em.to_addrs or None, cc_addrs=em.cc_addrs or None, source=em.source,
            )
            # Un nouvel email a pu relier ce mail à un autre fil depuis sa dernière indexation.
            state.set_thread(em.doc_id, threads.get(em.doc_id))
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
            "case_id": em.case_id,
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
        # Idempotence : mail déjà enrichi et texte inchangé (même content_hash déjà
        # à jour au moment où on entre ici, cf. skip plus haut sinon) -> on réutilise
        # l'enrichissement existant plutôt que de rappeler le LLM.
        state.update_progress(phase="enrichissement")
        cached_enr = graph_state.get_enrichment(em.doc_id)
        enrichment_reused = not force and bool(cached_enr)
        if enrichment_reused:
            state.log(f"[{i}/{total}] ↷ enrichissement inchangé (skippé) : {em.subject[:60]}")
            enr = cached_enr
        else:
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
        if enr and not enrichment_reused:
            # Chunk d'enrichissement cherchable (résumé/intention/faits) — inutile de
            # ré-embeder si on vient de réutiliser un enrichissement déjà indexé.
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
                "case_id": em.case_id,
                "message_id": em.message_id or None,
                "from_addr": em.from_addr or None,
                "to_addrs": em.to_addrs or None,
                "cc_addrs": em.cc_addrs or None,
                "source": em.source,
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

    case_state.set_current(None)

    # --- Construction des arêtes du graphe (après indexation de tous les docs) ---
    state.update_progress(phase="graphe")
    if incremental:
        _build_incremental_edges(emails)
        touched = [e.doc_id for e in emails] + [a.doc_id for e in emails for a in e.attachments]
        n_code_edges = graph_state.build_shared_code_edges_for(touched)
    else:
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
            | set(classifier.classify_all(att.filename, full_text)
                  if em.case_id == case_state.DEFAULT else [])
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
                    "case_id": em.case_id,
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
        # Part 0's hash doubles as the "did this attachment's text change" signal
        # used below to skip re-enrichment when nothing changed.
        part_doc_ids: list[str] = []
        canonical_unchanged = False
        for part_idx, (text, _) in enumerate(text_parts):
            if not text.strip():
                continue
            part_doc_id = att.doc_id if part_idx == 0 else f"{att.doc_id}_p{part_idx + 1}"
            part_label = f" (partie {part_idx + 1}/{len(text_parts)})" if len(text_parts) > 1 else ""

            content_hash = _hash(text)
            if not force and state.get_document_hash(part_doc_id) == content_hash:
                part_doc_ids.append(part_doc_id)
                if part_idx == 0:
                    canonical_unchanged = True
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
                "case_id": em.case_id,
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
                    "case_id": em.case_id,
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
        # Idempotence à deux niveaux :
        #  1. même doc_id, texte inchangé (ex. réindexation) -> réutilise directement.
        #  2. doc_id différent mais texte strictement identique (ex. la même PJ jointe
        #     à un autre email) -> réutilise l'enrichissement d'un autre doc_id plutôt
        #     que de rappeler le LLM pour un contenu déjà analysé.
        full_text_hash = _hash(full_text) if not force else None
        cached_enr = graph_state.get_enrichment(att.doc_id) if canonical_unchanged else None
        enrichment_reused = bool(canonical_unchanged and cached_enr)
        cross_doc_cached_enr = None
        if not enrichment_reused and not force and full_text_hash:
            cross_doc_cached_enr = graph_state.get_enrichment_by_content_hash(full_text_hash)

        if enrichment_reused:
            state.log(f"  PJ ↷ enrichissement inchangé (skippé) : {att.filename[:60]}")
            enr = cached_enr
        elif cross_doc_cached_enr:
            state.log(f"  PJ ↷ enrichissement réutilisé (contenu identique à une autre PJ) : {att.filename[:60]}")
            enr = cross_doc_cached_enr
            _persist_enrichment(att.doc_id, enr, content_hash=full_text_hash)
        else:
            text_for_enrich = full_text[:8000]  # garde-fou taille prompt
            state.log(f"  PJ → enrichissement LLM : {att.filename[:60]}")
            _t0_att = time.time()
            enr = enrich.enrich_attachment(att.filename, text_for_enrich, em.subject, enrich_chat, log=state.log)
            _dt_att = time.time() - _t0_att
            _persist_enrichment(att.doc_id, enr, content_hash=full_text_hash)
            if enr:
                state.log(f"  PJ ✓ enrichie en {_dt_att:.1f}s : {att.filename[:50]}")
        if enr:
            # Le chunk d'enrichissement n'a besoin d'être ré-embeddé que si on vient
            # de le (re)calculer — un enrichissement réutilisé du cache est déjà
            # indexé sous forme de chunk depuis le run précédent.
            if not enrichment_reused:
                enr_meta = {
                    **{
                        "type": "attachment",
                        "doc_id": att.doc_id,
                        "email_id": em.doc_id,
                        "case_id": em.case_id,
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
    import sys

    cfg.ensure_dirs()
    try:
        result = run()
    except IndexAlreadyRunning as exc:
        print(f"[indexer] {exc} Réessayez une fois celle-ci terminée.", flush=True)
        sys.exit(1)
    print("Résultat :", result)
