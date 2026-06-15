"""Graphe de connaissance en SQLite (data/app.sqlite).

Trois tables, alimentées à l'indexation par l'enrichissement LLM (parsing/enrich.py)
et par les liens déterministes (threading, codes partagés) :

- `doc_enrichment` : 1 ligne par document — intention, objectif, résumé, type, faits.
- `doc_entities`   : entités et codes extraits (requêtables : "quels docs citent FR0062729 ?").
- `doc_edges`      : arêtes typées du graphe (reply_to, attached_to, shares_code…).

Même base et même pattern de connexion que `state.py`. Zéro dépendance externe.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
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


def init_graph_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS doc_enrichment (
                doc_id            TEXT PRIMARY KEY,
                intention         TEXT,
                objectif          TEXT,
                resume            TEXT,
                doc_type          TEXT,             -- pour les PJ (acte, déclaration, planimétrie…)
                facts_json        TEXT,             -- liste JSON des faits marquants
                parties_json      TEXT,             -- liste JSON [{role, nom, qualite}]
                dates_json        TEXT,             -- liste JSON [{label, date}]
                declarations_json TEXT,             -- liste JSON [{auteur, contenu}]
                codes_json        TEXT,             -- dict JSON {cadastre, comptes_iban, fiscaux, dossiers, autres}
                enriched_at       REAL
            );

            CREATE TABLE IF NOT EXISTS doc_entities (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id     TEXT NOT NULL,
                kind       TEXT NOT NULL,   -- 'entity' | 'code_cadastre' | 'code_iban' |
                                            -- 'code_fiscal' | 'code_dossier' | 'code_autre'
                value      TEXT NOT NULL,
                value_norm TEXT NOT NULL    -- normalisé (upper, alphanum) pour le matching
            );

            CREATE TABLE IF NOT EXISTS doc_edges (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                src    TEXT NOT NULL,
                dst    TEXT NOT NULL,
                rel    TEXT NOT NULL,       -- reply_to | same_thread | attached_to |
                                            -- principal | annexe_of | accompanies | shares_code
                weight REAL DEFAULT 1.0,
                source TEXT,               -- header | llm | code_match
                UNIQUE(src, dst, rel)
            );

            CREATE INDEX IF NOT EXISTS idx_entities_doc  ON doc_entities(doc_id);
            CREATE INDEX IF NOT EXISTS idx_entities_norm ON doc_entities(value_norm);
            CREATE INDEX IF NOT EXISTS idx_edges_src     ON doc_edges(src);
            CREATE INDEX IF NOT EXISTS idx_edges_dst     ON doc_edges(dst);
            """
        )
        # Migration : ajoute les nouvelles colonnes JSON sur une table préexistante.
        cols = {row[1] for row in c.execute("PRAGMA table_info(doc_enrichment)").fetchall()}
        for col in ("parties_json", "dates_json", "declarations_json", "codes_json"):
            if col not in cols:
                c.execute(f"ALTER TABLE doc_enrichment ADD COLUMN {col} TEXT")

        # Migration : dédupliquer doc_edges si la table existait sans contrainte UNIQUE.
        # On supprime les doublons (même src/dst/rel, on garde l'id le plus petit).
        c.execute("""
            DELETE FROM doc_edges
            WHERE id NOT IN (
                SELECT MIN(id) FROM doc_edges GROUP BY src, dst, rel
            )
        """)


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
def normalize_code(value: str) -> str:
    """Normalise un code/entité pour le matching : majuscules, alphanumérique seul."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


# --------------------------------------------------------------------------- #
# Enrichissement
# --------------------------------------------------------------------------- #
def _to_json(v) -> str:
    if v is None:
        return "[]"
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def upsert_enrichment(doc_id: str, enr: dict) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO doc_enrichment
                 (doc_id, intention, objectif, resume, doc_type,
                  facts_json, parties_json, dates_json, declarations_json, codes_json,
                  enriched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(doc_id) DO UPDATE SET
                 intention=excluded.intention, objectif=excluded.objectif,
                 resume=excluded.resume, doc_type=excluded.doc_type,
                 facts_json=excluded.facts_json, parties_json=excluded.parties_json,
                 dates_json=excluded.dates_json, declarations_json=excluded.declarations_json,
                 codes_json=excluded.codes_json, enriched_at=excluded.enriched_at""",
            (
                doc_id,
                enr.get("intention"),
                enr.get("objectif"),
                enr.get("resume") or enr.get("description"),
                enr.get("doc_type"),
                _to_json(enr.get("faits") or enr.get("facts")),
                _to_json(enr.get("parties")),
                _to_json(enr.get("dates_evenements")),
                _to_json(enr.get("declarations")),
                _to_json(enr.get("codes")) if isinstance(enr.get("codes"), dict) else "{}",
                time.time(),
            ),
        )


def get_enrichment(doc_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM doc_enrichment WHERE doc_id=?", (doc_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    for field, key, default in (
        ("facts_json",        "facts",            "[]"),
        ("parties_json",      "parties",          "[]"),
        ("dates_json",        "dates_evenements", "[]"),
        ("declarations_json", "declarations",     "[]"),
        ("codes_json",        "codes",            "{}"),
    ):
        try:
            d[key] = json.loads(d.get(field) or default)
        except (json.JSONDecodeError, TypeError):
            d[key] = [] if default == "[]" else {}
    return d


# --------------------------------------------------------------------------- #
# Entités / codes
# --------------------------------------------------------------------------- #
_CODE_KIND_MAP = {
    "cadastre":     "code_cadastre",
    "comptes_iban": "code_iban",
    "fiscaux":      "code_fiscal",
    "dossiers":     "code_dossier",
    "autres":       "code_autre",
}


def add_entities(doc_id: str, entities: list[str], codes) -> None:
    """Persiste entités et codes dans doc_entities.

    `codes` peut être :
    - une liste de str (ancien format, tout en 'code_autre')
    - un dict {cadastre, comptes_iban, fiscaux, dossiers, autres} → nouveau format typé
    """
    rows: list[tuple] = []
    for v in entities or []:
        if isinstance(v, dict):
            v = " ".join(str(x) for x in v.values() if x)
        v = (v or "").strip()
        if v:
            rows.append((doc_id, "entity", v, normalize_code(v)))

    if isinstance(codes, dict):
        for sub_key, kind in _CODE_KIND_MAP.items():
            for v in codes.get(sub_key) or []:
                if isinstance(v, dict):
                    v = " ".join(str(x) for x in v.values() if x)
                v = (v or "").strip()
                if v:
                    rows.append((doc_id, kind, v, normalize_code(v)))
    else:
        for v in codes or []:
            if isinstance(v, dict):
                v = " ".join(str(x) for x in v.values() if x)
            v = (v or "").strip()
            if v:
                rows.append((doc_id, "code_autre", v, normalize_code(v)))

    if not rows:
        return
    with _conn() as c:
        c.executemany(
            "INSERT INTO doc_entities (doc_id, kind, value, value_norm) VALUES (?, ?, ?, ?)",
            rows,
        )


def docs_by_code(value: str) -> list[str]:
    """doc_ids citant un code/entité (par valeur normalisée)."""
    norm = normalize_code(value)
    if not norm:
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT doc_id FROM doc_entities WHERE value_norm=?", (norm,)
        ).fetchall()
    return [r["doc_id"] for r in rows]


def build_shared_code_edges(min_len: int = 4) -> int:
    """Crée les arêtes 'shares_code' entre docs partageant un même code (déterministe).

    Appelé une fois en fin d'indexation. Retourne le nombre d'arêtes créées.
    On ignore les codes trop courts (bruit) et on ne relie que les codes (pas les
    entités génériques comme des noms, trop fréquents).
    """
    with _conn() as c:
        groups = c.execute(
            """SELECT value_norm, GROUP_CONCAT(DISTINCT doc_id) AS docs
               FROM doc_entities
               WHERE kind LIKE 'code%' AND length(value_norm) >= ?
               GROUP BY value_norm
               HAVING COUNT(DISTINCT doc_id) >= 2""",
            (min_len,),
        ).fetchall()

        n = 0
        for g in groups:
            doc_ids = sorted(set(g["docs"].split(",")))
            for i in range(len(doc_ids)):
                for j in range(i + 1, len(doc_ids)):
                    c.execute(
                        """INSERT OR IGNORE INTO doc_edges (src, dst, rel, weight, source)
                           VALUES (?, ?, 'shares_code', 1.0, 'code_match')""",
                        (doc_ids[i], doc_ids[j]),
                    )
                    n += 1
    return n


# --------------------------------------------------------------------------- #
# Arêtes
# --------------------------------------------------------------------------- #
def add_edge(src: str, dst: str, rel: str, source: str = "llm", weight: float = 1.0) -> None:
    if not src or not dst or src == dst:
        return
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO doc_edges (src, dst, rel, weight, source) VALUES (?, ?, ?, ?, ?)",
            (src, dst, rel, weight, source),
        )


def neighbors(doc_id: str, rels: list[str] | None = None) -> list[dict]:
    """Voisins du document dans le graphe (arêtes dans les deux sens).

    Retourne [{doc_id, rel, source, direction}] dédupliqué par (doc_id, rel).
    """
    with _conn() as c:
        out_rows = c.execute(
            "SELECT dst AS other, rel, source FROM doc_edges WHERE src=?", (doc_id,)
        ).fetchall()
        in_rows = c.execute(
            "SELECT src AS other, rel, source FROM doc_edges WHERE dst=?", (doc_id,)
        ).fetchall()

    seen: set[tuple] = set()
    result: list[dict] = []
    for row, direction in [(r, "out") for r in out_rows] + [(r, "in") for r in in_rows]:
        if rels and row["rel"] not in rels:
            continue
        key = (row["other"], row["rel"])
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {"doc_id": row["other"], "rel": row["rel"], "source": row["source"],
             "direction": direction}
        )
    return result


# --------------------------------------------------------------------------- #
# Réinitialisation
# --------------------------------------------------------------------------- #
def clear_graph() -> None:
    """Vide les trois tables du graphe (appelé par reset_index)."""
    with _conn() as c:
        c.execute("DELETE FROM doc_enrichment")
        c.execute("DELETE FROM doc_entities")
        c.execute("DELETE FROM doc_edges")
