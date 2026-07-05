"""Authentification : table `users` en SQLite (data/generated/app.sqlite).

Même pattern que state.py : connexion courte via `_conn()`, `CREATE TABLE IF NOT
EXISTS` + migration de colonnes au démarrage. Mots de passe hashés avec
werkzeug.security (déjà une dépendance transitive de Flask).
"""
from __future__ import annotations

import secrets
import string
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

from werkzeug.security import check_password_hash, generate_password_hash

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


def init_users_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                username             TEXT UNIQUE NOT NULL,
                password_hash        TEXT NOT NULL,
                role                 TEXT NOT NULL DEFAULT 'user',
                must_change_password INTEGER DEFAULT 1,
                created_at           REAL,
                created_by           TEXT
            );
            """
        )


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def create_user(username: str, password: str, role: str = "user", created_by: str | None = None) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO users (username, password_hash, role, must_change_password, created_at, created_by)
               VALUES (?, ?, ?, 1, ?, ?)""",
            (username, generate_password_hash(password), role, time.time(), created_by),
        )
        return cur.lastrowid


def get_user_by_username(username: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]


def count_users(role: str | None = None) -> int:
    with _conn() as c:
        if role:
            row = c.execute("SELECT COUNT(*) AS n FROM users WHERE role=?", (role,)).fetchone()
        else:
            row = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return row["n"]


def set_password(user_id: int, password: str, must_change: bool = False) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE users SET password_hash=?, must_change_password=? WHERE id=?",
            (generate_password_hash(password), int(must_change), user_id),
        )


def clear_must_change_password(user_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET must_change_password=0 WHERE id=?", (user_id,))


def delete_user(user_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM users WHERE id=?", (user_id,))


def verify_password(username: str, password: str) -> dict | None:
    user = get_user_by_username(username)
    if not user or not check_password_hash(user["password_hash"], password):
        return None
    return user


def ensure_admin_bootstrap(admin_username: str, admin_password: str | None) -> str | None:
    """Crée le compte admin si la table users est vide. Retourne le mot de passe

    généré (à afficher une fois) si ADMIN_PASSWORD n'était pas fourni, sinon None.
    """
    if count_users() > 0:
        return None
    generated = None
    password = admin_password
    if not password:
        generated = generate_password()
        password = generated
    create_user(admin_username, password, role="admin", created_by=None)
    return generated
