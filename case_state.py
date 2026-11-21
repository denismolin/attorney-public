"""Dossiers (« cases ») : registre, dossier actif, textes et prompts par dossier.

Un dossier regroupe la correspondance d'une affaire. Le dossier `default` est celui
que l'application servait avant la gestion multi-dossiers : tout ce qui existait
lui appartient, et ses prompts sont ceux d'origine, à l'identique.

Règles :
- **Le dossier d'un email est donné par son emplacement** sous SOURCE_DIR
  (`cases/<case_id>/<expéditeur>/x.eml` ; tout le reste = `default`). Le registre
  SQLite n'en est qu'un reflet : une réinitialisation + réindexation le retrouve.
- **Tant qu'il n'existe que le dossier par défaut, aucune recherche n'est filtrée** :
  le comportement est strictement celui d'avant.
- Le dossier actif d'un traitement est porté par une variable de contexte
  (`use_case`), lue par la recherche et par la construction des prompts.
"""
from __future__ import annotations

import contextvars
import json
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import case_defaults
import case_private
from config import DB_PATH, SOURCE_DIR

DEFAULT = case_defaults.DEFAULT_CASE_ID
CASES_DIRNAME = "cases"
BACKFILL_META_KEY = "chroma_case_backfill"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("case_id", default=None)


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


def init_cases_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS cases (
                case_id          TEXT PRIMARY KEY,   -- identifiant court, immuable (nom de dossier)
                name             TEXT NOT NULL,
                description      TEXT,
                context_md       TEXT,               -- note de contexte injectée dans les prompts ;
                                                     -- NULL sur 'default' = prompts d'origine
                suggestions_json TEXT,               -- prompts suggérés propres au dossier (optionnel)
                status           TEXT NOT NULL DEFAULT 'active',   -- active | archived
                created_at       REAL,
                created_by       TEXT,
                updated_at       REAL
            );

            CREATE TABLE IF NOT EXISTS case_participants (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id    TEXT NOT NULL,
                kind       TEXT NOT NULL,            -- email | domain | name
                value      TEXT NOT NULL,
                value_norm TEXT NOT NULL,
                label      TEXT,
                role       TEXT,
                source     TEXT,                     -- manual | observed | confirmed
                hits       INTEGER DEFAULT 0,
                created_at REAL,
                UNIQUE (case_id, kind, value_norm)
            );
            CREATE INDEX IF NOT EXISTS idx_participants_norm ON case_participants (kind, value_norm);
            """
        )
        c.execute(
            "INSERT OR IGNORE INTO cases (case_id, name, status, created_at) VALUES (?, ?, 'active', ?)",
            (DEFAULT, case_defaults.DEFAULT_CASE_NAME, time.time()),
        )


# --------------------------------------------------------------------------- #
# Registre
# --------------------------------------------------------------------------- #
def _row(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    d = dict(row)
    try:
        d["suggestions"] = json.loads(d.pop("suggestions_json") or "null")
    except Exception:
        d["suggestions"] = None
    d["is_default"] = d["case_id"] == DEFAULT
    return d


def list_cases(include_archived: bool = False) -> list[dict]:
    q = "SELECT * FROM cases"
    if not include_archived:
        q += " WHERE status='active'"
    q += " ORDER BY (case_id != ?) ASC, name COLLATE NOCASE ASC"
    with _conn() as c:
        return [_row(r) for r in c.execute(q, (DEFAULT,)).fetchall()]


def get_case(case_id: str | None) -> dict | None:
    if not case_id:
        return None
    with _conn() as c:
        return _row(c.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone())


def exists(case_id: str | None, active_only: bool = True) -> bool:
    case = get_case(case_id)
    return bool(case and (case["status"] == "active" or not active_only))


def multi_case() -> bool:
    """Vrai dès qu'un second dossier existe (archivé ou non)."""
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM cases").fetchone()[0] > 1


def valid_slug(case_id: str) -> bool:
    return bool(_SLUG_RE.match(case_id or "")) and case_id != CASES_DIRNAME


def slugify(name: str) -> str:
    """Identifiant de dossier dérivé du nom (ASCII minuscule, tirets), unique."""
    base = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:32].strip("-") or "dossier"
    if len(base) < 2:
        base = "dossier-" + base
    slug, n = base, 2
    while get_case(slug) or slug == CASES_DIRNAME:
        slug = f"{base[:36]}-{n}"
        n += 1
    return slug


def create_case(
    name: str,
    description: str = "",
    context_md: str = "",
    case_id: str | None = None,
    created_by: str | None = None,
) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("Le nom du dossier est requis.")
    case_id = (case_id or "").strip().lower() or slugify(name)
    if not valid_slug(case_id):
        raise ValueError("Identifiant invalide : 2 à 40 caractères, lettres minuscules, chiffres et tirets.")
    if get_case(case_id):
        raise ValueError("Un dossier porte déjà cet identifiant.")
    now = time.time()
    with _conn() as c:
        c.execute(
            """INSERT INTO cases (case_id, name, description, context_md, status, created_at, created_by, updated_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?, ?)""",
            (case_id, name, description.strip(), context_md.strip(), now, created_by, now),
        )
    case_dir(case_id).mkdir(parents=True, exist_ok=True)
    return get_case(case_id)


def update_case(case_id: str, **fields) -> dict | None:
    allowed = {"name", "description", "context_md", "status"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "status" and v not in ("active", "archived"):
            raise ValueError("Statut invalide.")
        if k == "status" and case_id == DEFAULT and v != "active":
            raise ValueError("Le dossier principal ne peut pas être archivé.")
        if k == "name" and not str(v).strip():
            raise ValueError("Le nom du dossier est requis.")
        # Sur le dossier par défaut, une note vide = NULL = prompts d'origine.
        if k == "context_md" and not str(v).strip():
            sets.append("context_md=NULL")
            continue
        sets.append(f"{k}=?")
        params.append(str(v).strip())
    if sets:
        sets.append("updated_at=?")
        params.append(time.time())
        with _conn() as c:
            c.execute(f"UPDATE cases SET {', '.join(sets)} WHERE case_id=?", (*params, case_id))
    return get_case(case_id)


def case_dir(case_id: str) -> Path:
    """Racine des emails d'un dossier non-défaut : SOURCE_DIR/cases/<case_id>/."""
    return SOURCE_DIR / CASES_DIRNAME / case_id


def counts_by_case() -> dict[str, int]:
    with _conn() as c:
        rows = c.execute(
            "SELECT case_id, COUNT(*) AS n FROM documents WHERE type='mail' GROUP BY case_id"
        ).fetchall()
    return {r["case_id"]: r["n"] for r in rows}


# --------------------------------------------------------------------------- #
# Dossier actif (variable de contexte)
# --------------------------------------------------------------------------- #
def active_case_id() -> str:
    return _current.get() or DEFAULT


def set_current(case_id: str | None):
    return _current.set(case_id or None)


@contextmanager
def use_case(case_id: str | None):
    token = _current.set(case_id or None)
    try:
        yield
    finally:
        _current.reset(token)


def run_in_case(case_id: str | None, fn, *args, **kwargs):
    """Exécute `fn` avec `case_id` comme dossier actif (cible de thread de fond :
    un nouveau thread ne voit pas la variable de contexte de la requête)."""
    with use_case(case_id):
        return fn(*args, **kwargs)


def query_filter() -> str | None:
    """Dossier sur lequel filtrer les recherches, ou None s'il n'y a rien à filtrer.

    Avec le seul dossier par défaut, on ne filtre pas : les vecteurs indexés avant
    cette fonctionnalité ne portent pas tous la métadonnée `case_id`, et le
    comportement doit rester celui d'avant.
    """
    return active_case_id() if multi_case() else None


def vectors_tagged() -> bool:
    import state
    return state.get_meta(BACKFILL_META_KEY) == "done"


# --------------------------------------------------------------------------- #
# Prompts et textes d'interface
# --------------------------------------------------------------------------- #
# Début de la partie générique (règles, format de sortie) de chaque prompt d'origine.
# Ce qui précède décrit l'affaire d'origine et est remplacé pour les autres dossiers.
_PROMPT_BODY_MARKERS = {
    "chat": "\nRègles impératives",
    "intent": "\nÀ partir de la question",
    "synthesis": "\nTa mission est de construire",
    "advisor": "\nMéthode :",
    "enrich_email": "À partir de l'email fourni",
    "enrich_attachment": "À partir du texte du document fourni",
}

_GENERIC_INTROS = {
    "chat": "Tu es un assistant juridique qui aide à analyser la correspondance du dossier « {name} ».\n{context}",
    "intent": "Tu es un assistant juridique qui analyse une question sur le dossier « {name} ».\n",
    "synthesis": "Tu es un juriste assistant qui analyse la correspondance complète du dossier « {name} ».\n{context}",
    "advisor": (
        "Tu es l'AVOCAT CONSEIL du cabinet dans le dossier « {name} ». Tu défends les intérêts "
        "du client du cabinet, tels que décrits dans le contexte du dossier ci-dessous.\n{context}\n"
        "L'utilisateur te soumet un SCÉNARIO ou un DIRE de la partie adverse. Ta mission : "
        "élaborer une RÉACTION STRATÉGIQUE SUR LE FOND.\n"
    ),
    "enrich_email": "Tu es un assistant juridique qui analyse la correspondance du dossier « {name} ».\n",
    "enrich_attachment": "Tu es un assistant juridique qui analyse une pièce jointe du dossier « {name} ».\n",
}


def prompt_for(kind: str, legacy_text: str, case_id: str | None = None) -> str:
    """Prompt système pour le dossier actif.

    Dossier sans note de contexte (cas du dossier par défaut) : le prompt d'origine,
    inchangé. Sinon : une introduction propre au dossier, suivie des règles et du
    format de sortie du prompt d'origine.
    """
    case = get_case(case_id or active_case_id())
    if not case or not (case.get("context_md") or "").strip():
        if not case or case["is_default"]:
            # Textes propres à l'installation (fichier privé, hors image) s'ils existent,
            # sinon le prompt neutre du code.
            return case_private.prompt(kind) or legacy_text
    name = case["name"]
    ctx = (case.get("context_md") or "").strip()
    context = (
        "\nCONTEXTE DU DOSSIER (note du cabinet, à tenir pour acquise) :\n"
        f"<contexte_dossier>\n{ctx}\n</contexte_dossier>\n"
    ) if ctx else ""
    marker = _PROMPT_BODY_MARKERS.get(kind)
    intro = _GENERIC_INTROS.get(kind, "").format(name=name, context=context)
    if marker and marker in legacy_text and intro:
        return intro + legacy_text[legacy_text.index(marker):]
    # Repli : prompt d'origine complété par le contexte du dossier.
    return legacy_text + f"\n\nDossier concerné : « {name} ».{context}"


def ui_for(case_id: str | None = None) -> dict:
    """Libellés, exemples et prompts suggérés affichés pour un dossier."""
    case = get_case(case_id or active_case_id())
    if not case or case["is_default"]:
        ui = dict(case_defaults.GENERIC_UI)
        ui.update(case_private.ui())
        return ui
    ui = dict(case_defaults.GENERIC_UI)
    sugg = case.get("suggestions") or {}
    for key in ("synthesis_prompts", "advisor_prompts"):
        if isinstance(sugg.get(key), list) and sugg[key]:
            ui[key] = sugg[key]
    return ui


# --------------------------------------------------------------------------- #
# Participants (adresses, domaines, noms rattachés à un dossier)
# --------------------------------------------------------------------------- #
def normalize_participant(kind: str, value: str) -> str:
    v = (value or "").strip().lower()
    if kind == "domain":
        v = v.lstrip("@")
    if kind == "name":
        v = unicodedata.normalize("NFKD", v).encode("ascii", "ignore").decode()
        v = re.sub(r"[^a-z0-9]+", " ", v).strip()
    return v


def add_participant(
    case_id: str, kind: str, value: str, label: str = "", role: str = "", source: str = "manual"
) -> bool:
    if kind not in ("email", "domain", "name"):
        raise ValueError("Type de participant invalide.")
    norm = normalize_participant(kind, value)
    if not norm or (kind == "email" and "@" not in norm):
        raise ValueError("Valeur invalide.")
    with _conn() as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO case_participants
                 (case_id, kind, value, value_norm, label, role, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (case_id, kind, value.strip(), norm, label.strip(), role.strip(), source, time.time()),
        )
        return cur.rowcount == 1


def list_participants(case_id: str | None = None) -> list[dict]:
    q, params = "SELECT * FROM case_participants", ()
    if case_id:
        q += " WHERE case_id=?"
        params = (case_id,)
    q += " ORDER BY kind, value_norm"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def remove_participant(participant_id: int, case_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM case_participants WHERE id=? AND case_id=?", (participant_id, case_id))


def bump_participant(participant_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE case_participants SET hits=hits+1 WHERE id=?", (participant_id,))
