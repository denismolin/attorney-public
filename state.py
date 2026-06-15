"""État partagé en SQLite (data/app.sqlite) : progression d'indexation + registre.

- `index_progress` : une seule ligne (id=1) avec l'état courant du job.
- `index_log`      : journal défilant (messages horodatés).
- `documents`      : registre des doc_id déjà indexés (idempotence) + métadonnées
  d'affichage utilisées par la frise et la navigation (pas besoin de requêter
  Chroma pour afficher la timeline).

SQLite est suffisant et sans dépendance. Accès sérialisé par connexion courte.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from config import DB_PATH


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS index_progress (
                id              INTEGER PRIMARY KEY CHECK (id = 1),
                phase           TEXT,
                current         INTEGER DEFAULT 0,
                total           INTEGER DEFAULT 0,
                current_doc     TEXT,
                errors          INTEGER DEFAULT 0,
                running         INTEGER DEFAULT 0,
                started_at      REAL,
                updated_at      REAL,
                finished_at     REAL,
                enrich_provider TEXT,
                enrich_model    TEXT
            );

            CREATE TABLE IF NOT EXISTS index_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        REAL,
                level     TEXT,
                message   TEXT
            );

            CREATE TABLE IF NOT EXISTS documents (
                doc_id        TEXT PRIMARY KEY,
                type          TEXT,          -- 'mail' | 'attachment'
                correspondent TEXT,
                date          TEXT,          -- ISO
                subject       TEXT,          -- mail: sujet ; PJ: nom de fichier
                affaire       TEXT,          -- affaire principale (couleur/affichage)
                affaires      TEXT,          -- toutes les affaires, CSV (filtrage multi)
                email_id      TEXT,          -- mail parent (pour une PJ) ; sinon = doc_id
                source_path   TEXT,
                n_chunks      INTEGER DEFAULT 0,
                needs_ocr     INTEGER DEFAULT 0,
                content_hash  TEXT,
                thread_id     TEXT,          -- fil de discussion (In-Reply-To/References)
                in_reply_to   TEXT,          -- Message-ID auquel ce mail répond
                "references"  TEXT,          -- Message-IDs cités, CSV (mot réservé SQL -> quoté)
                indexed_at    REAL
            );
            """
        )
        # Garantit la ligne unique de progression.
        c.execute("INSERT OR IGNORE INTO index_progress (id, running) VALUES (1, 0)")
        # Migration : ajoute toute colonne TEXT manquante sur une table préexistante.
        cols = {row[1] for row in c.execute("PRAGMA table_info(documents)").fetchall()}
        for col in ("affaires", "thread_id", "in_reply_to", "references"):
            if col not in cols:
                c.execute(f'ALTER TABLE documents ADD COLUMN "{col}" TEXT')
        prog_cols = {row[1] for row in c.execute("PRAGMA table_info(index_progress)").fetchall()}
        for col in ("enrich_provider", "enrich_model"):
            if col not in prog_cols:
                c.execute(f"ALTER TABLE index_progress ADD COLUMN {col} TEXT")


# --------------------------------------------------------------------------- #
# Progression
# --------------------------------------------------------------------------- #
def start_progress(total: int, enrich_provider: str | None = None, enrich_model: str | None = None) -> None:
    now = time.time()
    with _conn() as c:
        c.execute(
            """UPDATE index_progress
               SET phase='démarrage', current=0, total=?, current_doc=NULL,
                   errors=0, running=1, started_at=?, updated_at=?, finished_at=NULL,
                   enrich_provider=?, enrich_model=?
               WHERE id=1""",
            (total, now, now, enrich_provider, enrich_model),
        )
        c.execute("DELETE FROM index_log")


def update_progress(
    phase: str | None = None,
    current: int | None = None,
    total: int | None = None,
    current_doc: str | None = None,
    inc_errors: int = 0,
) -> None:
    sets, params = [], []
    if phase is not None:
        sets.append("phase=?"); params.append(phase)
    if current is not None:
        sets.append("current=?"); params.append(current)
    if total is not None:
        sets.append("total=?"); params.append(total)
    if current_doc is not None:
        sets.append("current_doc=?"); params.append(current_doc)
    if inc_errors:
        sets.append("errors=errors+?"); params.append(inc_errors)
    sets.append("updated_at=?"); params.append(time.time())
    with _conn() as c:
        c.execute(f"UPDATE index_progress SET {', '.join(sets)} WHERE id=1", params)


def finish_progress(phase: str = "terminé") -> None:
    now = time.time()
    with _conn() as c:
        c.execute(
            "UPDATE index_progress SET phase=?, running=0, updated_at=?, finished_at=? WHERE id=1",
            (phase, now, now),
        )


def reset_stale_running() -> None:
    """Au démarrage de l'app : remet running=0 si un crash a laissé running=1."""
    with _conn() as c:
        c.execute(
            "UPDATE index_progress SET running=0, phase='interrompu' WHERE id=1 AND running=1"
        )


def is_running() -> bool:
    with _conn() as c:
        row = c.execute("SELECT running FROM index_progress WHERE id=1").fetchone()
        return bool(row and row["running"])


def get_status() -> dict:
    with _conn() as c:
        row = c.execute("SELECT * FROM index_progress WHERE id=1").fetchone()
        logs = c.execute(
            "SELECT ts, level, message FROM index_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
    status = dict(row) if row else {}
    status["log"] = [dict(r) for r in reversed(logs)]
    return status


def log(message: str, level: str = "info") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO index_log (ts, level, message) VALUES (?, ?, ?)",
            (time.time(), level, message),
        )


# --------------------------------------------------------------------------- #
# Registre des documents (idempotence + affichage timeline)
# --------------------------------------------------------------------------- #
def upsert_document(doc: dict) -> None:
    # Valeurs par défaut pour les colonnes optionnelles (threading) afin que les
    # appelants qui ne les fournissent pas (PJ sans fil) restent valides.
    defaults = {
        "indexed_at": time.time(),
        "affaires": doc.get("affaire", ""),
        "thread_id": None,
        "in_reply_to": None,
        "references": None,
    }
    with _conn() as c:
        c.execute(
            """INSERT INTO documents
                 (doc_id, type, correspondent, date, subject, affaire, affaires, email_id,
                  source_path, n_chunks, needs_ocr, content_hash,
                  thread_id, in_reply_to, "references", indexed_at)
               VALUES
                 (:doc_id, :type, :correspondent, :date, :subject, :affaire, :affaires, :email_id,
                  :source_path, :n_chunks, :needs_ocr, :content_hash,
                  :thread_id, :in_reply_to, :references, :indexed_at)
               ON CONFLICT(doc_id) DO UPDATE SET
                 type=excluded.type, correspondent=excluded.correspondent,
                 date=excluded.date, subject=excluded.subject, affaire=excluded.affaire,
                 affaires=excluded.affaires,
                 email_id=excluded.email_id, source_path=excluded.source_path,
                 n_chunks=excluded.n_chunks, needs_ocr=excluded.needs_ocr,
                 content_hash=excluded.content_hash,
                 thread_id=excluded.thread_id, in_reply_to=excluded.in_reply_to,
                 "references"=excluded."references", indexed_at=excluded.indexed_at""",
            {**defaults, **doc},
        )


def get_document(doc_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        return dict(row) if row else None


def get_document_hash(doc_id: str) -> str | None:
    with _conn() as c:
        row = c.execute(
            "SELECT content_hash FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        return row["content_hash"] if row else None


def list_documents(type_: str | None = None) -> list[dict]:
    q = "SELECT * FROM documents"
    params: tuple = ()
    if type_:
        q += " WHERE type=?"
        params = (type_,)
    q += " ORDER BY date ASC"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def list_emails() -> list[dict]:
    return list_documents("mail")


def get_thread_docs(thread_id: str, exclude_doc_id: str | None = None) -> list[dict]:
    """Tous les documents d'un fil de discussion, triés par date."""
    if not thread_id:
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM documents WHERE thread_id=? ORDER BY date ASC", (thread_id,)
        ).fetchall()
    docs = [dict(r) for r in rows]
    if exclude_doc_id:
        docs = [d for d in docs if d["doc_id"] != exclude_doc_id]
    return docs


def get_attachments_of(email_id: str) -> list[dict]:
    """Pièces jointes rattachées à un mail (via email_id)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM documents WHERE type='attachment' AND email_id=? AND doc_id != email_id",
            (email_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def reset_documents() -> None:
    """Vide la table documents (repart de zéro pour l'idempotence)."""
    with _conn() as c:
        c.execute("DELETE FROM documents")
        c.execute(
            """UPDATE index_progress
               SET phase='réinitialisé', current=0, total=0, current_doc=NULL,
                   errors=0, running=0, started_at=NULL, updated_at=?, finished_at=NULL
               WHERE id=1""",
            (time.time(),),
        )
        c.execute("DELETE FROM index_log")
