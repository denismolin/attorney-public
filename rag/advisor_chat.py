"""Agent « Conseil d'avocat » — élabore une stratégie contradictoire.

L'utilisateur expose un scénario / dire de la partie adverse (ex. procédure de
recouvrement de la dette DURAND) ; l'agent produit une réaction stratégique sur
le fond, au service de la défense (Pierre / Marie MARTIN).

Particularités vs les deux autres agents :
  - Orchestration par function-calling : l'agent décide lui-même d'interroger le
    chat (fait précis), de demander une synthèse sourcée, ou de fouiller les
    documents directement. Voir TOOLS / _run_tool_loop.
  - La boucle d'outils tourne en chat.complete() (non streamée — complete_stream
    ne supporte pas tools), puis la RÉDACTION FINALE est streamée via
    complete_stream() pour un meilleur ressenti.
  - Fallback déterministe : si le provider ne renvoie aucun tool_call (modèle sans
    support outils), on injecte d'office un briefing SQL + une recherche directe
    avant la rédaction finale, garantissant une réponse sourcée.
  - Termine toujours par une section « Pièces complémentaires utiles ».
"""
from __future__ import annotations

import json

from rag.chat import (
    _format_session_ctx,
    _history_messages,
    _strip_thinking,
    _update_session_ctx,
)
from rag.chat import answer as chat_answer
from rag.providers import Chat
from rag.retrieve import Hit, expand_with_graph, format_context, search
from rag.synthesis_chat import (
    _build_chat,
    _build_sql_briefing,
    _rewrite_query,
    synthesize,
)

ADVISOR_SYSTEM_PROMPT = """Tu es l'AVOCAT CONSEIL de la défense dans une succession \
franco-italienne (affaire DUPONT / MARTIN / DURAND). Tu défends les intérêts de \
Pierre et Marie MARTIN face à la partie adverse (Madame DURAND, et le cas \
échéant Claire DUPONT).

L'utilisateur te soumet un SCÉNARIO ou un DIRE de la partie adverse (par exemple sur \
la procédure de recouvrement de la dette de 98 500 €, la contestation d'un legs, les \
retraits de fonds…). Ta mission : élaborer une RÉACTION STRATÉGIQUE SUR LE FOND.

Méthode :
- Analyse la thèse adverse : ce qu'elle affirme, sur quoi elle s'appuie, ses présupposés.
- Identifie ses POINTS FAIBLES (factuels, juridiques, chronologiques, probatoires).
- Construis les MOYENS À OPPOSER : arguments de fait et de droit, pièces du dossier qui \
appuient la défense, contradictions à exploiter.
- Propose une LIGNE DE DÉFENSE claire et hiérarchisée (arguments principaux / subsidiaires).

Pour t'informer, tu DISPOSES D'OUTILS — utilise-les avant de conclure :
- `interroger_chat` pour un fait ou un point précis du dossier.
- `demander_synthese` pour une vue d'ensemble sourcée sur un thème.
- `rechercher_documents` pour fouiller toi-même les extraits bruts.
N'invente jamais : appuie chaque affirmation factuelle sur le dossier.

Règles de rédaction :
- Réponds en FRANÇAIS, style juridique professionnel, structuré (titres markdown ##, listes).
- Cite tes sources entre crochets [date — correspondant — sujet] après chaque \
affirmation factuelle.
- N'écris pas de préambule ni de formule de politesse : commence par le contenu.
- TERMINE TOUJOURS par une section `## Pièces complémentaires utiles` listant, à titre \
indicatif, les documents qui RENFORCERAIENT la stratégie : pour chacun, le type de pièce, \
pourquoi elle aide, et l'impact attendu. Précise que leur obtention reste à confirmer par \
l'utilisateur (faisabilité). S'il n'y en a aucune, indique-le explicitement.
"""

_MAX_TOOL_TURNS = 5

# Outils exposés au LLM (format OpenAI ; la couche providers traduit pour Anthropic/Mistral).
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "interroger_chat",
            "description": (
                "Interroge l'agent Q&A sur un fait ou un point PRÉCIS du dossier "
                "(date, montant, qui a dit quoi). Renvoie une réponse sourcée."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "La question factuelle ponctuelle.",
                    }
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "demander_synthese",
            "description": (
                "Demande à l'agent de synthèse un NARRATIF SOURCÉ sur un thème "
                "(vue d'ensemble : positions des parties, chronologie d'un litige). "
                "Renvoie une synthèse citant ses sources."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mission": {
                        "type": "string",
                        "description": "Le thème / la mission de synthèse.",
                    }
                },
                "required": ["mission"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rechercher_documents",
            "description": (
                "Recherche vectorielle dans les documents du dossier et renvoie les "
                "extraits bruts sourcés. À utiliser pour fouiller toi-même."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "requete": {
                        "type": "string",
                        "description": "Les termes de recherche (mots-clés substantiels).",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Nombre d'extraits à récupérer (défaut 8).",
                    },
                },
                "required": ["requete"],
            },
        },
    },
]


def _short(text: str, n: int = 80) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")


def _exec_tool(
    name: str,
    args: dict,
    chat: Chat,
    top_k: int,
    expand_extra: int,
    sources_acc: list[dict],
) -> str:
    """Exécute un outil et renvoie un texte à ré-injecter dans la conversation.

    Agrège les sources rencontrées dans `sources_acc` (dédupliquées par doc_id).
    """
    if name == "interroger_chat":
        res = chat_answer(args.get("question", ""), chat=chat)
        _merge_sources(sources_acc, res.get("sources") or [])
        return res.get("answer", "")

    if name == "demander_synthese":
        res = synthesize(
            args.get("mission", ""),
            chat=chat,
            top_k=top_k,
            expand_extra=expand_extra,
        )
        _merge_sources(sources_acc, res.get("sources") or [])
        return res.get("answer", "")

    if name == "rechercher_documents":
        query = _rewrite_query(args.get("requete", ""))
        k = int(args.get("top_k") or 8)
        hits: list[Hit] = search(query, top_k=k, expand=False)
        if expand_extra > 0:
            hits = expand_with_graph(hits, max_extra=expand_extra)
        context, sources = format_context(hits)
        _merge_sources(sources_acc, sources)
        return context or "(aucun extrait trouvé)"

    return f"(outil inconnu : {name})"


def _merge_sources(acc: list[dict], new: list[dict]) -> None:
    seen = {s.get("doc_id") for s in acc}
    for s in new:
        did = s.get("doc_id")
        if did not in seen:
            seen.add(did)
            acc.append(s)


def _run_tool_loop(
    chat: Chat,
    messages: list[dict],
    top_k: int,
    expand_extra: int,
    sources_acc: list[dict],
    on_tool=None,
    reasoning_effort: str | None = None,
) -> bool:
    """Fait tourner la boucle de function-calling jusqu'à ce que l'agent n'appelle
    plus d'outil (ou max tours). Mute `messages` et `sources_acc` en place.

    `on_tool(name, args)` est appelé (si fourni) avant chaque exécution d'outil,
    pour émettre une étape SSE.

    Retourne True si au moins un outil a été appelé, False sinon (fallback).
    """
    used_any = False
    for _ in range(_MAX_TOOL_TURNS):
        result = chat.complete(
            ADVISOR_SYSTEM_PROMPT, messages, tools=TOOLS, reasoning_effort=reasoning_effort
        )
        tool_calls = result.get("tool_calls") or []
        if not tool_calls:
            break
        used_any = True
        # Trace l'intention de l'assistant pour garder un historique cohérent.
        assistant_text = result.get("text") or "(appel d'outils)"
        names = ", ".join(tc.get("name", "?") for tc in tool_calls)
        messages.append(
            {"role": "assistant", "content": f"{assistant_text}\n[Appels: {names}]"}
        )
        for tc in tool_calls:
            name = tc.get("name", "")
            try:
                args = json.loads(tc.get("arguments") or "{}")
            except (ValueError, TypeError):
                args = {}
            if on_tool:
                on_tool(name, args)
            tool_result = _exec_tool(name, args, chat, top_k, expand_extra, sources_acc)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"[Résultat de l'outil {name}({_short(json.dumps(args, ensure_ascii=False), 100)})]"
                        f"\n\n{tool_result}"
                    ),
                }
            )
    return used_any


def _fallback_context(question: str, top_k: int, expand_extra: int, sources_acc: list[dict]) -> str:
    """Briefing SQL + recherche directe — utilisé si le provider n'a pas appelé d'outil."""
    briefing = _build_sql_briefing(question)
    query = _rewrite_query(question)
    hits: list[Hit] = search(query, top_k=top_k, expand=False)
    if expand_extra > 0:
        hits = expand_with_graph(hits, max_extra=expand_extra)
    context, sources = format_context(hits)
    _merge_sources(sources_acc, sources)
    parts = []
    if briefing:
        parts.append(f"[BRIEFING DU DOSSIER — données structurées]\n\n{briefing}")
    parts.append(f"[EXTRAITS PLEIN TEXTE]\n\n{context or '(aucun extrait trouvé)'}")
    return "\n\n".join(parts)


def _build_initial_messages(
    question: str,
    history: list[dict] | None,
    session_ctx: dict | None,
) -> list[dict]:
    ctx_block = _format_session_ctx(session_ctx or {})
    prefix = f"{ctx_block}\n\n" if ctx_block else ""
    messages = _history_messages(history)
    messages.append(
        {
            "role": "user",
            "content": (
                f"{prefix}SCÉNARIO / DIRE DE LA PARTIE ADVERSE :\n\n{question}\n\n"
                "Élabore la stratégie de défense. Sers-toi des outils pour t'informer "
                "avant de conclure."
            ),
        }
    )
    return messages


_FINAL_INSTRUCTION = (
    "Rédige MAINTENANT la stratégie de défense complète, structurée et sourcée "
    "[date — correspondant — sujet], en t'appuyant sur les informations recueillies "
    "ci-dessus. Termine impérativement par la section "
    "`## Pièces complémentaires utiles`."
)


def advise(
    question: str,
    history: list[dict] | None = None,
    session_ctx: dict | None = None,
    chat: Chat | None = None,
    chat_provider: str | None = None,
    chat_model: str | None = None,
    top_k: int = 25,
    expand_extra: int = 6,
    reasoning_effort: str | None = None,
) -> dict:
    """Variante non-streamée. Retourne {'answer', 'sources', 'session_ctx'}."""
    _chat = _build_chat(chat, chat_provider, chat_model)
    messages = _build_initial_messages(question, history, session_ctx)
    sources_acc: list[dict] = []

    used = _run_tool_loop(
        _chat, messages, top_k, expand_extra, sources_acc, reasoning_effort=reasoning_effort
    )
    if not used:
        messages.append(
            {
                "role": "user",
                "content": _fallback_context(question, top_k, expand_extra, sources_acc),
            }
        )
    messages.append({"role": "user", "content": _FINAL_INSTRUCTION})

    try:
        result = _chat.complete(
            ADVISOR_SYSTEM_PROMPT, messages, tools=None, reasoning_effort=reasoning_effort
        )
        answer_text = _strip_thinking(result.get("text", ""))
    except Exception as exc:  # noqa: BLE001
        answer_text = f"Erreur lors de l'élaboration de la stratégie : {exc}"

    new_ctx = _update_session_ctx(session_ctx, answer_text, [], None)
    return {"answer": answer_text, "sources": sources_acc, "session_ctx": new_ctx}


def advise_stream(
    question: str,
    history: list[dict] | None = None,
    session_ctx: dict | None = None,
    chat: Chat | None = None,
    chat_provider: str | None = None,
    chat_model: str | None = None,
    top_k: int = 25,
    expand_extra: int = 6,
    reasoning_effort: str | None = None,
):
    """Générateur SSE. Types d'événements :
      step      — étape de préparation
      tool      — outil consulté (nom + résumé d'argument)
      thinking  — raisonnement du LLM (rédaction finale)
      text      — réponse progressive (rédaction finale)
      sources   — fin : {sources, session_ctx}
    """

    def sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    yield sse({"type": "step", "text": "Analyse du scénario adverse…"})

    _chat = _build_chat(chat, chat_provider, chat_model)
    messages = _build_initial_messages(question, history, session_ctx)
    sources_acc: list[dict] = []

    yield sse({"type": "step", "text": "Consultation des autres agents…"})

    # Boucle d'outils réimplémentée ici (et non via _run_tool_loop) pour pouvoir
    # yielder un événement `tool` au fil de l'eau, avant chaque appel d'outil.
    # complete_stream ne supporte pas tools, donc la boucle tourne en complete().
    used_any = False
    try:
        for _ in range(_MAX_TOOL_TURNS):
            result = _chat.complete(
                ADVISOR_SYSTEM_PROMPT, messages, tools=TOOLS, reasoning_effort=reasoning_effort
            )
            tool_calls = result.get("tool_calls") or []
            if not tool_calls:
                break
            used_any = True
            assistant_text = result.get("text") or "(appel d'outils)"
            names = ", ".join(tc.get("name", "?") for tc in tool_calls)
            messages.append(
                {"role": "assistant", "content": f"{assistant_text}\n[Appels: {names}]"}
            )
            for tc in tool_calls:
                name = tc.get("name", "")
                try:
                    args = json.loads(tc.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = {}
                label = {
                    "interroger_chat": "Question au chat",
                    "demander_synthese": "Synthèse demandée",
                    "rechercher_documents": "Recherche documentaire",
                }.get(name, name)
                arg_preview = _short(
                    args.get("question") or args.get("mission") or args.get("requete") or ""
                )
                yield sse({"type": "tool", "text": f"🔧 {label} : {arg_preview}"})
                tool_result = _exec_tool(name, args, _chat, top_k, expand_extra, sources_acc)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[Résultat de l'outil {name}("
                            f"{_short(json.dumps(args, ensure_ascii=False), 100)})]"
                            f"\n\n{tool_result}"
                        ),
                    }
                )
    except Exception as exc:  # noqa: BLE001
        yield sse({"type": "step", "text": f"(outils indisponibles : {exc})"})

    if not used_any:
        yield sse({"type": "step", "text": "Recherche directe dans le dossier…"})
        messages.append(
            {
                "role": "user",
                "content": _fallback_context(question, top_k, expand_extra, sources_acc),
            }
        )

    messages.append({"role": "user", "content": _FINAL_INSTRUCTION})

    yield sse({"type": "step", "text": "Rédaction de la stratégie…"})

    # --- Rédaction finale streamée ---
    full_text = ""
    try:
        for event_type, chunk in _chat.complete_stream(
            ADVISOR_SYSTEM_PROMPT, messages, reasoning_effort=reasoning_effort
        ):
            if event_type == "thinking" and chunk:
                yield sse({"type": "thinking", "text": chunk})
            elif event_type == "text" and chunk:
                full_text += chunk
                yield sse({"type": "text", "text": chunk})
            elif event_type == "done":
                break
    except Exception as exc:  # noqa: BLE001
        full_text = f"Erreur lors de la rédaction : {exc}"
        yield sse({"type": "text", "text": full_text})

    full_text = _strip_thinking(full_text)
    new_ctx = _update_session_ctx(session_ctx, full_text, [], None)
    yield sse({"type": "sources", "sources": sources_acc, "session_ctx": new_ctx})
