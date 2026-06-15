"""Données de synthèse pour le panneau Jalon 3 (/synthesis).

Toutes les fonctions ouvrent leur propre connexion courte à SQLite
(même pattern que state.py / graph_state.py) et retournent des
structures Python sérialisables sans appel LLM.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from config import DB_PATH

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YEAR_RE = re.compile(r"^\d{4}$")
_REPLY_PREFIX_RE = re.compile(r"^(RE|TR|FW|R[ée]p|Fwd)[\s:]+", re.IGNORECASE)

_KIND_LABELS = {
    "code_cadastre": "Cadastre",
    "code_iban": "Compte / IBAN",
    "code_fiscal": "Référence fiscale",
    "code_dossier": "Référence dossier",
    "code_autre": "Autre référence",
}
_KIND_ORDER = ["code_cadastre", "code_iban", "code_fiscal", "code_dossier", "code_autre"]


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


def get_chronologie() -> list[dict]:
    """Tous les événements datés extraits des documents, triés chronologiquement.

    Filtre les dates ISO (YYYY-MM-DD) et les années seules (YYYY).
    Ignore les chaînes floues non parseables.
    """
    events: list[dict] = []
    with _conn() as c:
        rows = c.execute(
            """
            SELECT e.doc_id, e.dates_json,
                   d.subject, d.type, d.date AS doc_date,
                   d.correspondent, d.affaire
            FROM doc_enrichment e
            JOIN documents d ON e.doc_id = d.doc_id
            WHERE e.dates_json IS NOT NULL AND e.dates_json != '[]'
            """
        ).fetchall()

    for row in rows:
        try:
            dates = json.loads(row["dates_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(dates, list):
            continue
        for item in dates:
            if not isinstance(item, dict):
                continue
            label = (item.get("label") or "").strip()
            date_val = str(item.get("date") or "").strip()
            if not date_val or not label:
                continue
            if _ISO_DATE_RE.match(date_val):
                sort_key = date_val
            elif _YEAR_RE.match(date_val):
                sort_key = f"{date_val}-00-00"
            else:
                continue
            events.append(
                {
                    "date": date_val,
                    "date_sort_key": sort_key,
                    "label": label,
                    "doc_id": row["doc_id"],
                    "doc_subject": row["subject"] or "",
                    "doc_type": row["type"] or "",
                    "doc_date": (row["doc_date"] or "")[:10],
                    "doc_correspondent": row["correspondent"] or "",
                    "doc_affaire": row["affaire"] or "autre",
                }
            )

    events.sort(key=lambda e: e["date_sort_key"])
    return events


def get_cartographie_parties() -> list[dict]:
    """Toutes les personnes identifiées dans les documents, triées par fréquence.

    Déduplique les variantes de casse d'un même nom.
    """
    party_index: dict[str, dict] = {}

    with _conn() as c:
        rows = c.execute(
            """
            SELECT e.doc_id, e.parties_json
            FROM doc_enrichment e
            WHERE e.parties_json IS NOT NULL AND e.parties_json != '[]'
            """
        ).fetchall()

    for row in rows:
        try:
            parties = json.loads(row["parties_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parties, list):
            continue
        for p in parties:
            if not isinstance(p, dict):
                continue
            nom = (p.get("nom") or "").strip()
            if not nom or len(nom) < 2:
                continue
            nom_key = nom.upper()
            if nom_key not in party_index:
                party_index[nom_key] = {
                    "nom": nom,
                    "mention_count": 0,
                    "doc_ids": set(),
                    "roles": set(),
                    "qualites": set(),
                }
            rec = party_index[nom_key]
            if row["doc_id"] not in rec["doc_ids"]:
                rec["mention_count"] += 1
                rec["doc_ids"].add(row["doc_id"])
            role = (p.get("role") or "").strip()
            qualite = (p.get("qualite") or "").strip()
            if role:
                rec["roles"].add(role)
            if qualite:
                rec["qualites"].add(qualite)

    result = [
        {
            "nom": rec["nom"],
            "mention_count": rec["mention_count"],
            "roles": sorted(rec["roles"]),
            "qualites": sorted(rec["qualites"]),
            "doc_ids": sorted(rec["doc_ids"]),
        }
        for rec in party_index.values()
    ]
    result.sort(key=lambda x: -x["mention_count"])
    return result


def get_resumes_par_fil() -> list[dict]:
    """Résumé par fil de discussion : stats + documents avec résumé LLM."""
    threads_out: list[dict] = []

    with _conn() as c:
        thread_rows = c.execute(
            """
            SELECT d.thread_id,
                   COUNT(CASE WHEN d.type='mail' THEN 1 END) AS n_mails,
                   COUNT(CASE WHEN d.type='attachment' THEN 1 END) AS n_attachments,
                   MIN(d.date) AS date_min,
                   MAX(d.date) AS date_max,
                   d.affaire
            FROM documents d
            WHERE d.thread_id IS NOT NULL
            GROUP BY d.thread_id
            ORDER BY MIN(d.date) ASC
            """
        ).fetchall()

        for tr in thread_rows:
            if tr["n_mails"] == 0:
                continue
            doc_rows = c.execute(
                """
                SELECT d.doc_id, d.type, d.date, d.subject, d.correspondent,
                       e.resume, e.intention, e.parties_json
                FROM documents d
                LEFT JOIN doc_enrichment e ON d.doc_id = e.doc_id
                WHERE d.thread_id = ?
                ORDER BY d.date ASC
                """,
                (tr["thread_id"],),
            ).fetchall()

            parties_seen: set[str] = set()
            docs_list: list[dict] = []
            sujet = ""
            for dr in doc_rows:
                if dr["type"] == "mail" and not sujet:
                    raw = dr["subject"] or ""
                    sujet = _REPLY_PREFIX_RE.sub("", raw).strip()
                docs_list.append(
                    {
                        "doc_id": dr["doc_id"],
                        "type": dr["type"],
                        "date": (dr["date"] or "")[:10],
                        "subject": dr["subject"] or "",
                        "correspondent": dr["correspondent"] or "",
                        "resume": dr["resume"] or "",
                        "intention": dr["intention"] or "",
                    }
                )
                try:
                    ps = json.loads(dr["parties_json"] or "[]")
                    for p in ps:
                        nom = (p.get("nom") or "").strip()
                        if nom:
                            parties_seen.add(nom)
                except (json.JSONDecodeError, TypeError):
                    pass

            threads_out.append(
                {
                    "thread_id": tr["thread_id"],
                    "sujet": sujet or f"Fil {str(tr['thread_id'])[:8]}",
                    "n_mails": tr["n_mails"],
                    "n_attachments": tr["n_attachments"],
                    "date_min": (tr["date_min"] or "")[:10],
                    "date_max": (tr["date_max"] or "")[:10],
                    "affaire": tr["affaire"] or "autre",
                    "parties_uniques": sorted(parties_seen),
                    "docs": docs_list,
                }
            )

    return threads_out


def get_points_contentieux() -> list[dict]:
    """Toutes les déclarations extraites, groupées par auteur."""
    author_index: dict[str, dict] = {}

    with _conn() as c:
        rows = c.execute(
            """
            SELECT e.doc_id, e.declarations_json,
                   d.subject, d.date
            FROM doc_enrichment e
            JOIN documents d ON e.doc_id = d.doc_id
            WHERE e.declarations_json IS NOT NULL
              AND e.declarations_json != '[]'
            """
        ).fetchall()

    for row in rows:
        try:
            decls = json.loads(row["declarations_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(decls, list):
            continue
        for decl in decls:
            if not isinstance(decl, dict):
                continue
            auteur = (decl.get("auteur") or "").strip()
            contenu = (decl.get("contenu") or "").strip()
            if not auteur or not contenu:
                continue
            auteur_key = auteur.upper()
            if auteur_key not in author_index:
                author_index[auteur_key] = {
                    "auteur": auteur,
                    "n_declarations": 0,
                    "doc_ids": set(),
                    "declarations": [],
                }
            rec = author_index[auteur_key]
            rec["n_declarations"] += 1
            rec["doc_ids"].add(row["doc_id"])
            rec["declarations"].append(
                {
                    "contenu": contenu,
                    "doc_id": row["doc_id"],
                    "doc_subject": row["subject"] or "",
                    "doc_date": (row["date"] or "")[:10],
                }
            )

    result = []
    for rec in author_index.values():
        sorted_decls = sorted(rec["declarations"], key=lambda x: x["doc_date"])
        result.append(
            {
                "auteur": rec["auteur"],
                "n_declarations": rec["n_declarations"],
                "docs_count": len(rec["doc_ids"]),
                "declarations": sorted_decls,
            }
        )
    result.sort(key=lambda x: -x["n_declarations"])
    return result


def get_index_codes() -> list[dict]:
    """Toutes les références structurées, groupées par type.

    Utilise doc_entities (déjà normalisé) plutôt que codes_json
    pour éviter les variantes linguistiques du LLM (fiscali/fiscaux…).
    """
    kind_map: dict[str, dict[str, list]] = {k: {} for k in _KIND_ORDER}

    with _conn() as c:
        rows = c.execute(
            """
            SELECT de.kind, de.value, de.doc_id,
                   d.subject, d.date, d.type AS doc_type, d.affaire
            FROM doc_entities de
            JOIN documents d ON de.doc_id = d.doc_id
            WHERE de.kind != 'entity'
            ORDER BY de.kind, de.value, d.date ASC
            """
        ).fetchall()

    for row in rows:
        kind = row["kind"]
        if kind not in kind_map:
            kind_map[kind] = {}
        val = row["value"]
        if val not in kind_map[kind]:
            kind_map[kind][val] = []
        existing_ids = {d["doc_id"] for d in kind_map[kind][val]}
        if row["doc_id"] not in existing_ids:
            kind_map[kind][val].append(
                {
                    "doc_id": row["doc_id"],
                    "subject": row["subject"] or "",
                    "date": (row["date"] or "")[:10],
                    "doc_type": row["doc_type"] or "",
                    "affaire": row["affaire"] or "autre",
                }
            )

    result = []
    for kind in _KIND_ORDER:
        codes_for_kind = [
            {"value": val, "n_docs": len(docs), "docs": docs}
            for val, docs in sorted(
                kind_map.get(kind, {}).items(), key=lambda x: -len(x[1])
            )
        ]
        if codes_for_kind:
            result.append(
                {
                    "kind": kind,
                    "label": _KIND_LABELS.get(kind, kind),
                    "codes": codes_for_kind,
                }
            )
    return result


def get_all_synthesis_data() -> dict:
    """Appelle les 5 fonctions et retourne un dict prêt pour le template."""
    return {
        "chronologie": get_chronologie(),
        "parties": get_cartographie_parties(),
        "fils": get_resumes_par_fil(),
        "contentieux": get_points_contentieux(),
        "codes": get_index_codes(),
    }
