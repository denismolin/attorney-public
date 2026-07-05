"""Application Flask — jalon 1 : navigation (frise Plotly) + chat RAG + /admin.

Routes :
  GET  /                       frise chronologique + liste filtrable
  GET  /email/<doc_id>         détail d'un email (corps nettoyé, citations repliées, PJ)
  GET  /attachment/<att_id>    téléchargement d'une pièce jointe
  GET  /chat                   UI du chatbot
  POST /api/chat               question -> réponse sourcée
  GET  /admin                  UI d'indexation (bouton + barre + journal)
  POST /api/index/start        lance l'indexation (thread) ; 409 si déjà en cours
  GET  /api/index/status       progression (pollée par admin.js)
"""
from __future__ import annotations

import functools
import os
import secrets
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    stream_with_context,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)

import auth_state
import eval_state
import graph_state
import indexer
import state
from config import GENERATED_DIR, UPLOADS_DIR, cfg
from parsing.cleaner import clean_body
from parsing.eml_parser import parse_eml
from rag.index import heartbeat_ok
from viz.timeline_viz import build_timeline_html
from viz.network_viz import build_network_html
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.wrappers import Response as WerkzeugResponse

app = Flask(__name__)
# Archives de migration volumineuses (embeddings) → plafond d'upload généreux (2 Go).
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
if not os.environ.get("FLASK_SECRET_KEY"):
    print("[auth] FLASK_SECRET_KEY absente : clé de session éphémère générée "
          "(les connexions ne survivront pas à un redémarrage).")

cfg.ensure_dirs()
state.init_db()
state.reset_stale_running()
eval_state.init_eval_db()
graph_state.init_graph_db()
auth_state.init_users_db()

_admin_generated_password = auth_state.ensure_admin_bootstrap(
    admin_username=os.environ.get("ADMIN_USERNAME", "admin"),
    admin_password=os.environ.get("ADMIN_PASSWORD"),
)
if _admin_generated_password:
    print(
        "\n"
        "==================================================================\n"
        f"  Compte admin créé — identifiant : {os.environ.get('ADMIN_USERNAME', 'admin')}\n"
        f"  Mot de passe généré (à noter, affiché une seule fois) : {_admin_generated_password}\n"
        "==================================================================\n"
    )

login_manager = LoginManager()
login_manager.login_view = "login_page"
login_manager.init_app(app)

@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect(url_for(login_manager.login_view, next=request.url))


class User(UserMixin):
    def __init__(self, row: dict):
        self.id = row["id"]
        self.username = row["username"]
        self.role = row["role"]
        self.must_change_password = bool(row["must_change_password"])

    def get_id(self) -> str:
        return str(self.id)


@login_manager.user_loader
def load_user(user_id: str):
    row = auth_state.get_user_by_id(int(user_id))
    return User(row) if row else None


def admin_required(view):
    @functools.wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if current_user.is_authenticated:
        return _redirect_home()
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        row = auth_state.verify_password(username, password)
        if row:
            login_user(User(row))
            return _redirect_home()
        error = "Identifiants invalides."
    return render_template("login.html", error=error)


def _redirect_home():
    from flask import redirect, url_for
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return _redirect_home()


def _resolve_path(stored: str) -> Path:
    """Résout un source_path stocké en base, qu'il soit absolu local ou Docker (/app/data/generated/...)."""
    p = Path(stored)
    if p.exists():
        return p
    # Chemin Docker : /app/data/generated/... -> GENERATED_DIR/...
    s = stored.replace("\\", "/")
    for prefix in ("/app/data/generated/", "/app/data/generated"):
        if s.startswith(prefix):
            relative = s[len(prefix):]
            candidate = GENERATED_DIR / relative
            if candidate.exists():
                return candidate
    return p  # inexistant, sera géré par l'appelant


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #
@app.route("/")
@login_required
def index():
    docs = state.list_documents()
    timeline_html = build_timeline_html(docs)
    emails = [d for d in docs if d.get("type") == "mail"]
    return render_template(
        "timeline.html",
        timeline_html=timeline_html,
        emails=emails,
        n_docs=len(docs),
        chroma_ok=heartbeat_ok(),
    )


@app.route("/graph")
@login_required
def graph_page():
    docs = state.list_documents()
    with graph_state._conn() as c:
        edges = [dict(r) for r in c.execute(
            "SELECT src, dst, rel, MAX(weight) AS weight FROM doc_edges GROUP BY src, dst, rel"
        ).fetchall()]
    net_html = build_network_html(docs, edges) if docs else ""
    return render_template(
        "graph.html",
        net_html=net_html,
        n_nodes=len(docs),
        n_edges=len(edges),
    )


@app.route("/synthesis")
@login_required
def synthesis_page():
    return render_template(
        "synthesis.html",
        available_providers=_available_providers(),
        chat_provider=cfg.chat_provider,
    )


@app.route("/api/synthesis/chat", methods=["POST"])
@login_required
def api_synthesis_chat():
    payload = request.get_json(force=True) or {}
    question = (payload.get("question") or "").strip()
    history = payload.get("history") or []
    session_ctx = payload.get("session_ctx") or {}
    chat_provider = (payload.get("chat_provider") or "").strip() or None
    chat_model = (payload.get("chat_model") or "").strip() or None
    top_k = int(payload.get("top_k") or 25)
    expand_extra = int(payload.get("expand_extra") or 6)
    if not question:
        return jsonify({"error": "Question vide."}), 400
    if isinstance(history, list):
        history = history[-8:]
    else:
        history = []
    try:
        from rag.synthesis_chat import synthesize
        result = synthesize(question, history=history, session_ctx=session_ctx,
                            chat_provider=chat_provider, chat_model=chat_model,
                            top_k=top_k, expand_extra=expand_extra)
        return jsonify(result)
    except Exception as exc:  # pragma: no cover
        return jsonify({"error": f"Erreur synthèse : {exc}"}), 500


@app.route("/api/synthesis/stream", methods=["POST"])
@login_required
def api_synthesis_stream():
    payload = request.get_json(force=True) or {}
    question = (payload.get("question") or "").strip()
    history = payload.get("history") or []
    session_ctx = payload.get("session_ctx") or {}
    chat_provider = (payload.get("chat_provider") or "").strip() or None
    chat_model = (payload.get("chat_model") or "").strip() or None
    top_k = int(payload.get("top_k") or 25)
    expand_extra = int(payload.get("expand_extra") or 6)
    if not question:
        return jsonify({"error": "Question vide."}), 400
    if not isinstance(history, list):
        history = []
    history = history[-8:]

    def generate():
        import json as _json
        try:
            from rag.synthesis_chat import synthesize_stream
            yield from synthesize_stream(
                question,
                history=history,
                session_ctx=session_ctx,
                chat_provider=chat_provider,
                chat_model=chat_model,
                top_k=top_k,
                expand_extra=expand_extra,
            )
        except Exception as exc:  # pragma: no cover
            yield f"data: {_json.dumps({'type': 'error', 'text': str(exc)}, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


_VALID_EFFORTS = {"low", "medium", "high"}


def _clean_effort(value) -> str | None:
    """Valide le niveau d'effort de raisonnement (low|medium|high). None sinon."""
    v = (str(value or "").strip().lower())
    return v if v in _VALID_EFFORTS else None


@app.route("/advisor")
@login_required
def advisor_page():
    return render_template(
        "advisor.html",
        available_providers=_available_providers(),
        chat_provider=cfg.chat_provider,
    )


@app.route("/api/advisor/chat", methods=["POST"])
@login_required
def api_advisor_chat():
    payload = request.get_json(force=True) or {}
    question = (payload.get("question") or "").strip()
    history = payload.get("history") or []
    session_ctx = payload.get("session_ctx") or {}
    chat_provider = (payload.get("chat_provider") or "").strip() or None
    chat_model = (payload.get("chat_model") or "").strip() or None
    top_k = int(payload.get("top_k") or 25)
    expand_extra = int(payload.get("expand_extra") or 6)
    reasoning_effort = _clean_effort(payload.get("reasoning_effort"))
    if not question:
        return jsonify({"error": "Scénario vide."}), 400
    if isinstance(history, list):
        history = history[-8:]
    else:
        history = []
    try:
        from rag.advisor_chat import advise
        result = advise(question, history=history, session_ctx=session_ctx,
                        chat_provider=chat_provider, chat_model=chat_model,
                        top_k=top_k, expand_extra=expand_extra,
                        reasoning_effort=reasoning_effort)
        return jsonify(result)
    except Exception as exc:  # pragma: no cover
        return jsonify({"error": f"Erreur conseil : {exc}"}), 500


@app.route("/api/advisor/stream", methods=["POST"])
@login_required
def api_advisor_stream():
    payload = request.get_json(force=True) or {}
    question = (payload.get("question") or "").strip()
    history = payload.get("history") or []
    session_ctx = payload.get("session_ctx") or {}
    chat_provider = (payload.get("chat_provider") or "").strip() or None
    chat_model = (payload.get("chat_model") or "").strip() or None
    top_k = int(payload.get("top_k") or 25)
    expand_extra = int(payload.get("expand_extra") or 6)
    reasoning_effort = _clean_effort(payload.get("reasoning_effort"))
    if not question:
        return jsonify({"error": "Scénario vide."}), 400
    if not isinstance(history, list):
        history = []
    history = history[-8:]

    def generate():
        import json as _json
        try:
            from rag.advisor_chat import advise_stream
            yield from advise_stream(
                question,
                history=history,
                session_ctx=session_ctx,
                chat_provider=chat_provider,
                chat_model=chat_model,
                top_k=top_k,
                expand_extra=expand_extra,
                reasoning_effort=reasoning_effort,
            )
        except Exception as exc:  # pragma: no cover
            yield f"data: {_json.dumps({'type': 'error', 'text': str(exc)}, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/email/<doc_id>")
@login_required
def email_detail(doc_id: str):
    doc = state.get_document(doc_id)
    if not doc or doc.get("type") != "mail":
        abort(404)

    # Re-parse à la volée depuis le fichier source pour le corps complet + PJ.
    src = _resolve_path(doc["source_path"])
    body_new, body_quoted, attachments_list, headers = "", "", [], {}
    if src.exists():
        em = parse_eml(src, doc["correspondent"])
        cleaned = clean_body(em.body_text)
        body_new, body_quoted = cleaned["new"], cleaned["quoted"]
        headers = {
            "from": em.from_,
            "to": ", ".join(em.to),
            "cc": ", ".join(em.cc),
            "date": em.date_iso,
            "subject": em.subject,
        }
        for att in em.attachments:
            att_reg = state.get_document(att.doc_id)
            attachments_list.append(
                {
                    "doc_id": att.doc_id,
                    "filename": att.filename,
                    "size": att.size,
                    "needs_ocr": bool(att_reg and att_reg.get("needs_ocr")),
                }
            )

    return render_template(
        "email.html",
        doc=doc,
        headers=headers,
        body_new=body_new,
        body_quoted=body_quoted,
        attachments=attachments_list,
        graph=_doc_graph_context(doc_id, doc),
    )


# Libellés lisibles des relations du graphe (partagés UI + API).
_REL_LABELS = {
    "reply_to": "En réponse à",
    "same_thread": "Même fil de discussion",
    "attached_to": "Pièce jointe du mail",
    "principal": "Document principal",
    "annexe_of": "Annexe de",
    "accompanies": "Accompagne",
    "shares_code": "Partage une référence",
}


def _doc_graph_context(doc_id: str, doc: dict) -> dict:
    """Assemble le contexte graphe d'un document : enrichissement, fil, voisins.

    Réutilisé par la page de détail (HTML) et la route API JSON.
    """
    enrichment = graph_state.get_enrichment(doc_id)

    # Fil de discussion (autres mails du même thread).
    thread_docs = []
    if doc.get("thread_id"):
        for d in state.get_thread_docs(doc["thread_id"], exclude_doc_id=doc_id):
            thread_docs.append({
                "doc_id": d["doc_id"], "type": d["type"],
                "date": d.get("date", ""), "subject": d.get("subject", ""),
                "correspondent": d.get("correspondent", ""),
            })

    # Voisins du graphe (PJ, docs liés, références partagées).
    neighbors = []
    for nb in graph_state.neighbors(doc_id):
        nd = state.get_document(nb["doc_id"])
        if not nd:
            continue
        neighbors.append({
            "doc_id": nb["doc_id"],
            "rel": nb["rel"],
            "rel_label": _REL_LABELS.get(nb["rel"], nb["rel"]),
            "type": nd.get("type", ""),
            "subject": nd.get("subject", ""),
            "date": nd.get("date", ""),
            "correspondent": nd.get("correspondent", ""),
        })

    return {"enrichment": enrichment, "thread": thread_docs, "neighbors": neighbors}


@app.route("/api/doc/<doc_id>/graph")
@login_required
def api_doc_graph(doc_id: str):
    doc = state.get_document(doc_id)
    if not doc:
        abort(404)
    return jsonify(_doc_graph_context(doc_id, doc))


@app.route("/attachment/<att_id>")
@login_required
def attachment_download(att_id: str):
    doc = state.get_document(att_id)
    if not doc or doc.get("type") != "attachment":
        abort(404)
    abs_path = _resolve_path(doc["source_path"]).resolve()
    if not abs_path.exists():
        abort(404)
    # Sécurité : rester sous GENERATED_DIR (où vivent les PJ extraites).
    if GENERATED_DIR.resolve() not in abs_path.parents:
        abort(403)
    return send_file(abs_path, as_attachment=True, download_name=doc["subject"])


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
@app.route("/chat")
@login_required
def chat_page():
    return render_template(
        "chat.html",
        chroma_ok=heartbeat_ok(),
        available_providers=_available_providers(),
        chat_provider=cfg.chat_provider,
    )


@app.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    payload = request.get_json(force=True) or {}
    question = (payload.get("question") or "").strip()
    history = payload.get("history") or []
    session_ctx = payload.get("session_ctx") or {}
    chat_provider = (payload.get("chat_provider") or "").strip() or None
    chat_model = (payload.get("chat_model") or "").strip() or None
    if not question:
        return jsonify({"error": "Question vide."}), 400
    if isinstance(history, list):
        history = history[-12:]
    else:
        history = []
    try:
        from rag.chat import answer

        result = answer(question, history=history, session_ctx=session_ctx,
                        chat_provider=chat_provider, chat_model=chat_model)
        return jsonify(result)
    except Exception as exc:  # pragma: no cover
        return jsonify({"error": f"Erreur du chat : {exc}"}), 500


# --------------------------------------------------------------------------- #
# Indexation
# --------------------------------------------------------------------------- #
@app.route("/admin")
@admin_required
def admin_page():
    return render_template(
        "admin.html",
        chroma_ok=heartbeat_ok(),
        embed_provider=cfg.embed_provider,
        chat_provider=cfg.chat_provider,
        available_providers=_available_providers(),
    )


_UPLOAD_ALLOWED = {".eml"}
_UPLOAD_MAX_BYTES = 20 * 1024 * 1024  # 20 Mo par fichier


@app.route("/api/upload", methods=["POST"])
@admin_required
def api_upload():
    """Dépose un ou plusieurs fichiers .eml dans SOURCE_DIR/uploads/.

    Retourne la liste des fichiers acceptés et refusés.
    """
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "Aucun fichier reçu."}), 400

    accepted: list[str] = []
    rejected: list[dict] = []

    for f in files:
        name = f.filename or ""
        suffix = Path(name).suffix.lower()
        if suffix not in _UPLOAD_ALLOWED:
            rejected.append({"name": name, "reason": f"Extension {suffix!r} non supportée (attendu : .eml)"})
            continue
        if f.content_length and f.content_length > _UPLOAD_MAX_BYTES:
            rejected.append({"name": name, "reason": "Fichier trop volumineux (max 20 Mo)"})
            continue
        # Nom de fichier sûr
        safe_name = Path(name).name
        safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in safe_name)
        dest = UPLOADS_DIR / safe_name
        # Si le fichier existe déjà, on ajoute un suffixe numérique
        if dest.exists():
            base, ext = dest.stem, dest.suffix
            for n in range(1, 999):
                candidate = UPLOADS_DIR / f"{base}_{n}{ext}"
                if not candidate.exists():
                    dest = candidate
                    break
        try:
            data = f.read(_UPLOAD_MAX_BYTES + 1)
            if len(data) > _UPLOAD_MAX_BYTES:
                rejected.append({"name": name, "reason": "Fichier trop volumineux (max 20 Mo)"})
                continue
            dest.write_bytes(data)
            accepted.append(dest.name)
        except Exception as exc:
            rejected.append({"name": name, "reason": f"Erreur d'écriture : {exc}"})

    if not accepted and rejected:
        return jsonify({"error": "Aucun fichier accepté.", "rejected": rejected}), 400
    return jsonify({"accepted": accepted, "rejected": rejected})


@app.route("/api/index/start", methods=["POST"])
@admin_required
def api_index_start():
    if not heartbeat_ok():
        return jsonify({"error": "ChromaDB injoignable. Vérifiez le service chromadb."}), 503
    payload = request.get_json(force=True, silent=True) or {}
    enrich_provider = payload.get("enrich_provider") or None
    enrich_model = payload.get("enrich_model") or None
    started = indexer.start_async(enrich_provider=enrich_provider, enrich_model=enrich_model)
    if not started:
        return jsonify({"error": "Une indexation est déjà en cours."}), 409
    return jsonify({"status": "started"})


@app.route("/api/index/status")
@admin_required
def api_index_status():
    return jsonify(state.get_status())


@app.route("/api/index/reset", methods=["POST"])
@admin_required
def api_index_reset():
    if state.is_running():
        return jsonify({"error": "Indexation en cours — attendez la fin avant de réinitialiser."}), 409
    if not heartbeat_ok():
        return jsonify({"error": "ChromaDB injoignable."}), 503
    from rag.index import reset_index
    counts = reset_index()
    return jsonify({
        "status": "reset",
        "deleted_documents": counts.get("documents", 0),
        "deleted_chunks": counts.get("chunks", 0),
    })


# --------------------------------------------------------------------------- #
# Évaluation RAG
# --------------------------------------------------------------------------- #
def _available_providers() -> list[str]:
    providers = []
    if cfg.openai_api_key:
        providers.append("openai")
    if cfg.anthropic_api_key:
        providers.append("anthropic")
    if cfg.mistral_api_key:
        providers.append("mistral")
    if cfg.vllm_chat_base_url or cfg.vllm_base_url:
        providers.append("vllm")
    return providers


@app.route("/eval")
@login_required
def eval_page():
    return render_template(
        "eval.html",
        available_providers=_available_providers(),
        chroma_ok=heartbeat_ok(),
    )


def _fetch_models_for_provider(provider: str) -> tuple[list[str], str | None]:
    """Retourne (liste_modèles, erreur_ou_None) pour un provider donné."""
    provider = provider.lower()
    models: list[str] = []
    try:
        if provider == "openai" and cfg.openai_api_key:
            from openai import OpenAI
            client = OpenAI(api_key=cfg.openai_api_key)
            data = client.models.list().data
            models = sorted(
                [m.id for m in data if any(m.id.startswith(p) for p in ("gpt-", "o1", "o3", "o4", "chatgpt"))],
                reverse=True,
            )
        elif provider == "anthropic" and cfg.anthropic_api_key:
            import anthropic
            client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
            data = client.models.list().data
            models = [m.id for m in data]
        elif provider == "mistral" and cfg.mistral_api_key:
            from mistralai import Mistral
            client = Mistral(api_key=cfg.mistral_api_key)
            resp = client.models.list()
            models = sorted([m.id for m in (resp.data or [])], reverse=True)
        elif provider == "vllm":
            from openai import OpenAI
            base_url = cfg.vllm_chat_base_url or cfg.vllm_base_url
            if base_url:
                client = OpenAI(api_key=cfg.vllm_api_key or "EMPTY", base_url=base_url)
                data = client.models.list().data
                models = [m.id for m in data]
    except Exception as exc:
        return [], str(exc)
    return models, None


@app.route("/api/eval/models/<provider>")
@login_required
def api_eval_models(provider: str):
    """Retourne la liste des modèles disponibles pour un provider donné."""
    models, err = _fetch_models_for_provider(provider)
    if err:
        return jsonify({"models": [], "error": err})
    return jsonify({"models": models})


@app.route("/api/chat/models/<provider>")
@login_required
def api_chat_models(provider: str):
    """Mêmes modèles que /api/eval/models — partagé par la page Chat."""
    models, err = _fetch_models_for_provider(provider)
    if err:
        return jsonify({"models": [], "error": err})
    return jsonify({"models": models})


@app.route("/api/eval/generate", methods=["POST"])
@login_required
def api_eval_generate():
    from eval.generator import start_async as gen_start
    payload = request.get_json(force=True) or {}
    provider = payload.get("provider", "")
    model = payload.get("model", "auto")
    n_questions = int(payload.get("n_questions", 10))
    categories = payload.get("categories") or ["factual", "procedural", "comparative", "temporal", "synthetic"]
    difficulties = payload.get("difficulties") or ["easy", "medium", "hard"]
    if not provider:
        return jsonify({"error": "provider manquant"}), 400
    if eval_state.is_gen_running():
        return jsonify({"error": "Génération déjà en cours."}), 409
    started = gen_start(provider, model, n_questions, categories, difficulties)
    if not started:
        return jsonify({"error": "Génération déjà en cours."}), 409
    return jsonify({"status": "started"})


@app.route("/api/eval/gen_status")
@login_required
def api_eval_gen_status():
    return jsonify(eval_state.get_gen_status())


@app.route("/api/eval/gen_cancel", methods=["POST"])
@login_required
def api_eval_gen_cancel():
    from eval.generator import cancel as gen_cancel
    gen_cancel()
    return jsonify({"status": "cancelling"})


@app.route("/api/eval/questions", methods=["GET"])
@login_required
def api_eval_questions_list():
    category = request.args.get("category")
    difficulty = request.args.get("difficulty")
    return jsonify(eval_state.list_questions(category=category, difficulty=difficulty))


@app.route("/api/eval/questions", methods=["POST"])
@login_required
def api_eval_question_add():
    payload = request.get_json(force=True) or {}
    if not payload.get("question") or not payload.get("expected"):
        return jsonify({"error": "question et expected requis"}), 400
    q_id = eval_state.insert_question(payload)
    return jsonify({"id": q_id})


@app.route("/api/eval/questions/<int:q_id>", methods=["PUT"])
@login_required
def api_eval_question_update(q_id: int):
    payload = request.get_json(force=True) or {}
    eval_state.update_question(q_id, payload)
    return jsonify({"status": "updated"})


@app.route("/api/eval/questions/<int:q_id>", methods=["DELETE"])
@login_required
def api_eval_question_delete(q_id: int):
    eval_state.delete_question(q_id)
    return jsonify({"status": "deleted"})


@app.route("/api/eval/questions/clear", methods=["DELETE"])
@login_required
def api_eval_questions_clear():
    n = eval_state.clear_questions()
    return jsonify({"deleted": n})


@app.route("/api/eval/export")
@login_required
def api_eval_export():
    import io
    import json as _json
    data = eval_state.export_questions()
    buf = io.BytesIO(_json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/json",
        as_attachment=True,
        download_name="golden_dataset.json",
    )


@app.route("/api/eval/import", methods=["POST"])
@login_required
def api_eval_import():
    import json as _json
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "Aucun fichier"}), 400
    try:
        items = _json.loads(f.read().decode("utf-8"))
        if not isinstance(items, list):
            return jsonify({"error": "Le fichier doit contenir une liste JSON"}), 400
    except Exception as exc:
        return jsonify({"error": f"JSON invalide : {exc}"}), 400
    result = eval_state.import_questions(items)
    return jsonify(result)


@app.route("/api/eval/run", methods=["POST"])
@login_required
def api_eval_run():
    from eval.judge import start_run
    payload = request.get_json(force=True) or {}
    provider = payload.get("provider", "")
    model = payload.get("model", "auto")
    question_ids = payload.get("question_ids") or None
    threshold = float(payload.get("threshold", 6.0))
    if not provider:
        return jsonify({"error": "provider manquant"}), 400
    try:
        run_id = start_run(provider, model, question_ids, threshold)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"run_id": run_id})


@app.route("/api/eval/run_status/<run_id>")
@login_required
def api_eval_run_status(run_id: str):
    run = eval_state.get_run(run_id)
    if not run:
        abort(404)
    return jsonify(run)


@app.route("/api/eval/run_cancel/<run_id>", methods=["POST"])
@login_required
def api_eval_run_cancel(run_id: str):
    from eval.judge import cancel_run
    ok = cancel_run(run_id)
    return jsonify({"status": "cancelling" if ok else "not_found"})


@app.route("/api/eval/results/<run_id>")
@login_required
def api_eval_results(run_id: str):
    run = eval_state.get_run(run_id)
    if not run:
        abort(404)
    category = request.args.get("category")
    verdict = request.args.get("verdict")
    difficulty = request.args.get("difficulty")
    results = eval_state.list_results(run_id, category=category, verdict=verdict, difficulty=difficulty)
    summary = eval_state.get_summary(run_id)
    return jsonify({"run": run, "results": results, "summary": summary})


@app.route("/api/eval/runs")
@login_required
def api_eval_runs():
    return jsonify(eval_state.list_runs())


@app.route("/api/eval/download/<run_id>")
@login_required
def api_eval_download(run_id: str):
    import io
    fmt = request.args.get("format", "md")
    if fmt == "html":
        content = eval_state.build_html_report(run_id)
        mimetype = "text/html"
        ext = "html"
    else:
        content = eval_state.build_markdown_report(run_id)
        mimetype = "text/markdown"
        ext = "md"
    buf = io.BytesIO(content.encode("utf-8"))
    buf.seek(0)
    return send_file(
        buf,
        mimetype=mimetype,
        as_attachment=True,
        download_name=f"eval_{run_id[:8]}.{ext}",
    )


# --------------------------------------------------------------------------- #
# Migration / sauvegarde (export-import des bases)
# --------------------------------------------------------------------------- #
@app.route("/api/migrate/export")
@admin_required
def api_migrate_export():
    """Crée une archive de migration et la renvoie en téléchargement.

    ?sources=0 pour une archive légère (sans les emails sources .eml).
    """
    if not heartbeat_ok():
        return jsonify({"error": "ChromaDB injoignable — impossible d'exporter les vecteurs."}), 503
    if state.is_running():
        return jsonify({"error": "Indexation en cours — réessayez après la fin."}), 409
    with_sources = request.args.get("sources", "1") not in ("0", "false", "no")
    try:
        import migrate
        archive = migrate.export_archive(with_sources=with_sources)
    except Exception as exc:  # pragma: no cover
        return jsonify({"error": f"Échec de l'export : {exc}"}), 500
    return send_file(archive, as_attachment=True, download_name=archive.name)


@app.route("/api/migrate/import", methods=["POST"])
@admin_required
def api_migrate_import():
    """Importe une archive de migration (.zip) — réinjecte SQLite + vecteurs Chroma."""
    if state.is_running():
        return jsonify({"error": "Indexation en cours — attendez la fin avant d'importer."}), 409
    if not heartbeat_ok():
        return jsonify({"error": "ChromaDB injoignable."}), 503
    f = request.files.get("file")
    if not f or not (f.filename or "").lower().endswith(".zip"):
        return jsonify({"error": "Fichier .zip attendu."}), 400
    import tempfile
    import migrate
    tmp = Path(tempfile.gettempdir()) / f"avocat-upload-{Path(f.filename).name}"
    try:
        f.save(tmp)
        summary = migrate.import_archive(tmp, assume_yes=True)
        return jsonify(summary)
    except Exception as exc:  # pragma: no cover
        return jsonify({"error": f"Échec de l'import : {exc}"}), 500
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Réglages (providers, clés API, modèles) — appliqués à chaud
# --------------------------------------------------------------------------- #
@app.route("/settings")
@admin_required
def settings_page():
    return render_template(
        "settings.html",
        settings=cfg.public_dict(),
        available_providers=_available_providers(),
    )


@app.route("/api/settings", methods=["GET"])
@admin_required
def api_settings_get():
    return jsonify(cfg.public_dict())


# Champs texte (non secrets) modifiables via l'UI.
_SETTINGS_TEXT_FIELDS = (
    "embed_provider", "chat_provider", "embed_model", "chat_model",
    "mistral_chat_model", "vllm_base_url",
)


@app.route("/api/settings", methods=["POST"])
@admin_required
def api_settings_post():
    from config import KEY_FIELDS
    payload = request.get_json(force=True) or {}
    updates: dict = {}
    for k in _SETTINGS_TEXT_FIELDS:
        if k in payload and payload[k] is not None:
            updates[k] = str(payload[k]).strip()
    # Clés API : seules celles renseignées (non vides) sont prises en compte —
    # un champ laissé vide conserve la clé existante.
    keys = payload.get("keys") or {}
    for prov, field_ in KEY_FIELDS.items():
        val = (keys.get(prov) or "").strip() if isinstance(keys.get(prov), str) else ""
        if val:
            updates[field_] = val
    cfg.apply(updates)
    return jsonify({"status": "saved", "settings": cfg.public_dict(),
                    "available_providers": _available_providers()})


@app.route("/api/settings/test", methods=["POST"])
@admin_required
def api_settings_test():
    """Valide un provider en listant ses modèles (clé/endpoint OK ?)."""
    payload = request.get_json(force=True) or {}
    provider = (payload.get("provider") or "").strip().lower()
    if not provider:
        return jsonify({"ok": False, "error": "provider manquant"}), 400
    models, err = _fetch_models_for_provider(provider)
    if err:
        return jsonify({"ok": False, "error": err})
    return jsonify({"ok": True, "n_models": len(models),
                    "sample": models[:5]})


# --------------------------------------------------------------------------- #
# Utilisateurs (admin uniquement)
# --------------------------------------------------------------------------- #
def _user_public(row: dict) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "must_change_password": bool(row["must_change_password"]),
        "created_at": row["created_at"],
        "created_by": row["created_by"],
    }


@app.route("/admin/users")
@admin_required
def admin_users_page():
    return render_template("admin_users.html", users=[_user_public(u) for u in auth_state.list_users()])


@app.route("/api/users", methods=["GET"])
@admin_required
def api_users_list():
    return jsonify([_user_public(u) for u in auth_state.list_users()])


@app.route("/api/users", methods=["POST"])
@admin_required
def api_users_create():
    payload = request.get_json(force=True) or {}
    username = (payload.get("username") or "").strip()
    role = (payload.get("role") or "user").strip()
    if not username:
        return jsonify({"error": "Identifiant requis."}), 400
    if role not in ("admin", "user"):
        return jsonify({"error": "Rôle invalide."}), 400
    if auth_state.get_user_by_username(username):
        return jsonify({"error": "Cet identifiant existe déjà."}), 409
    password = auth_state.generate_password()
    user_id = auth_state.create_user(username, password, role=role, created_by=current_user.username)
    return jsonify({"id": user_id, "username": username, "role": role, "password": password})


@app.route("/api/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def api_users_reset_password(user_id: int):
    row = auth_state.get_user_by_id(user_id)
    if not row:
        abort(404)
    password = auth_state.generate_password()
    auth_state.set_password(user_id, password, must_change=True)
    return jsonify({"id": user_id, "password": password})


@app.route("/api/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def api_users_delete(user_id: int):
    row = auth_state.get_user_by_id(user_id)
    if not row:
        abort(404)
    if user_id == current_user.id:
        return jsonify({"error": "Impossible de supprimer votre propre compte."}), 400
    if row["role"] == "admin" and auth_state.count_users(role="admin") <= 1:
        return jsonify({"error": "Impossible de supprimer le dernier compte admin."}), 400
    auth_state.delete_user(user_id)
    return jsonify({"status": "deleted"})


_root = os.environ.get("APPLICATION_ROOT", "/").rstrip("/")
if _root:
    app.config["APPLICATION_ROOT"] = _root
    app = DispatcherMiddleware(WerkzeugResponse("Not found", status=404), {_root: app})

if __name__ == "__main__":
    # use_reloader=False : le reloader redémarrerait le process et tuerait le
    # thread d'indexation de fond. threaded=True : sert le polling /status pendant
    # que l'indexation tourne.
    flask_app = app.mounts[_root] if _root else app
    flask_app.run(
        host=cfg.flask_host,
        port=cfg.flask_port,
        # Debug désactivé par défaut ; activer avec FLASK_DEBUG=1 en dev local.
        debug=os.environ.get("FLASK_DEBUG") == "1",
        use_reloader=False,
        threaded=True,
    )
