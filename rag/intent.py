"""Analyse d'intention d'une question utilisateur.

Appel LLM léger pour décomposer une question en sous-questions indépendantes,
identifier les entités clés, l'affaire probable et les faits déjà établis en session.
"""
from __future__ import annotations

from parsing.jsonparse import parse_json_response as _parse_json

_SYSTEM = """Tu es un assistant juridique qui analyse une question sur un dossier de succession
(affaire DUPONT / MARTIN / DURAND).

À partir de la question et de l'historique de la conversation, retourne UNIQUEMENT un objet JSON :
{
  "intention": "informative|comparative|temporelle|exhaustive|résumé",
  "sous_questions": [
    {"id": "q1", "texte": "...", "depends_on": []}
  ],
  "entites_cles": ["nom, montant, date, code…"],
  "affaire_probable": "succession|dette|pro|autre|null",
  "contexte_acquis": ["fait déjà établi dans cette session"]
}

Règles :
- Si la question est simple et directe : sous_questions = 1 élément, depends_on = [].
- Si la question contient plusieurs demandes distinctes : décompose en sous-questions numérotées q1, q2…
  avec depends_on si une sous-question nécessite les résultats d'une précédente.
- contexte_acquis : liste les faits importants déjà affirmés par l'assistant dans l'historique récent.
  Si rien de pertinent, retourne [].
- entites_cles : noms propres, montants (ex. "98500 €"), dates-clés, identifiants cités dans la question.
- affaire_probable : "null" si impossible à déterminer.
- Sois concis. N'invente rien.
"""


def analyse_question(
    question: str,
    history: list[dict],
    chat,
) -> dict | None:
    """Retourne {intention, sous_questions, entites_cles, affaire_probable, contexte_acquis} ou None."""
    # Résumé de l'historique récent (6 derniers messages assistant seulement)
    recent_facts: list[str] = []
    for msg in history[-12:]:
        if msg.get("role") == "assistant" and msg.get("content"):
            recent_facts.append(msg["content"][:300])

    history_block = ""
    if recent_facts:
        history_block = "Historique récent (réponses assistant) :\n" + "\n---\n".join(recent_facts)

    user_content = f"{history_block}\n\nQuestion : {question}".strip()

    try:
        resp = chat.complete(
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
            tools=None,
        )
        text = resp.get("text", "")
        return _parse_json(text)
    except Exception:  # noqa: BLE001
        return None
