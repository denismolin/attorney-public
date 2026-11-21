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
            CREATE TABLE IF NOT EXISTS login_attempts (
                key TEXT NOT NULL,
                ts  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_login_attempts_key ON login_attempts(key, ts);
            """
        )


# --------------------------------------------------------------------------- #
# Limitation des tentatives de connexion (partagée entre workers via SQLite)
# --------------------------------------------------------------------------- #
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_PER_USER = 8       # échecs par identifiant sur la fenêtre
LOGIN_MAX_PER_ADDR = 40      # échecs par adresse sur la fenêtre (large : proxy partagé)


def _login_keys(username: str, addr: str | None) -> list[tuple[str, int]]:
    keys = [("u:" + (username or "").strip().lower()[:80], LOGIN_MAX_PER_USER)]
    if addr:
        keys.append(("a:" + addr[:80], LOGIN_MAX_PER_ADDR))
    return keys


def login_retry_after(username: str, addr: str | None) -> int:
    """Secondes à attendre avant une nouvelle tentative (0 = tentative autorisée)."""
    now = time.time()
    since = now - LOGIN_WINDOW_SECONDS
    wait = 0
    with _conn() as c:
        for key, limit in _login_keys(username, addr):
            rows = c.execute(
                "SELECT ts FROM login_attempts WHERE key=? AND ts>? ORDER BY ts DESC LIMIT ?",
                (key, since, limit),
            ).fetchall()
            if len(rows) >= limit:
                # Déblocage quand le plus ancien des `limit` derniers échecs sort de la fenêtre.
                wait = max(wait, int(rows[-1]["ts"] + LOGIN_WINDOW_SECONDS - now) + 1)
    return wait


def record_login_failure(username: str, addr: str | None) -> None:
    now = time.time()
    with _conn() as c:
        c.execute("DELETE FROM login_attempts WHERE ts<?", (now - LOGIN_WINDOW_SECONDS,))
        c.executemany(
            "INSERT INTO login_attempts (key, ts) VALUES (?, ?)",
            [(key, now) for key, _ in _login_keys(username, addr)],
        )


def clear_login_failures(username: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM login_attempts WHERE key=?", ("u:" + (username or "").strip().lower()[:80],))


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


_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


def verify_password(username: str, password: str) -> dict | None:
    user = get_user_by_username(username)
    if not user:
        # Même coût de calcul qu'un compte existant : le temps de réponse ne révèle pas
        # si l'identifiant existe.
        check_password_hash(_DUMMY_HASH, password)
        return None
    if not check_password_hash(user["password_hash"], password):
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
    try:
        user_id = create_user(admin_username, password, role="admin", created_by=None)
    except sqlite3.IntegrityError:
        # Un autre worker vient de créer le compte au même instant (premier démarrage).
        return None
    if not generated:
        # Mot de passe choisi par l'opérateur (ADMIN_PASSWORD) : pas de changement imposé.
        clear_must_change_password(user_id)
    return generated


# --------------------------------------------------------------------------- #
# Première installation : code à usage unique protégeant la création de l'admin
# --------------------------------------------------------------------------- #
_SETUP_SECRET_KEY = "setup_secret"
_SETUP_FAILS_KEY = "setup_failures"
SETUP_MAX_FAILURES = 10


def setup_code() -> str:
    """Code d'installation, identique pour tous les processus de l'app.

    Dérivé d'un secret tiré une fois et stocké en base : les workers gunicorn, qui
    démarrent en parallèle, affichent ainsi le même code. Il n'est jamais servi par
    HTTP ; seul qui lit les journaux du serveur le connaît.
    """
    import hashlib
    import hmac
    import state

    state.set_meta_if_absent(_SETUP_SECRET_KEY, secrets.token_hex(32))
    secret = state.get_meta(_SETUP_SECRET_KEY) or ""
    digest = hmac.new(secret.encode(), b"setup-code", hashlib.sha256).hexdigest().upper()
    return f"{digest[:4]}-{digest[4:8]}-{digest[8:12]}"


def check_setup_code(candidate: str) -> bool:
    """Compare en temps constant. Après trop d'échecs, le code est remplacé (le nouveau
    est affiché dans les journaux) : il ne peut pas être deviné par essais successifs."""
    import hmac
    import state

    wanted = setup_code()
    given = "".join(ch for ch in (candidate or "").upper() if ch.isalnum())
    if hmac.compare_digest(given, wanted.replace("-", "")):
        state.delete_meta(_SETUP_FAILS_KEY)
        return True
    fails = int(state.get_meta(_SETUP_FAILS_KEY, "0") or 0) + 1
    state.set_meta(_SETUP_FAILS_KEY, str(fails))
    if fails >= SETUP_MAX_FAILURES:
        state.delete_meta(_SETUP_SECRET_KEY)
        state.delete_meta(_SETUP_FAILS_KEY)
        print(f"[setup] Trop d'essais : nouveau code d'installation : {setup_code()}", flush=True)
    return False


def clear_setup_code() -> None:
    import state
    state.delete_meta(_SETUP_SECRET_KEY)
    state.delete_meta(_SETUP_FAILS_KEY)


def apply_password_policy_migration() -> None:
    """Migration ponctuelle : exempte les comptes existants du changement imposé.

    Le drapeau `must_change_password` valait 1 pour tous les comptes sans jamais être
    appliqué. Il l'est désormais, mais seulement pour les comptes créés ou
    réinitialisés après cette mise à jour : les comptes déjà en place se connectent
    comme avant.
    """
    import state
    if state.set_meta_if_absent("password_enforcement", str(time.time())):
        with _conn() as c:
            c.execute("UPDATE users SET must_change_password=0")
