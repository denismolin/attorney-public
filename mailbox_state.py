"""Boîte mail de collecte : état du démon, journal, file de tri (SQLite partagé).

- `mailbox_state` : une ligne (id=1) — suivi IMAP (UIDVALIDITY, dernier UID) et
  battement du démon.
- `mailbox_log`   : journal défilant du démon.
- `inbox_items`   : un email reçu = une ligne, de la réception au classement.

Statuts d'un élément :
  pending      en attente de tri par un utilisateur
  needs_admin  un utilisateur propose un nouveau dossier, à valider par un admin
  filed        classé dans un dossier, en attente d'indexation par le démon
  indexed      indexé (visible dans le dossier)
  rejected     écarté par un utilisateur
  duplicate    déjà présent dans un dossier
  quarantined  expéditeur non autorisé ou non authentifié
  error        traitement impossible (voir `error`)
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

from config import DB_PATH

OPEN_STATUSES = ("pending", "needs_admin")
DAEMON_STALE_SECONDS = 180


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


def init_mailbox_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS mailbox_state (
                id                  INTEGER PRIMARY KEY CHECK (id = 1),
                account_key         TEXT,       -- hôte|utilisateur|dossier : un changement réinitialise le suivi
                uidvalidity         INTEGER,
                last_uid            INTEGER DEFAULT 0,
                last_poll_at        REAL,
                last_ok_at          REAL,
                last_error          TEXT,
                consecutive_errors  INTEGER DEFAULT 0,
                daemon_heartbeat_at REAL,
                daemon_host         TEXT
            );

            CREATE TABLE IF NOT EXISTS mailbox_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      REAL,
                level   TEXT,
                message TEXT
            );

            CREATE TABLE IF NOT EXISTS inbox_items (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                uid                INTEGER,
                uidvalidity        INTEGER,
                received_at        REAL,
                forwarder          TEXT,          -- adresse de la personne qui a transféré
                forwarder_note     TEXT,          -- son mot d'accompagnement (non indexé)
                sender_verified    INTEGER DEFAULT 0,
                orig_from          TEXT,
                orig_to            TEXT,
                orig_date          TEXT,
                subject            TEXT,
                preview            TEXT,
                n_attachments      INTEGER DEFAULT 0,
                sha256             TEXT UNIQUE,   -- empreinte de l'email reconstitué
                wrapper_sha256     TEXT,          -- empreinte du message reçu (transfert)
                message_id         TEXT,
                doc_id             TEXT,
                raw_path           TEXT,          -- message reçu, tel quel (quarantaine)
                stored_path        TEXT,          -- email reconstitué (en attente, puis dans le dossier)
                unwrap_mode        TEXT,          -- rfc822 | eml_attachment | inline | none
                status             TEXT NOT NULL DEFAULT 'pending',
                suggested_case_id  TEXT,
                confidence         REAL,
                deterministic      INTEGER DEFAULT 0,
                reasons_json       TEXT,
                candidates_json    TEXT,
                proposed_case_name TEXT,
                case_id            TEXT,
                decided_by         TEXT,
                decided_at         REAL,
                error              TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox_items (status, received_at);
            CREATE INDEX IF NOT EXISTS idx_inbox_wrapper ON inbox_items (wrapper_sha256);
            """
        )
        c.execute("INSERT OR IGNORE INTO mailbox_state (id, last_uid) VALUES (1, 0)")


# --------------------------------------------------------------------------- #
# État du démon
# --------------------------------------------------------------------------- #
def get_state() -> dict:
    with _conn() as c:
        row = c.execute("SELECT * FROM mailbox_state WHERE id=1").fetchone()
    st = dict(row) if row else {}
    hb = st.get("daemon_heartbeat_at") or 0
    st["daemon_alive"] = (time.time() - hb) < DAEMON_STALE_SECONDS
    return st


def update_state(**fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE mailbox_state SET {sets} WHERE id=1", tuple(fields.values()))


def heartbeat(host: str) -> None:
    update_state(daemon_heartbeat_at=time.time(), daemon_host=host)


def log(message: str, level: str = "info") -> None:
    print(f"[mail] {message}", flush=True)
    with _conn() as c:
        c.execute("INSERT INTO mailbox_log (ts, level, message) VALUES (?, ?, ?)",
                  (time.time(), level, message))
        # Journal borné : on ne garde que les 500 dernières lignes.
        c.execute("DELETE FROM mailbox_log WHERE id <= (SELECT MAX(id) FROM mailbox_log) - 500")


def get_log(limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, level, message FROM mailbox_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


# --------------------------------------------------------------------------- #
# File de tri
# --------------------------------------------------------------------------- #
def _item(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    d = dict(row)
    for key in ("reasons", "candidates"):
        try:
            d[key] = json.loads(d.pop(f"{key}_json") or "[]")
        except Exception:
            d[key] = []
    return d


def insert_item(item: dict) -> int | None:
    """Ajoute un élément. None si le même email (même empreinte) est déjà en file."""
    item = dict(item)
    item["reasons_json"] = json.dumps(item.pop("reasons", []), ensure_ascii=False)
    item["candidates_json"] = json.dumps(item.pop("candidates", []), ensure_ascii=False)
    item.setdefault("received_at", time.time())
    cols = list(item)
    with _conn() as c:
        cur = c.execute(
            f"INSERT OR IGNORE INTO inbox_items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(item[k] for k in cols),
        )
        return cur.lastrowid if cur.rowcount else None


def update_item(item_id: int, **fields) -> None:
    if "reasons" in fields:
        fields["reasons_json"] = json.dumps(fields.pop("reasons"), ensure_ascii=False)
    if "candidates" in fields:
        fields["candidates_json"] = json.dumps(fields.pop("candidates"), ensure_ascii=False)
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE inbox_items SET {sets} WHERE id=?", (*fields.values(), item_id))


def claim_item(item_id: int, from_statuses: tuple[str, ...], to_status: str, **fields) -> bool:
    """Change le statut seulement s'il est encore l'un de `from_statuses` : deux
    utilisateurs qui trient le même email en même temps ne le classent pas deux fois."""
    fields = {"status": to_status, **fields}
    sets = ", ".join(f"{k}=?" for k in fields)
    marks = ", ".join("?" * len(from_statuses))
    with _conn() as c:
        cur = c.execute(
            f"UPDATE inbox_items SET {sets} WHERE id=? AND status IN ({marks})",
            (*fields.values(), item_id, *from_statuses),
        )
        return cur.rowcount == 1


def delete_item(item_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM inbox_items WHERE id=?", (item_id,))


def get_item(item_id: int) -> dict | None:
    with _conn() as c:
        return _item(c.execute("SELECT * FROM inbox_items WHERE id=?", (item_id,)).fetchone())


def wrapper_seen(wrapper_sha256: str) -> bool:
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM inbox_items WHERE wrapper_sha256=? LIMIT 1", (wrapper_sha256,)
        ).fetchone() is not None


def find_by_message_id(message_id: str, statuses: tuple[str, ...]) -> dict | None:
    if not message_id:
        return None
    marks = ", ".join("?" * len(statuses))
    with _conn() as c:
        return _item(c.execute(
            f"SELECT * FROM inbox_items WHERE message_id=? AND status IN ({marks}) ORDER BY id LIMIT 1",
            (message_id, *statuses),
        ).fetchone())


def list_items(statuses: tuple[str, ...] | None = None, limit: int = 200) -> list[dict]:
    q, params = "SELECT * FROM inbox_items", []
    if statuses:
        q += f" WHERE status IN ({', '.join('?' * len(statuses))})"
        params += list(statuses)
    q += " ORDER BY received_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        return [_item(r) for r in c.execute(q, params).fetchall()]


def count_by_status() -> dict[str, int]:
    with _conn() as c:
        rows = c.execute("SELECT status, COUNT(*) AS n FROM inbox_items GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def count_open(include_admin: bool = False) -> int:
    counts = count_by_status()
    n = counts.get("pending", 0)
    if include_admin:
        n += counts.get("needs_admin", 0)
    return n
