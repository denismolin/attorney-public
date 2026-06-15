"""Plan de recherche déterministe à partir de l'analyse d'intention.

Convertit les sous_questions en paramètres concrets pour search() :
query_text optimisé, filtre affaire, top_k adapté, ordre topologique.
Aucun appel LLM — transformation pure.
"""
from __future__ import annotations

_EXHAUSTIVE_INTENTIONS = {"exhaustive", "comparative"}
_TOP_K_DEFAULT = 6
_TOP_K_EXHAUSTIVE = 12


def build_query_plan(analysis: dict) -> list[dict]:
    """Retourne une liste ordonnée de requêtes prêtes à exécuter.

    Chaque élément : {query_id, query_text, affaire, depends_on, top_k}.
    L'ordre respecte les dépendances (topological sort simple).
    """
    if not analysis:
        return []

    intention = analysis.get("intention", "informative")
    affaire = analysis.get("affaire_probable") or None
    if affaire == "null":
        affaire = None
    entites = analysis.get("entites_cles") or []
    sous_questions = analysis.get("sous_questions") or []

    # Fallback : pas de sous-questions → plan à 1 entrée
    if not sous_questions:
        return []

    top_k = _TOP_K_EXHAUSTIVE if intention in _EXHAUSTIVE_INTENTIONS else _TOP_K_DEFAULT

    # Enrichit le texte de chaque sous-question avec les entités clés si pertinentes.
    entites_suffix = (" " + " ".join(entites)) if entites else ""

    plan: list[dict] = []
    for sq in sous_questions:
        q_id = sq.get("id", f"q{len(plan)+1}")
        q_text = (sq.get("texte") or "").strip()
        if not q_text:
            continue
        # Injecte les entités dans le texte de recherche si elles n'y sont pas déjà.
        for ent in entites:
            if ent.lower() not in q_text.lower():
                q_text = f"{q_text} {ent}"
        plan.append({
            "query_id":   q_id,
            "query_text": q_text.strip(),
            "affaire":    affaire,
            "depends_on": sq.get("depends_on") or [],
            "top_k":      top_k,
        })

    return _topological_sort(plan)


def _topological_sort(plan: list[dict]) -> list[dict]:
    """Trie le plan pour respecter les dépendances (depends_on)."""
    index = {p["query_id"]: p for p in plan}
    visited: set[str] = set()
    result: list[dict] = []

    def visit(qid: str) -> None:
        if qid in visited:
            return
        visited.add(qid)
        for dep in index.get(qid, {}).get("depends_on", []):
            if dep in index:
                visit(dep)
        if qid in index:
            result.append(index[qid])

    for p in plan:
        visit(p["query_id"])

    return result
