"""État SQLite pour le panel d'évaluation RAG.

Tables :
- eval_gen_progress : progression de la génération (singleton id=1)
- eval_gen_log      : journal de génération
- eval_questions    : golden dataset (questions + réponses de référence)
- eval_runs         : runs d'évaluation (un run = une session d'évaluation complète)
- eval_results      : résultats par question pour chaque run
"""
from __future__ import annotations

import json
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


def init_eval_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS eval_gen_progress (
                id       INTEGER PRIMARY KEY CHECK (id = 1),
                running  INTEGER DEFAULT 0,
                current  INTEGER DEFAULT 0,
                total    INTEGER DEFAULT 0,
                phase    TEXT    DEFAULT 'inactif',
                updated_at REAL
            );

            CREATE TABLE IF NOT EXISTS eval_gen_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      REAL,
                level   TEXT,
                message TEXT
            );

            CREATE TABLE IF NOT EXISTS eval_questions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                question     TEXT    NOT NULL,
                expected     TEXT    NOT NULL,
                category     TEXT    NOT NULL,
                difficulty   TEXT    NOT NULL,
                notes        TEXT,
                source_chunk TEXT,
                created_at   REAL    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS eval_runs (
                run_id      TEXT    PRIMARY KEY,
                started_at  REAL    NOT NULL,
                finished_at REAL,
                provider    TEXT    NOT NULL,
                model       TEXT    NOT NULL,
                total       INTEGER DEFAULT 0,
                current     INTEGER DEFAULT 0,
                phase       TEXT    DEFAULT 'pending',
                running     INTEGER DEFAULT 0,
                cancelled   INTEGER DEFAULT 0,
                errors      INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS eval_results (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id       TEXT    NOT NULL,
                question_id  INTEGER NOT NULL,
                actual       TEXT,
                sources      TEXT,
                score        REAL,
                reasoning    TEXT,
                verdict      TEXT,
                faithfulness REAL,
                relevance    REAL,
                completeness REAL,
                evaluated_at REAL
            );
            """
        )
        c.execute("INSERT OR IGNORE INTO eval_gen_progress (id, running) VALUES (1, 0)")
        # Réinitialise running=0 au démarrage (évite un état bloqué après un crash)
        c.execute("UPDATE eval_gen_progress SET running=0 WHERE id=1 AND running=1")
        # Migration : ajoute la colonne sources si absente (DB créée avant cette version)
        cols = {row[1] for row in c.execute("PRAGMA table_info(eval_results)").fetchall()}
        if "sources" not in cols:
            c.execute("ALTER TABLE eval_results ADD COLUMN sources TEXT")


# --------------------------------------------------------------------------- #
# Progression génération (singleton)
# --------------------------------------------------------------------------- #
def start_gen_progress(total: int) -> None:
    with _conn() as c:
        c.execute(
            """UPDATE eval_gen_progress
               SET running=1, current=0, total=?, phase='démarrage', updated_at=?
               WHERE id=1""",
            (total, time.time()),
        )
        c.execute("DELETE FROM eval_gen_log")


def update_gen_progress(
    current: int | None = None,
    phase: str | None = None,
) -> None:
    sets, params = [], []
    if current is not None:
        sets.append("current=?"); params.append(current)
    if phase is not None:
        sets.append("phase=?"); params.append(phase)
    sets.append("updated_at=?"); params.append(time.time())
    with _conn() as c:
        c.execute(f"UPDATE eval_gen_progress SET {', '.join(sets)} WHERE id=1", params)


def finish_gen_progress(phase: str = "terminé") -> None:
    with _conn() as c:
        c.execute(
            "UPDATE eval_gen_progress SET running=0, phase=?, updated_at=? WHERE id=1",
            (phase, time.time()),
        )


def is_gen_running() -> bool:
    with _conn() as c:
        row = c.execute("SELECT running FROM eval_gen_progress WHERE id=1").fetchone()
        return bool(row and row["running"])


def get_gen_status() -> dict:
    with _conn() as c:
        row = c.execute("SELECT * FROM eval_gen_progress WHERE id=1").fetchone()
        logs = c.execute(
            "SELECT ts, level, message FROM eval_gen_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
    status = dict(row) if row else {}
    status["log"] = [dict(r) for r in reversed(logs)]
    return status


def gen_log(message: str, level: str = "info") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO eval_gen_log (ts, level, message) VALUES (?, ?, ?)",
            (time.time(), level, message),
        )


# --------------------------------------------------------------------------- #
# Questions CRUD
# --------------------------------------------------------------------------- #
def insert_question(q: dict) -> int:
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO eval_questions
               (question, expected, category, difficulty, notes, source_chunk, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                q["question"],
                q["expected"],
                q.get("category", "factual"),
                q.get("difficulty", "medium"),
                q.get("notes"),
                q.get("source_chunk"),
                time.time(),
            ),
        )
        return cur.lastrowid


def update_question(q_id: int, fields: dict) -> None:
    allowed = {"question", "expected", "category", "difficulty", "notes"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?"); params.append(v)
    if not sets:
        return
    params.append(q_id)
    with _conn() as c:
        c.execute(f"UPDATE eval_questions SET {', '.join(sets)} WHERE id=?", params)


def delete_question(q_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM eval_questions WHERE id=?", (q_id,))


def get_question(q_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM eval_questions WHERE id=?", (q_id,)).fetchone()
        return dict(row) if row else None


def list_questions(
    category: str | None = None,
    difficulty: str | None = None,
) -> list[dict]:
    q = "SELECT * FROM eval_questions WHERE 1=1"
    params: list = []
    if category:
        q += " AND category=?"; params.append(category)
    if difficulty:
        q += " AND difficulty=?"; params.append(difficulty)
    q += " ORDER BY id ASC"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def clear_questions() -> int:
    with _conn() as c:
        cur = c.execute("DELETE FROM eval_questions")
        return cur.rowcount


def export_questions() -> list[dict]:
    return list_questions()


def import_questions(items: list[dict]) -> dict:
    imported, skipped = 0, 0
    for item in items:
        if not item.get("question") or not item.get("expected"):
            skipped += 1
            continue
        try:
            insert_question(item)
            imported += 1
        except Exception:
            skipped += 1
    return {"imported": imported, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
def create_run(run_id: str, provider: str, model: str, total: int) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO eval_runs
               (run_id, started_at, provider, model, total, current, phase, running)
               VALUES (?, ?, ?, ?, ?, 0, 'démarrage', 1)""",
            (run_id, time.time(), provider, model, total),
        )


def update_run(run_id: str, **fields) -> None:
    allowed = {"current", "phase", "errors", "running", "cancelled", "finished_at"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?"); params.append(v)
    if not sets:
        return
    params.append(run_id)
    with _conn() as c:
        c.execute(f"UPDATE eval_runs SET {', '.join(sets)} WHERE run_id=?", params)


def finish_run(run_id: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE eval_runs SET running=0, phase='terminé', finished_at=? WHERE run_id=?",
            (time.time(), run_id),
        )


def cancel_run_db(run_id: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE eval_runs SET cancelled=1, running=0, phase='annulé', finished_at=? WHERE run_id=?",
            (time.time(), run_id),
        )


def get_run(run_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM eval_runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None


def list_runs() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM eval_runs ORDER BY started_at DESC"
        ).fetchall()]


# --------------------------------------------------------------------------- #
# Résultats
# --------------------------------------------------------------------------- #
def insert_result(run_id: str, question_id: int, result: dict) -> None:
    sources = result.get("sources")
    if isinstance(sources, (list, dict)):
        sources = json.dumps(sources, ensure_ascii=False)
    with _conn() as c:
        c.execute(
            """INSERT INTO eval_results
               (run_id, question_id, actual, sources, score, reasoning, verdict,
                faithfulness, relevance, completeness, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                question_id,
                result.get("actual"),
                sources,
                result.get("score"),
                result.get("reasoning"),
                result.get("verdict"),
                result.get("faithfulness"),
                result.get("relevance"),
                result.get("completeness"),
                time.time(),
            ),
        )


def list_results(
    run_id: str,
    category: str | None = None,
    verdict: str | None = None,
    difficulty: str | None = None,
) -> list[dict]:
    q = """
        SELECT r.*, q.question, q.expected, q.category, q.difficulty, q.notes
        FROM eval_results r
        JOIN eval_questions q ON q.id = r.question_id
        WHERE r.run_id=?
    """
    params: list = [run_id]
    if category:
        q += " AND q.category=?"; params.append(category)
    if verdict:
        q += " AND r.verdict=?"; params.append(verdict)
    if difficulty:
        q += " AND q.difficulty=?"; params.append(difficulty)
    q += " ORDER BY r.id ASC"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def get_summary(run_id: str) -> dict:
    with _conn() as c:
        rows = c.execute(
            "SELECT score, verdict FROM eval_results WHERE run_id=?", (run_id,)
        ).fetchall()
    if not rows:
        return {"total": 0, "avg_score": 0, "pass_rate": 0, "distribution": [0]*5}
    scores = [r["score"] for r in rows if r["score"] is not None]
    verdicts = [r["verdict"] for r in rows]
    total = len(rows)
    avg = sum(scores) / len(scores) if scores else 0
    passes = sum(1 for v in verdicts if v == "pass")
    buckets = [0] * 5
    for s in scores:
        idx = min(int(s / 2), 4)
        buckets[idx] += 1
    return {
        "total": total,
        "avg_score": round(avg, 2),
        "pass_rate": round(passes / total * 100, 1) if total else 0,
        "distribution": buckets,
    }


# --------------------------------------------------------------------------- #
# Rapport (génération en mémoire)
# --------------------------------------------------------------------------- #
def build_markdown_report(run_id: str) -> str:
    run = get_run(run_id)
    if not run:
        return "# Rapport introuvable\n"
    summary = get_summary(run_id)
    results = list_results(run_id)

    lines = [
        f"# Rapport d'évaluation RAG",
        f"",
        f"**Run ID** : `{run_id}`  ",
        f"**Provider** : {run['provider']} — **Modèle** : {run['model']}  ",
        f"**Date** : {_fmt_ts(run['started_at'])}",
        f"",
        f"## Résumé",
        f"",
        f"| Métrique | Valeur |",
        f"|---|---|",
        f"| Questions évaluées | {summary['total']} |",
        f"| Score moyen | {summary['avg_score']} / 10 |",
        f"| Taux de réussite | {summary['pass_rate']} % |",
        f"",
        f"## Résultats détaillés",
        f"",
        f"| # | Question | Score | Verdict | Fidélité | Pertinence | Complétude |",
        f"|---|---|---|---|---|---|---|",
    ]
    for r in results:
        verdict_md = "✅ pass" if r["verdict"] == "pass" else "❌ fail"
        lines.append(
            f"| {r['question_id']} "
            f"| {_md_escape(r['question'][:80])} "
            f"| {r['score'] or '—'} "
            f"| {verdict_md} "
            f"| {r['faithfulness'] or '—'} "
            f"| {r['relevance'] or '—'} "
            f"| {r['completeness'] or '—'} |"
        )
    lines += ["", "## Raisonnements", ""]
    for r in results:
        lines += [
            f"### Q{r['question_id']} — {r['question'][:100]}",
            f"**Attendu** : {r['expected'][:300]}",
            f"",
            f"**Obtenu** : {(r['actual'] or '')[:300]}",
            f"",
            f"**Raisonnement juge** : {r['reasoning'] or '—'}",
            f"",
        ]
    return "\n".join(lines)


def build_html_report(run_id: str) -> str:
    run = get_run(run_id)
    if not run:
        return "<p>Rapport introuvable.</p>"
    summary = get_summary(run_id)
    results = list_results(run_id)

    rows_html = ""
    for r in results:
        verdict_cls = "ok" if r["verdict"] == "pass" else "ko"
        verdict_lbl = "pass" if r["verdict"] == "pass" else "fail"
        rows_html += (
            f"<tr>"
            f"<td>{r['question_id']}</td>"
            f"<td>{_h(r['question'])}</td>"
            f"<td>{_h(r['expected'][:200])}</td>"
            f"<td>{_h((r['actual'] or '')[:200])}</td>"
            f"<td>{r['score'] or '—'}</td>"
            f"<td class='{verdict_cls}'>{verdict_lbl}</td>"
            f"<td><small>{_h(r['reasoning'] or '')}</small></td>"
            f"</tr>\n"
        )

    return f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<title>Rapport RAG — {run_id}</title>
<style>
  body{{font-family:system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;}}
  h1{{color:#1f2933;}} table{{border-collapse:collapse;width:100%;font-size:.85rem;}}
  th,td{{border:1px solid #e3e1dc;padding:.4rem .6rem;text-align:left;vertical-align:top;}}
  th{{background:#f0efea;}} .ok{{color:#2e7d32;font-weight:bold;}} .ko{{color:#c62828;font-weight:bold;}}
  .cards{{display:flex;gap:1rem;margin:1rem 0;}}
  .card{{background:#f7f6f3;border:1px solid #e3e1dc;border-radius:8px;padding:1rem 1.5rem;text-align:center;}}
  .card strong{{display:block;font-size:1.6rem;color:#2c7fb8;}}
</style></head><body>
<h1>Rapport d'évaluation RAG</h1>
<p><strong>Run</strong> : {run_id} &nbsp;|&nbsp;
   <strong>Provider</strong> : {_h(run['provider'])} &nbsp;|&nbsp;
   <strong>Modèle</strong> : {_h(run['model'])} &nbsp;|&nbsp;
   <strong>Date</strong> : {_fmt_ts(run['started_at'])}</p>
<div class="cards">
  <div class="card"><strong>{summary['total']}</strong>Questions</div>
  <div class="card"><strong>{summary['avg_score']}/10</strong>Score moyen</div>
  <div class="card"><strong>{summary['pass_rate']} %</strong>Taux de réussite</div>
</div>
<table>
<thead><tr><th>#</th><th>Question</th><th>Attendu</th><th>Obtenu</th>
<th>Score</th><th>Verdict</th><th>Raisonnement</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
</body></html>"""


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def _h(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
