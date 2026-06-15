"""Chatbot synthèse — construit un narratif juridique sourcé sur l'historique complet.

Différence vs rag/chat.py :
  - Prompt système orienté narratif (pas Q&A ponctuelle)
  - Briefing SQL compact injecté avant les chunks Chroma : chronologie filtrée,
    résumés de fils, déclarations des parties
  - top_k plus élevé (25 par défaut) pour couvrir plus de documents pertinents
  - expand_extra contrôle le nombre de voisins de graphe ajoutés (6 par défaut)
"""
from __future__ import annotations

import re

from rag.chat import (
    _format_session_ctx,
    _history_messages,
    _strip_thinking,
    _update_session_ctx,
)
from rag.providers import Chat, get_chat
from rag.retrieve import Hit, format_context, search

SYNTHESIS_SYSTEM_PROMPT = """Tu es un juriste assistant qui analyse la correspondance \
complète d'une succession franco-italienne (DUPONT / MARTIN / DURAND).

Ta mission est de construire des NARRATIFS SOURCÉS : synthèses, chronologies, \
analyses contradictoires, points de contentieux — à partir des documents fournis.

Règles impératives :
- Réponds en FRANÇAIS, style juridique professionnel.
- Cite TOUTES tes sources entre crochets [date — correspondant — sujet] \
  immédiatement après chaque affirmation factuelle.
- Structure ta réponse avec des titres markdown (##) et des listes quand utile.
- Si des informations sont manquantes ou contradictoires entre documents, \
  signale-le explicitement.
- Ne fabrique rien : appuie-toi uniquement sur le contexte fourni.
- N'écris pas de préambule ni de formule de politesse : commence directement \
  par le contenu.
"""

_KEYWORDS_RE = re.compile(r"\b\w{4,}\b", re.IGNORECASE)

# Mots de remplissage français à retirer avant la recherche vectorielle.
_STOPWORDS = {
    "fais", "faire", "moi", "une", "synthese", "synthèse", "tous", "toutes",
    "pour", "dans", "avec", "sans", "sur", "sous", "entre", "vers", "depuis",
    "cette", "cette", "quel", "quelle", "quels", "quelles", "comment", "quand",
    "pourquoi", "parce", "afin", "donc", "mais", "ainsi", "aussi", "bien",
    "avoir", "être", "faire", "aller", "venir", "tout", "plus", "moins",
    "peut", "dois", "faut", "liste", "lister", "résume", "résumer", "donne",
    "donner", "montre", "montrer", "explique", "expliquer", "décris", "décrire",
    "identifie", "identifier", "récapitule", "récapituler", "quoi", "qui",
    "sont", "était", "avait", "avez", "avons", "avaient", "comme", "très",
    "trop", "assez", "seul", "seule", "même", "autre", "autres", "each",
}


def _keywords(text: str) -> set[str]:
    return {m.lower() for m in _KEYWORDS_RE.findall(text)}


def _rewrite_query(question: str) -> str:
    """Extrait les termes substantiels de la question pour une requête vectorielle dense.

    Retire les mots de remplissage (verbes d'instruction, stopwords) pour que
    l'embedding matche sur le contenu des documents et non sur la forme de la question.
    Ex: "Fais moi une synthèse des estimations de la maison d'Italie"
     -> "estimations maison Italie"
    """
    # Garde uniquement les mots de 4+ caractères hors stopwords
    words = re.findall(r"\b\w{4,}\b", question, re.IGNORECASE)
    filtered = [w for w in words if w.lower() not in _STOPWORDS]
    if not filtered or len(filtered) < 2:
        return question
    return " ".join(filtered)


def _build_sql_briefing(question: str) -> str:
    """Construit un briefing compact (≤ 4000 chars) depuis les données SQLite enrichies.

    Inclut :
    - Stats globales du dossier
    - Chronologie filtrée (événements dont le label contient des mots de la question)
    - Résumés des fils de discussion (sujet + intention + résumés tronqués)
    - Déclarations des parties (top 12 par fréquence)
    """
    from viz.synthesis_data import (
        get_chronologie,
        get_points_contentieux,
        get_resumes_par_fil,
    )

    q_kw = _keywords(question)
    parts: list[str] = []

    # --- Chronologie filtrée ---
    events = get_chronologie()
    if events:
        filtered = [
            e for e in events
            if q_kw & _keywords(e.get("label", "") + " " + e.get("doc_subject", ""))
        ] or events  # si aucun match, garde tout
        filtered = filtered[:25]
        lines = ["## Chronologie des événements clés"]
        for e in filtered:
            src = f"[{e.get('doc_date', '')[:10]} — {e.get('doc_correspondent', '')} — {e.get('doc_subject', '')[:60]}]"
            lines.append(f"- {e['date']} : {e['label']} {src}")
        parts.append("\n".join(lines))

    # --- Résumés des fils de discussion ---
    fils = get_resumes_par_fil()
    if fils:
        lines = ["## Fils de discussion"]
        for fil in fils:
            sujet = fil.get("sujet", "")
            n = fil.get("n_mails", 0)
            d_min = (fil.get("date_min") or "")[:10]
            d_max = (fil.get("date_max") or "")[:10]
            lines.append(f"\n### {sujet} ({n} mails, {d_min} → {d_max})")
            # Résumés des documents du fil (intention + resume, tronqués)
            for doc in (fil.get("docs") or [])[:6]:
                intention = (doc.get("intention") or "").strip()
                resume = (doc.get("resume") or "").strip()
                if not intention and not resume:
                    continue
                label = f"[{(doc.get('date') or '')[:10]} — {doc.get('correspondent', '')} — {doc.get('subject', '')[:50]}]"
                snippet = (intention or resume)[:150]
                lines.append(f"  - {label} {snippet}")
        parts.append("\n".join(lines))

    # --- Déclarations des parties ---
    contentieux = get_points_contentieux()
    if contentieux:
        lines = ["## Déclarations des parties"]
        for auteur_block in contentieux[:12]:
            auteur = auteur_block.get("auteur", "")
            lines.append(f"\n### {auteur}")
            for decl in (auteur_block.get("declarations") or [])[:5]:
                contenu = (decl.get("contenu") or "").strip()[:200]
                src = f"[{(decl.get('doc_date') or '')[:10]} — {decl.get('doc_subject', '')[:50]}]"
                lines.append(f"  - {contenu} {src}")
        parts.append("\n".join(lines))

    briefing = "\n\n".join(parts)
    # Limite stricte pour ne pas saturer le contexte LLM
    if len(briefing) > 4000:
        briefing = briefing[:4000] + "\n[... briefing tronqué]"
    return briefing


def _build_chat(
    chat: Chat | None,
    chat_provider: str | None,
    chat_model: str | None,
) -> Chat:
    if chat is not None:
        return chat
    if chat_provider or chat_model:
        from config import cfg as _cfg
        import copy
        _c = copy.copy(_cfg)
        if chat_provider:
            _c.chat_provider = chat_provider
        if chat_model:
            _c.chat_model = chat_model
        return get_chat(_c)
    return get_chat()


def _build_messages(
    question: str,
    history: list[dict] | None,
    session_ctx: dict | None,
    briefing: str,
    context: str,
) -> list[dict]:
    ctx_block = _format_session_ctx(session_ctx or {})
    prefix = f"{ctx_block}\n\n" if ctx_block else ""
    briefing_block = (
        f"[BRIEFING DU DOSSIER — données structurées extraites]\n\n{briefing}"
    ) if briefing else ""
    context_block = f"[EXTRAITS PLEIN TEXTE — documents pertinents]\n\n{context}"
    messages = _history_messages(history)
    messages.append({
        "role": "user",
        "content": (
            f"{prefix}"
            f"{briefing_block}\n\n"
            f"{context_block}\n\n"
            "---\n\n"
            f"QUESTION / MISSION : {question}\n\n"
            "Construis un narratif sourcé en citant tes sources "
            "[date — correspondant — sujet] après chaque affirmation."
        ),
    })
    return messages


def synthesize_stream(
    question: str,
    history: list[dict] | None = None,
    session_ctx: dict | None = None,
    chat: Chat | None = None,
    chat_provider: str | None = None,
    chat_model: str | None = None,
    top_k: int = 25,
    expand_extra: int = 6,
):
    """Générateur SSE pour la synthèse.

    Yield des chaînes formatées SSE :
      data: {"type": "step", "text": "..."}   — étape de préparation
      data: {"type": "thinking", "text": "..."} — raisonnement du LLM
      data: {"type": "text", "text": "..."}   — réponse progressive
      data: {"type": "sources", "sources": [...], "session_ctx": {...}} — fin
    """
    import json as _json

    def sse(obj: dict) -> str:
        return f"data: {_json.dumps(obj, ensure_ascii=False)}\n\n"

    yield sse({"type": "step", "text": "Préparation du briefing structuré…"})

    briefing = _build_sql_briefing(question)

    yield sse({"type": "step", "text": "Recherche vectorielle dans les documents…"})

    search_query = _rewrite_query(question)
    hits: list[Hit] = search(search_query, top_k=top_k, expand=False)
    if expand_extra > 0:
        from rag.retrieve import expand_with_graph
        hits = expand_with_graph(hits, max_extra=expand_extra)
    context, sources = format_context(hits)
    if not context:
        context = "(aucun extrait plein texte trouvé dans les documents indexés)"

    yield sse({"type": "step", "text": "Synthèse en cours…"})

    _chat = _build_chat(chat, chat_provider, chat_model)
    messages = _build_messages(question, history, session_ctx, briefing, context)

    full_text = ""
    try:
        for event_type, chunk in _chat.complete_stream(SYNTHESIS_SYSTEM_PROMPT, messages):
            if event_type == "thinking" and chunk:
                yield sse({"type": "thinking", "text": chunk})
            elif event_type == "text" and chunk:
                full_text += chunk
                yield sse({"type": "text", "text": chunk})
            elif event_type == "done":
                break
    except Exception as exc:
        full_text = f"Erreur lors de la synthèse : {exc}"
        yield sse({"type": "text", "text": full_text})

    full_text = _strip_thinking(full_text)
    new_ctx = _update_session_ctx(session_ctx, full_text, hits, None)
    yield sse({"type": "sources", "sources": sources, "session_ctx": new_ctx})


def synthesize(
    question: str,
    history: list[dict] | None = None,
    session_ctx: dict | None = None,
    chat: Chat | None = None,
    chat_provider: str | None = None,
    chat_model: str | None = None,
    top_k: int = 25,
    expand_extra: int = 6,
) -> dict:
    """Construit un narratif sourcé sur l'historique complet.

    Retourne {'answer': str, 'sources': [...], 'session_ctx': dict}.
    """
    _chat = _build_chat(chat, chat_provider, chat_model)
    briefing = _build_sql_briefing(question)
    search_query = _rewrite_query(question)
    hits: list[Hit] = search(search_query, top_k=top_k, expand=False)
    if expand_extra > 0:
        from rag.retrieve import expand_with_graph
        hits = expand_with_graph(hits, max_extra=expand_extra)
    context, sources = format_context(hits)
    if not context:
        context = "(aucun extrait plein texte trouvé dans les documents indexés)"
    messages = _build_messages(question, history, session_ctx, briefing, context)
    try:
        result = _chat.complete(SYNTHESIS_SYSTEM_PROMPT, messages, tools=None)
        answer_text = _strip_thinking(result.get("text", ""))
    except Exception as exc:
        answer_text = f"Erreur lors de la synthèse : {exc}"
    new_ctx = _update_session_ctx(session_ctx, answer_text, hits, None)
    return {"answer": answer_text, "sources": sources, "session_ctx": new_ctx}
