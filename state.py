"""État partagé en SQLite (data/generated/app.sqlite) : progression d'indexation + registre.

- `index_progress` : une seule ligne (id=1) avec l'état courant du job.
- `index_log`      : journal défilant (messages horodatés).
- `documents`      : registre des doc_id déjà indexés (idempotence) + métadonnées
  d'affichage utilisées par la frise et la navigation (pas besoin de requêter
  Chroma pour afficher la timeline).

SQLite est suffisant et sans dépendance. Accès sérialisé par connexion courte.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import uuid
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


def add_column_if_missing(c: sqlite3.Connection, table: str, col: str, decl: str = "TEXT") -> None:
    """Migration additive et tolérante à la concurrence.

    Plusieurs processus (workers gunicorn, démon mail, CLI) initialisent le schéma
    en parallèle : entre le `PRAGMA table_info` et l'`ALTER`, un autre peut avoir
    ajouté la colonne. On avale donc spécifiquement « duplicate column name ».
    """
    cols = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
    if col in cols:
        return
    try:
        c.execute(f'ALTER TABLE {table} ADD COLUMN "{col}" {decl}')
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at REAL
            );

            CREATE TABLE IF NOT EXISTS job_lease (
                name         TEXT PRIMARY KEY,
                owner        TEXT,
                kind         TEXT,
                acquired_at  REAL,
                heartbeat_at REAL
            );

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
        for col in ("affaires", "thread_id", "in_reply_to", "references"):
            add_column_if_missing(c, "documents", col)
        for col in ("enrich_provider", "enrich_model"):
            add_column_if_missing(c, "index_progress", col)
        # Multi-dossiers + en-têtes utiles au classement. Défaut constant : les lignes
        # existantes appartiennent au dossier par défaut sans réécriture.
        add_column_if_missing(c, "documents", "case_id", "TEXT NOT NULL DEFAULT 'default'")
        for col in ("message_id", "from_addr", "to_addrs", "cc_addrs", "source"):
            add_column_if_missing(c, "documents", col)
        # Index créés APRÈS les ALTER (les colonnes peuvent ne pas exister avant).
        c.execute("CREATE INDEX IF NOT EXISTS idx_documents_case ON documents (case_id, type, date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_documents_thread ON documents (thread_id)")


# --------------------------------------------------------------------------- #
# Méta (drapeaux de migration ponctuelle, clé/valeur)
# --------------------------------------------------------------------------- #
def get_meta(key: str, default: str | None = None) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_meta(key: str, value: str | None) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO app_meta (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, time.time()),
        )


def set_meta_if_absent(key: str, value: str) -> bool:
    """Pose la valeur seulement si la clé n'existe pas. True si c'est nous qui l'avons posée."""
    with _conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO app_meta (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, time.time()),
        )
        return cur.rowcount == 1


def delete_meta(key: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM app_meta WHERE key=?", (key,))


# --------------------------------------------------------------------------- #
# Bail (lease) inter-processus
#
# Verrou portable (Linux/Windows, conteneurs partageant le même volume) pour les
# jobs exclusifs comme l'indexation. Le détenteur rafraîchit `heartbeat_at` ;
# un bail sans battement depuis LEASE_STALE_SECONDS est considéré abandonné
# (processus tué) et peut être repris, sans redémarrer l'app.
# --------------------------------------------------------------------------- #
INDEX_LEASE = "index"
LEASE_HEARTBEAT_SECONDS = 15
LEASE_STALE_SECONDS = 90


def new_lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def acquire_lease(name: str, owner: str, kind: str = "") -> bool:
    """Tente de prendre le bail. False s'il est détenu (et vivant) par un autre."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT owner, heartbeat_at FROM job_lease WHERE name=?", (name,)
        ).fetchone()
        now = time.time()
        if row and row[0] != owner and (now - (row[1] or 0)) < LEASE_STALE_SECONDS:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            """INSERT INTO job_lease (name, owner, kind, acquired_at, heartbeat_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, kind=excluded.kind,
                 acquired_at=excluded.acquired_at, heartbeat_at=excluded.heartbeat_at""",
            (name, owner, kind, now, now),
        )
        conn.execute("COMMIT")
        return True
    finally:
        conn.close()


def heartbeat_lease(name: str, owner: str) -> bool:
    """Rafraîchit le bail. False si on ne le détient plus (repris par un autre)."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE job_lease SET heartbeat_at=? WHERE name=? AND owner=?",
            (time.time(), name, owner),
        )
        return cur.rowcount == 1


def release_lease(name: str, owner: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM job_lease WHERE name=? AND owner=?", (name, owner))


def lease_alive(name: str) -> bool:
    with _conn() as c:
        row = c.execute("SELECT heartbeat_at FROM job_lease WHERE name=?", (name,)).fetchone()
    return bool(row and (time.time() - (row["heartbeat_at"] or 0)) < LEASE_STALE_SECONDS)


class LeaseKeeper:
    """Fil de battement : maintient le bail vivant pendant un job long.

    Nécessaire car un appel LLM (retries) ou un OCR peut bloquer plusieurs minutes.
    """

    def __init__(self, name: str, owner: str):
        self.name, self.owner = name, owner
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"lease-{name}", daemon=True)

    def _loop(self) -> None:
        while not self._stop.wait(LEASE_HEARTBEAT_SECONDS):
            try:
                heartbeat_lease(self.name, self.owner)
            except Exception as exc:  # base momentanément verrouillée : on réessaie au tour suivant
                print(f"[lease] battement manqué ({self.name}) : {exc}", flush=True)

    def start(self) -> "LeaseKeeper":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- #
# Progression
# --------------------------------------------------------------------------- #
def start_progress(
    total: int,
    enrich_provider: str | None = None,
    enrich_model: str | None = None,
    clear_log: bool = True,
) -> None:
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
        if clear_log:
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
    """Remet running=0 si un crash a laissé running=1.

    Ne touche à rien tant que le bail d'indexation est vivant : un autre processus
    (second worker gunicorn, démon mail, CLI) peut être en train d'indexer, et le
    (re)démarrage d'un worker ne doit pas effacer son état.
    """
    if lease_alive(INDEX_LEASE):
        return
    with _conn() as c:
        c.execute(
            "UPDATE index_progress SET running=0, phase='interrompu' WHERE id=1 AND running=1"
        )


def is_running() -> bool:
    """Vrai si une indexation tient le bail (quel que soit le processus)."""
    return lease_alive(INDEX_LEASE)


def get_status() -> dict:
    with _conn() as c:
        row = c.execute("SELECT * FROM index_progress WHERE id=1").fetchone()
        logs = c.execute(
            "SELECT ts, level, message FROM index_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
    status = dict(row) if row else {}
    # Le bail fait foi : un job tué laisse running=1 en base mais plus de battement.
    alive = lease_alive(INDEX_LEASE)
    if status.get("running") and not alive:
        status["phase"] = "interrompu"
    status["running"] = 1 if alive else 0
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
        # Colonnes ajoutées ensuite : None = « ne pas toucher à la valeur en base ».
        "case_id": None,
        "message_id": None,
        "from_addr": None,
        "to_addrs": None,
        "cc_addrs": None,
        "source": None,
    }
    with _conn() as c:
        c.execute(
            """INSERT INTO documents
                 (doc_id, type, correspondent, date, subject, affaire, affaires, email_id,
                  source_path, n_chunks, needs_ocr, content_hash,
                  thread_id, in_reply_to, "references", indexed_at,
                  case_id, message_id, from_addr, to_addrs, cc_addrs, source)
               VALUES
                 (:doc_id, :type, :correspondent, :date, :subject, :affaire, :affaires, :email_id,
                  :source_path, :n_chunks, :needs_ocr, :content_hash,
                  :thread_id, :in_reply_to, :references, :indexed_at,
                  COALESCE(:case_id, 'default'), :message_id, :from_addr, :to_addrs, :cc_addrs, :source)
               ON CONFLICT(doc_id) DO UPDATE SET
                 type=excluded.type, correspondent=excluded.correspondent,
                 date=excluded.date, subject=excluded.subject, affaire=excluded.affaire,
                 affaires=excluded.affaires,
                 email_id=excluded.email_id, source_path=excluded.source_path,
                 n_chunks=excluded.n_chunks, needs_ocr=excluded.needs_ocr,
                 content_hash=excluded.content_hash,
                 thread_id=excluded.thread_id, in_reply_to=excluded.in_reply_to,
                 "references"=excluded."references", indexed_at=excluded.indexed_at,
                 case_id=COALESCE(:case_id, documents.case_id),
                 message_id=COALESCE(excluded.message_id, documents.message_id),
                 from_addr=COALESCE(excluded.from_addr, documents.from_addr),
                 to_addrs=COALESCE(excluded.to_addrs, documents.to_addrs),
                 cc_addrs=COALESCE(excluded.cc_addrs, documents.cc_addrs),
                 source=COALESCE(excluded.source, documents.source)""",
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


def list_documents(type_: str | None = None, case_id: str | None = None) -> list[dict]:
    """Documents du registre. `case_id=None` : tous les dossiers (comportement d'origine)."""
    q = "SELECT * FROM documents"
    where, params = [], []
    if type_:
        where.append("type=?"); params.append(type_)
    if case_id:
        where.append("case_id=?"); params.append(case_id)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY date ASC"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def list_emails(case_id: str | None = None) -> list[dict]:
    return list_documents("mail", case_id=case_id)


def fill_email_headers(doc_id: str, **headers) -> None:
    """Complète les en-têtes manquants d'un document déjà indexé (sans rien écraser)."""
    cols = ("message_id", "from_addr", "to_addrs", "cc_addrs", "source")
    sets = [f"{k}=COALESCE({k}, ?)" for k in cols if headers.get(k)]
    if not sets:
        return
    with _conn() as c:
        c.execute(
            f"UPDATE documents SET {', '.join(sets)} WHERE doc_id=?",
            (*[headers[k] for k in cols if headers.get(k)], doc_id),
        )


def set_thread(doc_id: str, thread_id: str | None) -> None:
    with _conn() as c:
        c.execute("UPDATE documents SET thread_id=? WHERE doc_id=? AND thread_id IS NOT ?",
                  (thread_id, doc_id, thread_id))


def thread_candidates(cited_doc_ids: list[str], message_id: str | None, exclude_doc_id: str) -> list[str]:
    """Fils existants auxquels un email se rattache, du plus ancien au plus récent :
    ceux des emails qu'il cite, et ceux des emails qui le citent."""
    clauses, params = [], []
    if cited_doc_ids:
        clauses.append(f"doc_id IN ({','.join('?' * len(cited_doc_ids))})")
        params += cited_doc_ids
    if message_id:
        clauses.append("in_reply_to = ?")
        params.append(message_id)
        clauses.append("instr(COALESCE(\"references\", ''), ?) > 0")
        params.append(message_id)
    if not clauses:
        return []
    with _conn() as c:
        rows = c.execute(
            f"""SELECT thread_id, MIN(date) AS first FROM documents
                WHERE type='mail' AND thread_id IS NOT NULL AND doc_id != ?
                  AND ({' OR '.join(clauses)})
                GROUP BY thread_id ORDER BY first ASC""",
            (exclude_doc_id, *params),
        ).fetchall()
    return [r["thread_id"] for r in rows]


def cases_of_address(address: str) -> dict[str, int]:
    """{case_id: nombre d'emails} dont l'expéditeur est cette adresse."""
    if not address:
        return {}
    with _conn() as c:
        rows = c.execute(
            "SELECT case_id, COUNT(*) AS n FROM documents WHERE type='mail' AND from_addr=? GROUP BY case_id",
            (address.lower(),),
        ).fetchall()
    return {r["case_id"]: r["n"] for r in rows}


def cases_of_subject(normalized: str, normalizer) -> dict[str, int]:
    """{case_id: nombre d'emails} dont l'objet, normalisé par `normalizer`, vaut `normalized`."""
    with _conn() as c:
        rows = c.execute("SELECT case_id, subject FROM documents WHERE type='mail'").fetchall()
    out: dict[str, int] = {}
    for r in rows:
        if normalizer(r["subject"] or "") == normalized:
            out[r["case_id"]] = out.get(r["case_id"], 0) + 1
    return out


def set_source(doc_id: str, source: str) -> None:
    with _conn() as c:
        c.execute("UPDATE documents SET source=? WHERE doc_id=?", (source, doc_id))


def replies_to(message_id: str) -> list[str]:
    """doc_id des emails déjà indexés qui répondent directement à ce Message-ID."""
    if not message_id:
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT doc_id FROM documents WHERE type='mail' AND in_reply_to=?", (message_id,)
        ).fetchall()
    return [r["doc_id"] for r in rows]


def merge_threads(keep: str, absorbed: list[str]) -> None:
    """Fusionne des fils (un nouvel email vient de les relier)."""
    if not absorbed:
        return
    with _conn() as c:
        c.execute(
            f"UPDATE documents SET thread_id=? WHERE thread_id IN ({','.join('?' * len(absorbed))})",
            (keep, *absorbed),
        )


def set_documents_case(email_id: str, case_id: str) -> list[str]:
    """Rattache un email et ses pièces jointes à un dossier. Retourne les doc_id touchés."""
    with _conn() as c:
        ids = [r["doc_id"] for r in c.execute(
            "SELECT doc_id FROM documents WHERE doc_id=? OR email_id=?", (email_id, email_id)
        ).fetchall()]
        c.execute("UPDATE documents SET case_id=? WHERE doc_id=? OR email_id=?",
                  (case_id, email_id, email_id))
    return ids


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
