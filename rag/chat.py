"""Chat RAG avec agent structuré (analyse → plan → exécution → synthèse).

Flux :
  1. analyse_question() — décompose la question en sous-questions, identifie
     les entités clés et le contexte déjà établi en session.
  2. build_query_plan() — convertit les sous-questions en requêtes Chroma concrètes.
  3. _execute_plan() — exécute les requêtes, déduplique les hits.
  4. chat.complete() — synthèse avec le contexte de session + extraits trouvés.

Fallback : si l'analyse échoue, bascule en retrieve-then-read direct.
"""
from __future__ import annotations

import re

from rag import intent as intent_mod
from rag import planner as planner_mod
from rag.providers import Chat, get_chat
from rag.retrieve import Hit, format_context, search

SYSTEM_PROMPT = """Tu es un assistant juridique qui aide à analyser la correspondance \
d'une succession franco-italienne (affaire DUPONT / MARTIN / DURAND).

Règles impératives :
- Réponds en FRANÇAIS, de façon précise, concise et professionnelle.
- Appuie-toi UNIQUEMENT sur le CONTEXTE fourni ; ne réponds jamais de mémoire.
- Cite tes sources entre crochets [date — correspondant — sujet] juste après \
l'affirmation qu'elles appuient.
- Si l'information est absente, dis-le clairement plutôt que d'inventer.
- N'écris pas ton raisonnement interne ni de préambule : donne directement la réponse.
"""

_FOLLOWUP_TURNS = 2


def _strip_thinking(text: str) -> str:
    text = re.sub(r"(?is)<think>.*?</think>", "", text)
    text = re.sub(
        r"(?is)^\s*(thinking process|let me think|the user is asking|l'utilisateur demande)\b.*?\n\n",
        "",
        text,
    )
    return text.strip()


def _history_messages(history: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for turn in history or []:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    return out


def _execute_plan(plan: list[dict]) -> list[Hit]:
    """Exécute les requêtes du plan, déduplique par parent_doc_id."""
    seen: set[str] = set()
    hits: list[Hit] = []
    for step in plan:
        q = step.get("query_text", "")
        if not q:
            continue
        results = search(
            q,
            top_k=step.get("top_k", 6),
            affaire=step.get("affaire"),
            expand=True,
        )
        for h in results:
            if h.parent_doc_id not in seen:
                seen.add(h.parent_doc_id)
                hits.append(h)
    return hits


def _format_session_ctx(session_ctx: dict) -> str:
    """Formate le contexte de session pour le prompt de synthèse."""
    faits = session_ctx.get("faits_etablis") or []
    entites = session_ctx.get("entites_connues") or []
    if not faits and not entites:
        return ""
    lines = ["CONTEXTE DE SESSION (faits établis dans cette conversation) :"]
    for f in faits[:10]:
        lines.append(f"- {f}")
    if entites:
        lines.append(f"Entités clés déjà connues : {', '.join(list(entites)[:10])}")
    return "\n".join(lines)


def _update_session_ctx(
    session_ctx: dict | None,
    answer_text: str,
    hits: list[Hit],
    analysis: dict | None,
) -> dict:
    """Met à jour le contexte de session après une réponse."""
    ctx = {
        "faits_etablis": list(session_ctx.get("faits_etablis") or []) if session_ctx else [],
        "docs_vus":      set(session_ctx.get("docs_vus") or []) if session_ctx else set(),
        "entites_connues": set(session_ctx.get("entites_connues") or []) if session_ctx else set(),
    }
    # Ajoute les entités de l'analyse
    if analysis:
        for ent in analysis.get("entites_cles") or []:
            ctx["entites_connues"].add(ent)
    # Ajoute les doc_ids vus
    for h in hits:
        ctx["docs_vus"].add(h.parent_doc_id)
    # Ajoute un résumé tronqué de la réponse comme fait établi
    snippet = answer_text.strip()[:200]
    if snippet and snippet not in ctx["faits_etablis"]:
        ctx["faits_etablis"].append(snippet)
    # Garde les 20 derniers faits
    ctx["faits_etablis"] = ctx["faits_etablis"][-20:]
    # Sérialise les sets en listes pour JSON
    ctx["docs_vus"] = list(ctx["docs_vus"])
    ctx["entites_connues"] = list(ctx["entites_connues"])
    return ctx


def _build_search_query(question: str, history: list[dict] | None) -> str:
    if not history:
        return question
    recent_user = [
        t["content"] for t in history[-_FOLLOWUP_TURNS * 2:]
        if t.get("role") == "user" and t.get("content")
    ]
    return (recent_user[-1] + " " + question) if recent_user else question


def _retrieve_then_read(
    question: str,
    history: list[dict] | None,
    chat: Chat,
    session_ctx: dict | None = None,
) -> dict:
    """Fallback : recherche directe sans analyse d'intention."""
    query = _build_search_query(question, history)
    hits = search(query, top_k=8, expand=True)
    context, sources = format_context(hits)
    if not context:
        context = "(aucun extrait pertinent trouvé dans les documents indexés)"

    ctx_block = _format_session_ctx(session_ctx or {})
    prefix = f"{ctx_block}\n\n" if ctx_block else ""

    messages = _history_messages(history)
    messages.append({
        "role": "user",
        "content": (
            f"{prefix}CONTEXTE (extraits du dossier) :\n\n{context}\n\n---\n\n"
            f"QUESTION : {question}\n\n"
            f"Réponds en français en citant tes sources [date — correspondant — sujet]."
        ),
    })
    result = chat.complete(SYSTEM_PROMPT, messages, tools=None)
    answer_text = _strip_thinking(result.get("text", ""))
    new_ctx = _update_session_ctx(session_ctx, answer_text, hits, None)
    return {"answer": answer_text, "sources": sources, "session_ctx": new_ctx}


def answer(
    question: str,
    history: list[dict] | None = None,
    session_ctx: dict | None = None,
    chat: Chat | None = None,
    chat_provider: str | None = None,
    chat_model: str | None = None,
) -> dict:
    """Retourne {'answer': str, 'sources': [...], 'session_ctx': dict}.

    Flux : analyse → plan → exécution → synthèse.
    Fallback en retrieve-then-read si l'analyse échoue.
    """
    if chat is None:
        if chat_provider or chat_model:
            from config import cfg as _cfg
            import copy
            _c = copy.copy(_cfg)
            if chat_provider:
                _c.chat_provider = chat_provider
            if chat_model:
                _c.chat_model = chat_model
            chat = get_chat(_c)
        else:
            chat = get_chat()

    # Phase 1 : analyse d'intention
    analysis = intent_mod.analyse_question(question, history or [], chat)

    # Phase 2 : plan de recherche
    plan = planner_mod.build_query_plan(analysis) if analysis else []

    if not plan:
        # Fallback : pas de plan → retrieve-then-read direct
        return _retrieve_then_read(question, history, chat, session_ctx)

    # Phase 3 : exécution du plan
    hits = _execute_plan(plan)
    context, sources = format_context(hits)
    if not context:
        context = "(aucun extrait pertinent trouvé dans les documents indexés)"

    # Phase 4 : synthèse
    ctx_block = _format_session_ctx(session_ctx or {})
    prefix = f"{ctx_block}\n\n" if ctx_block else ""

    # Injecte le contexte acquis de l'analyse dans le prompt
    ctx_acquis = analysis.get("contexte_acquis") or []
    if ctx_acquis:
        acquis_block = "Faits déjà établis dans cette session :\n" + "\n".join(f"- {f}" for f in ctx_acquis[:5])
        prefix = f"{prefix}{acquis_block}\n\n"

    messages = _history_messages(history)
    messages.append({
        "role": "user",
        "content": (
            f"{prefix}CONTEXTE (extraits du dossier) :\n\n{context}\n\n---\n\n"
            f"QUESTION : {question}\n\n"
            f"Réponds en français en citant tes sources [date — correspondant — sujet]."
        ),
    })

    try:
        result = chat.complete(SYSTEM_PROMPT, messages, tools=None)
        answer_text = _strip_thinking(result.get("text", ""))
    except Exception:  # noqa: BLE001
        return _retrieve_then_read(question, history, chat, session_ctx)

    new_ctx = _update_session_ctx(session_ctx, answer_text, hits, analysis)
    return {"answer": answer_text, "sources": sources, "session_ctx": new_ctx}
