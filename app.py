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
import hashlib
import os
import re
import secrets
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
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
import case_defaults
import case_state
import db_init
import eval_state
import graph_state
import indexer
import mail_intake
import mailbox_state
import state
from config import APP_VERSION, GENERATED_DIR, SOURCE_DIR, UPLOADS_DIR, cfg
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


def _session_secret() -> tuple[str, str]:
    """Clé de signature des sessions : FLASK_SECRET_KEY, sinon une clé générée une fois
    et conservée avec les données (fichier privé, jamais exporté), commune aux workers."""
    env = os.environ.get("FLASK_SECRET_KEY")
    if env:
        return env, "env"
    path = GENERATED_DIR / ".session_key"
    try:
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        for _ in range(20):   # un autre worker peut être en train de l'écrire
            key = path.read_text().strip()
            if len(key) >= 32:
                return key, "file"
            __import__("time").sleep(0.05)
    except OSError:
        pass
    return secrets.token_hex(32), "ephemeral"


app.config["SECRET_KEY"], _SECRET_SOURCE = _session_secret()
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
# Cookie de session jamais envoyé avec une requête POST venue d'un autre site : une page
# tierce ne peut pas agir à la place d'un utilisateur connecté (CSRF). Inaccessible au
# JavaScript. `SESSION_COOKIE_SECURE=1` (HTTPS uniquement) à activer derrière un proxy TLS.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE") == "1"


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    # Les pages ne s'affichent dans un cadre que depuis l'application elle-même (graphe).
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=15552000")
    # Pages et réponses d'API contiennent des données de dossier : jamais en cache partagé.
    if request.endpoint != "static" and "Cache-Control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _trusted_hosts() -> set[str]:
    hosts = {request.host.lower()}
    fwd = request.headers.get("X-Forwarded-Host", "")
    hosts.update(h.strip().lower() for h in fwd.split(",") if h.strip())
    extra = os.environ.get("TRUSTED_ORIGINS", "")
    hosts.update(h.strip().lower().split("://")[-1].rstrip("/") for h in extra.split(",") if h.strip())
    return hosts


@app.before_request
def _block_cross_site_writes():
    """Refuse toute requête modifiante émise par un autre site (CSRF).

    Le navigateur indique l'origine de la requête : `Sec-Fetch-Site` (HTTPS et localhost),
    sinon `Origin`. Un client sans navigateur (script, curl) n'envoie ni l'un ni l'autre
    et n'est pas concerné : il ne transporte pas le cookie d'un utilisateur à son insu."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    site = (request.headers.get("Sec-Fetch-Site") or "").lower()
    if site in ("same-origin", "none"):
        return None
    origin = request.headers.get("Origin")
    if site == "cross-site":
        blocked = True
    elif origin:
        from urllib.parse import urlparse
        blocked = origin == "null" or urlparse(origin).netloc.lower() not in _trusted_hosts()
    else:
        blocked = False
    if not blocked:
        return None
    if request.path.startswith(request.script_root + "/api/"):
        return jsonify({"error": "Requête refusée : origine non autorisée."}), 403
    return "Requête refusée : origine non autorisée.", 403

_STATIC_VERSION = str(int(os.environ.get("BUILD_TIME", __import__("time").time())))

@app.context_processor
def inject_static_version():
    return {"sv": _STATIC_VERSION, "app_version": APP_VERSION}


@app.template_filter("datefr")
def _datefr(ts) -> str:
    """Horodatage Unix -> « 21/11/2026 »."""
    import time as _time
    try:
        return _time.strftime("%d/%m/%Y", _time.localtime(float(ts))) if ts else ""
    except (TypeError, ValueError):
        return ""


# Macro d'icônes disponible dans tous les gabarits (y compris les blocs des gabarits enfants).
app.jinja_env.globals["icon"] = app.jinja_env.get_template("_icons.html").module.icon
if _SECRET_SOURCE == "ephemeral":
    print("[auth] Clé de session éphémère (dossier de données non inscriptible) : "
          "les connexions ne survivront pas à un redémarrage. Définissez FLASK_SECRET_KEY.")

db_init.ensure_all(reset_stale=True)


@app.before_request
def _refresh_settings():
    # Réglages enregistrés par un autre worker/processus : pris en compte sans redémarrage.
    cfg.refresh_if_changed()


@app.before_request
def _bind_active_case():
    """Dossier actif de la requête : paramètre `case_id`, puis en-tête `X-Case-Id`
    (posé par l'interface, propre à l'onglet), puis session, puis dossier par défaut.
    Un client d'API qui n'envoie rien travaille donc sur le dossier par défaut."""
    chosen = case_state.DEFAULT
    for cand in (request.args.get("case_id"), request.headers.get("X-Case-Id"), session.get("case_id")):
        if cand and case_state.exists(cand):
            chosen = cand
            break
    case_state.set_current(chosen)


@app.context_processor
def inject_cases():
    if not current_user.is_authenticated:
        return {}
    active = case_state.get_case(case_state.active_case_id()) or case_state.get_case(case_state.DEFAULT)
    return {
        "active_case": active,
        "active_case_id": active["case_id"],
        "cases": case_state.list_cases(),
        # Emails en attente de tri (les administrateurs voient aussi les dossiers proposés).
        "inbox_count": mailbox_state.count_open(include_admin=current_user.role == "admin"),
        "mailbox_enabled": cfg.imap_enabled,
        "mailbox_address": cfg.mailbox_address,
    }


def _in_case_stream(gen):
    """Garde le dossier actif pendant toute la durée d'une réponse en flux."""
    case_id = case_state.active_case_id()

    def wrapped():
        with case_state.use_case(case_id):
            yield from gen
    return wrapped()


# Premier démarrage (aucun compte) :
#  - ADMIN_PASSWORD fourni (installation scriptée) -> le compte admin est créé, comme avant ;
#  - sinon -> l'assistant d'installation (/setup) crée le compte, protégé par un code
#    à usage unique affiché ici, dans les journaux du serveur.
if os.environ.get("ADMIN_PASSWORD"):
    auth_state.ensure_admin_bootstrap(
        admin_username=os.environ.get("ADMIN_USERNAME", "admin"),
        admin_password=os.environ.get("ADMIN_PASSWORD"),
    )
elif auth_state.count_users() == 0:
    print(
        "\n"
        "==================================================================\n"
        "  Première installation : ouvrez l'application dans un navigateur\n"
        f"  et saisissez ce code d'installation : {auth_state.setup_code()}\n"
        "==================================================================\n",
        flush=True,
    )

login_manager = LoginManager()
login_manager.login_view = "login_page"
login_manager.init_app(app)

@login_manager.unauthorized_handler
def unauthorized():
    if auth_state.count_users() == 0 and not request.path.startswith("/api/"):
        return redirect(url_for("setup_page"))
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
    if auth_state.count_users() == 0:
        return redirect(url_for("setup_page"))
    next_url = _safe_next(request.values.get("next"))
    if current_user.is_authenticated:
        return redirect(next_url or url_for("home_page"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        wait = auth_state.login_retry_after(username, request.remote_addr)
        if wait:
            minutes = max(1, round(wait / 60))
            error = f"Trop de tentatives. Réessayez dans {minutes} minute{'s' if minutes > 1 else ''}."
            resp = make_response(render_template("login.html", error=error, next_url=next_url), 429)
            resp.headers["Retry-After"] = str(wait)
            return resp
        row = auth_state.verify_password(username, password)
        if row:
            auth_state.clear_login_failures(username)
            session.clear()   # nouvelle session à la connexion (pas de reprise d'un identifiant antérieur)
            login_user(User(row))
            return redirect(next_url or url_for("home_page"))
        auth_state.record_login_failure(username, request.remote_addr)
        error = "Identifiants invalides."
    return render_template("login.html", error=error, next_url=next_url)


def _safe_next(target: str | None) -> str | None:
    """N'accepte qu'une redirection interne à l'application (pas de redirection ouverte)."""
    if not target:
        return None
    from urllib.parse import urlparse
    u = urlparse(target)
    if u.scheme or u.netloc:
        # URL absolue : acceptée seulement si elle pointe sur notre propre hôte.
        if u.netloc != request.host:
            return None
        target = u.path + (("?" + u.query) if u.query else "")
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    return target


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("login_page"))


# --------------------------------------------------------------------------- #
# Mot de passe (libre-service) + changement imposé aux comptes neufs/réinitialisés
# --------------------------------------------------------------------------- #
_PASSWORD_MIN_LENGTH = 10
_PASSWORD_EXEMPT_ENDPOINTS = {"account_password", "logout", "login_page", "static"}


# --------------------------------------------------------------------------- #
# Assistant d'installation (premier démarrage)
# --------------------------------------------------------------------------- #
_SETUP_STEPS = ("admin", "storage", "models", "mailbox", "case", "done")


def _setup_step() -> str:
    """Étape courante de l'installation. Une installation existante (des comptes, pas
    de trace d'assistant) est considérée comme terminée : elle ne voit jamais l'assistant."""
    if auth_state.count_users() == 0:
        return "admin"
    step = state.get_meta("setup_step")
    return step if step in _SETUP_STEPS else "done"


@app.route("/setup")
def setup_page():
    step = _setup_step()
    if step == "done":
        abort(404)
    if step != "admin":
        if not current_user.is_authenticated:
            return redirect(url_for("login_page", next=url_for("setup_page")))
        if current_user.role != "admin":
            abort(403)
    from rag.index import CHUNKS_COLLECTION, get_client
    has_vectors = False
    if step != "admin":
        try:
            has_vectors = get_client().get_collection(CHUNKS_COLLECTION).count() > 0
        except Exception:
            has_vectors = False
    return render_template(
        "setup.html", bare_layout=True, step=step, steps=_SETUP_STEPS[:-1],
        settings=cfg.public_dict() if step != "admin" else None,
        has_vectors=has_vectors,
        default_case=case_state.get_case(case_state.DEFAULT) if step != "admin" else None,
        min_length=_PASSWORD_MIN_LENGTH,
    )


@app.route("/api/setup/admin", methods=["POST"])
def api_setup_admin():
    """Crée le premier compte administrateur. Possible une seule fois, avec le code
    d'installation affiché dans les journaux du serveur."""
    import time as _time
    if auth_state.count_users() > 0:
        abort(404)
    payload = request.get_json(silent=True) or {}
    if not auth_state.check_setup_code(payload.get("code") or ""):
        _time.sleep(1.0)      # freine les essais en rafale
        return jsonify({"error": "Code d'installation incorrect. Il figure dans les journaux du serveur."}), 403
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,40}", username):
        return jsonify({"error": "Identifiant : 3 à 40 caractères (lettres, chiffres, point, tiret)."}), 400
    if len(password) < _PASSWORD_MIN_LENGTH:
        return jsonify({"error": f"Le mot de passe doit compter au moins {_PASSWORD_MIN_LENGTH} caractères."}), 400
    if password != (payload.get("confirm") or ""):
        return jsonify({"error": "Les deux saisies du mot de passe ne correspondent pas."}), 400
    # Garde-fou contre deux créations simultanées : un seul appel obtient le jeton.
    if not state.set_meta_if_absent("setup_admin_created", username):
        abort(404)
    user_id = auth_state.create_user(username, password, role="admin", created_by="setup")
    auth_state.clear_must_change_password(user_id)
    auth_state.clear_setup_code()
    state.set_meta("setup_step", "storage")
    login_user(User(auth_state.get_user_by_id(user_id)))
    return jsonify({"status": "created", "next": "storage"})


@app.route("/api/setup/step", methods=["POST"])
@admin_required
def api_setup_step():
    """Mémorise l'avancement : l'assistant peut être quitté puis repris."""
    if _setup_step() in ("admin", "done"):
        abort(404)
    step = ((request.get_json(silent=True) or {}).get("step") or "").strip()
    if step not in _SETUP_STEPS or step == "admin":
        return jsonify({"error": "Étape inconnue."}), 400
    state.set_meta("setup_step", step)
    return jsonify({"step": step})


@app.before_request
def _enforce_password_change():
    if not current_user.is_authenticated or not current_user.must_change_password:
        return None
    if request.endpoint in _PASSWORD_EXEMPT_ENDPOINTS:
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "password_change_required"}), 403
    return redirect(url_for("account_password"))


@app.route("/account/password", methods=["GET", "POST"])
@login_required
def account_password():
    error, success = None, False
    forced = current_user.must_change_password
    if request.method == "POST":
        current = request.form.get("current") or ""
        new = request.form.get("new") or ""
        confirm = request.form.get("confirm") or ""
        if not auth_state.verify_password(current_user.username, current):
            error = "Le mot de passe actuel est incorrect."
        elif len(new) < _PASSWORD_MIN_LENGTH:
            error = f"Le nouveau mot de passe doit compter au moins {_PASSWORD_MIN_LENGTH} caractères."
        elif new != confirm:
            error = "Les deux saisies du nouveau mot de passe ne correspondent pas."
        elif new == current:
            error = "Le nouveau mot de passe doit être différent de l'actuel."
        else:
            auth_state.set_password(current_user.id, new, must_change=False)
            if forced:
                return redirect(url_for("home_page"))
            success = True
    return render_template(
        "account_password.html", error=error, success=success, forced=forced,
        bare_layout=forced, min_length=_PASSWORD_MIN_LENGTH,
    )


# --------------------------------------------------------------------------- #
# Pages d'erreur (JSON pour l'API, gabarit sinon)
# --------------------------------------------------------------------------- #
_ERROR_PAGES = {
    403: ("forbidden", "Accès réservé", "Cette page est réservée aux administrateurs."),
    404: ("not_found", "Page introuvable", "Cette adresse ne correspond à aucune page."),
    500: ("server_error", "Erreur interne", "Une erreur est survenue. Réessayez dans un instant."),
}


def _error_response(code: int):
    slug, title, message = _ERROR_PAGES[code]
    if request.path.startswith("/api/"):
        return jsonify({"error": slug}), code
    return render_template("error.html", title=title, message=message), code


@app.errorhandler(403)
def _err_403(_e):
    return _error_response(403)


@app.errorhandler(404)
def _err_404(_e):
    return _error_response(404)


@app.errorhandler(500)
def _err_500(_e):
    return _error_response(500)


# --------------------------------------------------------------------------- #
# Accueil (tableau de bord)
# --------------------------------------------------------------------------- #
def _case_ui() -> dict:
    """Libellés, exemples et prompts suggérés du dossier actif."""
    return case_state.ui_for()


def _home_stats(docs: list[dict]) -> dict:
    mails = [d for d in docs if d.get("type") == "mail"]
    dates = sorted(d["date"][:10] for d in mails if d.get("date") and not d["date"].startswith("1970"))
    return {
        "n_mails": len(mails),
        "n_attachments": sum(1 for d in docs if d.get("type") == "attachment"),
        "n_threads": len({d.get("thread_id") or d["doc_id"] for d in mails}),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
    }


def _system_health() -> list[dict]:
    """État du système affiché aux administrateurs sur l'accueil."""
    import time as _time
    items = []
    ok = heartbeat_ok()
    items.append({"label": "Base vectorielle (ChromaDB)", "state": "ok" if ok else "ko",
                  "value": f"{cfg.chroma_host}:{cfg.chroma_port}" + ("" if ok else " — injoignable")})
    st = state.get_status()
    if st.get("running"):
        items.append({"label": "Indexation", "state": "warn-dot",
                      "value": f"en cours ({st.get('current') or 0}/{st.get('total') or 0})"})
    else:
        when = st.get("finished_at")
        label = _time.strftime("%d/%m/%Y %H:%M", _time.localtime(when)) if when else "jamais lancée"
        phase = st.get("phase") or ""
        bad = phase in ("échec", "interrompu")
        items.append({"label": "Dernière indexation", "state": "ko" if bad else ("ok" if when else ""),
                      "value": f"{label}" + (f" — {phase}" if bad else "")})
    providers = _available_providers()
    items.append({"label": "Fournisseurs de modèles", "state": "ok" if providers else "ko",
                  "value": ", ".join(providers) if providers else "aucun configuré"})
    items.append({"label": "Modèle d'embedding", "state": "",
                  "value": f"{cfg.embed_provider} / {cfg.embed_model}"})
    if _SECRET_SOURCE == "ephemeral":
        items.append({"label": "Clé de session (FLASK_SECRET_KEY)", "state": "warn-dot",
                      "value": "éphémère : les connexions ne survivent pas à un redémarrage"})
    return items


@app.route("/accueil")
@login_required
def home_page():
    docs = state.list_documents(case_id=case_state.query_filter())
    mails = [d for d in docs if d.get("type") == "mail"]
    recent = sorted(mails, key=lambda d: d.get("date") or "", reverse=True)[:6]
    return render_template(
        "home.html",
        case_name=(case_state.get_case(case_state.active_case_id()) or {}).get("name")
        or case_defaults.DEFAULT_CASE_NAME,
        stats=_home_stats(docs),
        recent=recent,
        health=_system_health() if current_user.role == "admin" else [],
        setup_pending=current_user.role == "admin" and _setup_step() != "done",
    )


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
    docs = state.list_documents(case_id=case_state.query_filter())
    timeline_html = build_timeline_html(docs)
    emails = [d for d in docs if d.get("type") == "mail"]
    return render_template(
        "timeline.html",
        timeline_html=timeline_html,
        emails=emails,
        n_docs=len(docs),
        chroma_ok=heartbeat_ok(),
    )


def _graph_data():
    docs = state.list_documents(case_id=case_state.query_filter())
    with graph_state._conn() as c:
        edges = [dict(r) for r in c.execute(
            "SELECT src, dst, rel, MAX(weight) AS weight FROM doc_edges GROUP BY src, dst, rel"
        ).fetchall()]
    # Seules les relations internes au dossier affiché comptent.
    ids = {d["doc_id"] for d in docs}
    edges = [e for e in edges if e["src"] in ids and e["dst"] in ids]
    return docs, edges


@app.route("/graph")
@login_required
def graph_page():
    docs, edges = _graph_data()
    return render_template("graph.html", has_graph=bool(docs), n_nodes=len(docs), n_edges=len(edges))


@app.route("/graph/embed")
@login_required
def graph_embed():
    """Document autonome du graphe (pyvis), affiché dans un iframe par /graph : ses
    feuilles de style (Bootstrap) restent ainsi confinées au cadre."""
    docs, edges = _graph_data()
    html = build_network_html(docs, edges) if docs else "<!doctype html><title>Graphe</title>"
    resp = Response(html, mimetype="text/html")
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp


@app.route("/synthesis")
@login_required
def synthesis_page():
    return render_template(
        "synthesis.html",
        available_providers=_available_providers(),
        chat_provider=cfg.chat_provider,
        default_chat_model=_resolved_chat_model(),
        **_case_ui(),
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
        stream_with_context(_in_case_stream(generate())),
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
        default_chat_model=_resolved_chat_model(),
        **_case_ui(),
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
        stream_with_context(_in_case_stream(generate())),
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
        default_chat_model=_resolved_chat_model(),
        **_case_ui(),
    )


@app.route("/aide")
@login_required
def aide_page():
    return render_template("aide.html")


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
_UPLOAD_MAX_BYTES = 200 * 1024 * 1024  # 200 Mo par fichier


@app.route("/api/upload", methods=["POST"])
@admin_required
def api_upload():
    """Dépose un ou plusieurs fichiers .eml dans SOURCE_DIR/uploads/.

    Retourne la liste des fichiers acceptés et refusés.
    """
    # Dossier de destination : sans `case_id`, l'emplacement historique (dossier par défaut).
    target_case = (request.form.get("case_id") or "").strip()
    if target_case and target_case != case_state.DEFAULT:
        if not case_state.exists(target_case):
            return jsonify({"error": "Dossier inconnu."}), 400
        uploads_dir = case_state.case_dir(target_case) / "uploads"
    else:
        uploads_dir = UPLOADS_DIR
    uploads_dir.mkdir(parents=True, exist_ok=True)
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "Aucun fichier reçu."}), 400

    accepted: list[str] = []
    rejected: list[dict] = []

    # Build hash index of existing .eml files — streamed 64 KB at a time, never full file in RAM
    _CHUNK = 65536
    existing_hashes: set[str] = set()
    for existing in SOURCE_DIR.rglob("*.eml"):
        try:
            h = hashlib.sha256()
            with existing.open("rb") as fh:
                while chunk := fh.read(_CHUNK):
                    h.update(chunk)
            existing_hashes.add(h.hexdigest())
        except OSError:
            pass
    # Also track hashes of files accepted within this batch (dedup within the upload itself)
    batch_hashes: set[str] = set()

    for f in files:
        name = f.filename or ""
        suffix = Path(name).suffix.lower()
        if suffix not in _UPLOAD_ALLOWED:
            rejected.append({"name": name, "reason": f"Extension {suffix!r} non supportée (attendu : .eml)"})
            continue
        # Safe destination filename
        safe_name = Path(name).name
        safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in safe_name)
        dest = uploads_dir / safe_name
        if dest.exists():
            base, ext = dest.stem, dest.suffix
            for n in range(1, 999):
                candidate = uploads_dir / f"{base}_{n}{ext}"
                if not candidate.exists():
                    dest = candidate
                    break
        # Stream incoming file to disk while hashing — never loads whole file into RAM
        tmp = dest.with_suffix(".tmp")
        try:
            h = hashlib.sha256()
            size = 0
            with tmp.open("wb") as out:
                while chunk := f.stream.read(_CHUNK):
                    size += len(chunk)
                    if size > _UPLOAD_MAX_BYTES:
                        break
                    h.update(chunk)
                    out.write(chunk)
            if size > _UPLOAD_MAX_BYTES:
                tmp.unlink(missing_ok=True)
                rejected.append({"name": name, "reason": "Fichier trop volumineux (max 200 Mo)"})
                continue
            digest = h.hexdigest()
            if digest in existing_hashes or digest in batch_hashes:
                tmp.unlink(missing_ok=True)
                rejected.append({"name": name, "reason": "Fichier déjà présent (doublon ignoré)"})
                continue
            tmp.rename(dest)
            batch_hashes.add(digest)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            rejected.append({"name": name, "reason": f"Erreur d'écriture : {exc}"})
            continue
        accepted.append(dest.name)

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
    force_reindex = bool(payload.get("force"))
    started = indexer.start_async(
        enrich_provider=enrich_provider, enrich_model=enrich_model, force=force_reindex
    )
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
def _resolved_chat_model() -> str:
    model = cfg.chat_model or ""
    if model.lower() == "auto" or not model:
        provider = (cfg.chat_provider or "").lower()
        if provider == "mistral":
            return cfg.mistral_chat_model or "mistral-small-latest"
    return model


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
@admin_required
def eval_page():
    return render_template(
        "eval.html",
        available_providers=_available_providers(),
        chroma_ok=heartbeat_ok(),
    )


_KEY_FRAGMENT_RE = re.compile(r"\b[\w-]*\*{2,}[\w-]*\b|\bsk-[\w-]{6,}\b")


def _sanitize_provider_error(message: str) -> str:
    """Retire toute sous-chaîne ressemblant à une clé API (même partiellement
    masquée) des messages d'erreur remontés par les SDK provider avant de les
    renvoyer au client — certains SDK (OpenAI) incluent un fragment de la clé
    dans le message d'erreur 401."""
    return _KEY_FRAGMENT_RE.sub("[clé masquée]", message)


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
            _CHAT_PREFIXES = ("mistral-", "open-mistral", "open-mixtral", "codestral", "ministral", "pixtral", "devstral")
            models = sorted(
                [m.id for m in (resp.data or []) if any(m.id.startswith(p) for p in _CHAT_PREFIXES)],
                reverse=True,
            )
        elif provider == "vllm":
            from openai import OpenAI
            base_url = cfg.vllm_chat_base_url or cfg.vllm_base_url
            if base_url:
                client = OpenAI(api_key=cfg.vllm_api_key or "EMPTY", base_url=base_url)
                data = client.models.list().data
                models = [m.id for m in data]
    except Exception as exc:
        return [], _sanitize_provider_error(str(exc))
    return models, None


@app.route("/api/eval/models/<provider>")
@admin_required
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
@admin_required
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
@admin_required
def api_eval_gen_status():
    return jsonify(eval_state.get_gen_status())


@app.route("/api/eval/gen_cancel", methods=["POST"])
@admin_required
def api_eval_gen_cancel():
    from eval.generator import cancel as gen_cancel
    gen_cancel()
    return jsonify({"status": "cancelling"})


@app.route("/api/eval/questions", methods=["GET"])
@admin_required
def api_eval_questions_list():
    category = request.args.get("category")
    difficulty = request.args.get("difficulty")
    return jsonify(eval_state.list_questions(category=category, difficulty=difficulty))


@app.route("/api/eval/questions", methods=["POST"])
@admin_required
def api_eval_question_add():
    payload = request.get_json(force=True) or {}
    if not payload.get("question") or not payload.get("expected"):
        return jsonify({"error": "question et expected requis"}), 400
    q_id = eval_state.insert_question(payload)
    return jsonify({"id": q_id})


@app.route("/api/eval/questions/<int:q_id>", methods=["PUT"])
@admin_required
def api_eval_question_update(q_id: int):
    payload = request.get_json(force=True) or {}
    eval_state.update_question(q_id, payload)
    return jsonify({"status": "updated"})


@app.route("/api/eval/questions/<int:q_id>", methods=["DELETE"])
@admin_required
def api_eval_question_delete(q_id: int):
    eval_state.delete_question(q_id)
    return jsonify({"status": "deleted"})


@app.route("/api/eval/questions/clear", methods=["DELETE"])
@admin_required
def api_eval_questions_clear():
    n = eval_state.clear_questions()
    return jsonify({"deleted": n})


@app.route("/api/eval/export")
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
def api_eval_run_status(run_id: str):
    run = eval_state.get_run(run_id)
    if not run:
        abort(404)
    return jsonify(run)


@app.route("/api/eval/run_cancel/<run_id>", methods=["POST"])
@admin_required
def api_eval_run_cancel(run_id: str):
    from eval.judge import cancel_run
    ok = cancel_run(run_id)
    return jsonify({"status": "cancelling" if ok else "not_found"})


@app.route("/api/eval/results/<run_id>")
@admin_required
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
@admin_required
def api_eval_runs():
    return jsonify(eval_state.list_runs())


@app.route("/api/eval/download/<run_id>")
@admin_required
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


# Modèles d'embedding proposés par provider (page Réglages). Le modèle courant
# reste sélectionnable même s'il n'est pas listé (index existant).
EMBED_MODELS = {
    "local": ["intfloat/multilingual-e5-large", "BAAI/bge-m3"],
    "openai": ["text-embedding-3-large", "text-embedding-3-small"],
    "mistral": ["mistral-embed"],
    "vllm": ["intfloat/multilingual-e5-large", "BAAI/bge-m3"],
}


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
        embed_models=EMBED_MODELS,
    )


@app.route("/api/settings", methods=["GET"])
@admin_required
def api_settings_get():
    return jsonify(cfg.public_dict())


# Champs texte (non secrets) modifiables via l'UI.
_SETTINGS_TEXT_FIELDS = (
    "embed_provider", "chat_provider", "embed_model", "chat_model",
    "mistral_chat_model", "vllm_base_url", "docling_url", "vision_url",
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
    if provider == "docling":
        return jsonify(_test_docling())
    if provider == "vision":
        return jsonify(_test_vision())
    if provider == "local":
        return jsonify(_test_local_embedding())
    models, err = _fetch_models_for_provider(provider)
    if err:
        return jsonify({"ok": False, "error": err})
    return jsonify({"ok": True, "n_models": len(models),
                    "sample": models[:5]})


def _test_docling() -> dict:
    url = (cfg.docling_url or "").rstrip("/")
    if not url:
        return {"ok": False, "error": "Docling désactivé (adresse vide)."}
    try:
        import httpx
        r = httpx.get(url + "/health", timeout=5.0)
        r.raise_for_status()
        return {"ok": True, "message": "Docling répond."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Docling injoignable ({type(exc).__name__}) — poste de travail en veille ?"}


def _test_vision() -> dict:
    url = (cfg.vision_url or "").rstrip("/")
    if not url:
        return {"ok": False, "error": "Lecture vision désactivée (adresse vide)."}
    try:
        import httpx
        r = httpx.get(url + "/models", timeout=5.0,
                      headers={"Authorization": f"Bearer {cfg.vision_api_key}"} if cfg.vision_api_key else {})
        r.raise_for_status()
        ids = [m.get("id") for m in r.json().get("data", [])]
        return {"ok": True, "message": "Modèle vision prêt : " + (", ".join(i for i in ids if i) or "?")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Modèle vision injoignable ({type(exc).__name__}) — poste de travail en veille ?"}


def _test_local_embedding() -> dict:
    """Embede un mot avec chaque modèle local configuré."""
    from dataclasses import replace
    from rag.providers import get_embedder
    results, ok = [], True
    for model in sorted(cfg.local_embed_urls):
        try:
            vec = get_embedder(replace(cfg, embed_provider="local", embed_model=model)).embed(["test"])[0]
            results.append(f"{model.split('/')[-1]} : OK ({len(vec)} dim.)")
        except Exception as exc:  # noqa: BLE001
            ok = False
            results.append(f"{model.split('/')[-1]} : injoignable ({type(exc).__name__})")
    if not results:
        return {"ok": False, "error": "Aucun modèle local configuré (EMBED_URL_E5 / EMBED_URL_BGE_M3)."}
    return {"ok": ok, "message": " · ".join(results), "error": None if ok else " · ".join(results)}


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


# --------------------------------------------------------------------------- #
# Dossiers
# --------------------------------------------------------------------------- #
def _case_public(case: dict, counts: dict | None = None) -> dict:
    out = {k: case.get(k) for k in ("case_id", "name", "description", "context_md", "status", "is_default")}
    if counts is not None:
        out["n_mails"] = counts.get(case["case_id"], 0)
    return out


@app.route("/api/cases", methods=["GET"])
@login_required
def api_cases_list():
    counts = case_state.counts_by_case()
    include_archived = current_user.role == "admin" and request.args.get("all") == "1"
    return jsonify({
        "active": case_state.active_case_id(),
        "cases": [_case_public(c, counts) for c in case_state.list_cases(include_archived)],
    })


@app.route("/api/cases/active", methods=["POST"])
@login_required
def api_cases_set_active():
    payload = request.get_json(silent=True) or {}
    case_id = (payload.get("case_id") or "").strip()
    if not case_state.exists(case_id):
        return jsonify({"error": "Dossier inconnu."}), 404
    session["case_id"] = case_id
    return jsonify({"active": case_id})


@app.route("/cases")
@admin_required
def cases_page():
    counts = case_state.counts_by_case()
    cases = [_case_public(c, counts) for c in case_state.list_cases(include_archived=True)]
    for c in cases:
        c["participants"] = case_state.list_participants(c["case_id"])
    return render_template("cases.html", all_cases=cases, vectors_tagged=case_state.vectors_tagged())


@app.route("/api/cases", methods=["POST"])
@admin_required
def api_cases_create():
    payload = request.get_json(force=True) or {}
    # Premier dossier supplémentaire : les vecteurs existants doivent d'abord être
    # rattachés au dossier par défaut, sinon les recherches filtrées les ignoreraient.
    if not case_state.vectors_tagged():
        try:
            import migrate
            migrate.backfill_case_metadata()
        except Exception as exc:
            return jsonify({"error": "Préparation de l'index impossible : "
                            + _sanitize_provider_error(str(exc))}), 503
    try:
        case = case_state.create_case(
            name=payload.get("name") or "",
            description=payload.get("description") or "",
            context_md=payload.get("context_md") or "",
            case_id=payload.get("case_id") or None,
            created_by=current_user.username,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_case_public(case)), 201


@app.route("/api/cases/<case_id>", methods=["PUT"])
@admin_required
def api_cases_update(case_id: str):
    if not case_state.get_case(case_id):
        return jsonify({"error": "Dossier inconnu."}), 404
    payload = request.get_json(force=True) or {}
    try:
        case = case_state.update_case(
            case_id,
            name=payload.get("name"),
            description=payload.get("description"),
            context_md=payload.get("context_md"),
            status=payload.get("status"),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if case["status"] != "active" and session.get("case_id") == case_id:
        session.pop("case_id", None)
    return jsonify(_case_public(case))


@app.route("/api/cases/<case_id>/participants", methods=["POST"])
@admin_required
def api_cases_participant_add(case_id: str):
    if not case_state.get_case(case_id):
        return jsonify({"error": "Dossier inconnu."}), 404
    payload = request.get_json(force=True) or {}
    try:
        added = case_state.add_participant(
            case_id, (payload.get("kind") or "email").strip(), payload.get("value") or "",
            label=payload.get("label") or "", role=payload.get("role") or "",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not added:
        return jsonify({"error": "Ce participant est déjà rattaché au dossier."}), 409
    return jsonify({"participants": case_state.list_participants(case_id)}), 201


@app.route("/api/cases/<case_id>/participants/<int:participant_id>", methods=["DELETE"])
@admin_required
def api_cases_participant_delete(case_id: str, participant_id: int):
    case_state.remove_participant(participant_id, case_id)
    return jsonify({"participants": case_state.list_participants(case_id)})


# --------------------------------------------------------------------------- #
# Boîte de collecte : tri des emails reçus
# --------------------------------------------------------------------------- #
_INBOX_VIEWS = {
    "open": ("pending", "needs_admin"),
    "done": ("filed", "indexed", "rejected", "duplicate"),
    "quarantine": ("quarantined", "error"),
}


def _inbox_public(item: dict) -> dict:
    keys = ("id", "received_at", "forwarder", "forwarder_note", "sender_verified", "orig_from", "orig_to",
            "orig_date", "subject", "preview", "n_attachments", "unwrap_mode", "status",
            "suggested_case_id", "confidence", "deterministic", "reasons", "candidates",
            "proposed_case_name", "case_id", "decided_by", "decided_at", "error", "doc_id")
    return {k: item.get(k) for k in keys}


def _triage(action, *args):
    """Exécute une décision de tri et renvoie l'élément à jour (ou l'erreur métier)."""
    try:
        return jsonify(_inbox_public(action(*args)))
    except mail_intake.TriageError as exc:
        return jsonify({"error": str(exc)}), 409


@app.route("/inbox")
@login_required
def inbox_page():
    return render_template(
        "inbox.html",
        counts=mailbox_state.count_by_status(),
        inbox_cases=[{"case_id": c["case_id"], "name": c["name"]} for c in case_state.list_cases()],
    )


@app.route("/api/inbox")
@login_required
def api_inbox_list():
    view = request.args.get("view", "open")
    if view not in _INBOX_VIEWS:
        return jsonify({"error": "Vue inconnue."}), 400
    if view == "quarantine" and current_user.role != "admin":
        abort(403)
    items = mailbox_state.list_items(_INBOX_VIEWS[view])
    return jsonify({"items": [_inbox_public(i) for i in items], "counts": mailbox_state.count_by_status()})


@app.route("/api/inbox/<int:item_id>/file", methods=["POST"])
@login_required
def api_inbox_file(item_id: int):
    payload = request.get_json(silent=True) or {}
    return _triage(mail_intake.file_item, item_id, (payload.get("case_id") or "").strip(), current_user.username)


@app.route("/api/inbox/<int:item_id>/reject", methods=["POST"])
@login_required
def api_inbox_reject(item_id: int):
    return _triage(mail_intake.reject_item, item_id, current_user.username)


@app.route("/api/inbox/<int:item_id>/propose-case", methods=["POST"])
@login_required
def api_inbox_propose_case(item_id: int):
    payload = request.get_json(silent=True) or {}
    return _triage(mail_intake.propose_case, item_id, payload.get("name") or "", current_user.username)


@app.route("/api/inbox/<int:item_id>/approve-case", methods=["POST"])
@admin_required
def api_inbox_approve_case(item_id: int):
    """Crée le dossier proposé (ou saisi par l'administrateur) et y classe l'email."""
    item = mailbox_state.get_item(item_id)
    if not item or item["status"] not in mailbox_state.OPEN_STATUSES:
        return jsonify({"error": "Cet email a déjà été traité."}), 409
    payload = request.get_json(silent=True) or {}
    if not case_state.vectors_tagged():
        try:
            import migrate
            migrate.backfill_case_metadata()
        except Exception as exc:
            return jsonify({"error": "Préparation de l'index impossible : "
                            + _sanitize_provider_error(str(exc))}), 503
    try:
        case = case_state.create_case(
            name=payload.get("name") or item.get("proposed_case_name") or "",
            description=payload.get("description") or "",
            context_md=payload.get("context_md") or "",
            created_by=current_user.username,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return _triage(mail_intake.file_item, item_id, case["case_id"], current_user.username)


@app.route("/api/inbox/<int:item_id>/restore", methods=["POST"])
@admin_required
def api_inbox_restore(item_id: int):
    return _triage(mail_intake.restore_item, item_id, current_user.username)


@app.route("/api/mailbox/status")
@admin_required
def api_mailbox_status():
    return jsonify({
        "enabled": cfg.imap_enabled,
        "state": mailbox_state.get_state(),
        "counts": mailbox_state.count_by_status(),
        "log": mailbox_state.get_log(80),
        "allowed_senders": len(cfg.allowed_senders()),
    })


# --------------------------------------------------------------------------- #
# Réglages : boîte de collecte et base vectorielle
# --------------------------------------------------------------------------- #
_MAILBOX_FIELDS = (
    "imap_enabled", "imap_host", "imap_port", "imap_ssl", "imap_user", "imap_folder",
    "imap_poll_interval", "imap_processed_action", "imap_processed_folder", "mailbox_address",
    "mailbox_allowed_senders", "mailbox_require_auth_results", "mailbox_autofile",
    "mailbox_autofile_threshold",
)


def _mailbox_updates(payload: dict) -> tuple[dict, str | None]:
    updates = {k: payload[k] for k in _MAILBOX_FIELDS if k in payload and payload[k] is not None}
    # Mot de passe : un champ laissé vide conserve celui déjà enregistré.
    password = payload.get("imap_password")
    if isinstance(password, str) and password:
        updates["imap_password"] = password
    try:
        if "imap_port" in updates and not (0 < int(updates["imap_port"]) < 65536):
            return {}, "Port invalide."
        if "imap_poll_interval" in updates and int(updates["imap_poll_interval"]) < 30:
            return {}, "L'intervalle de relève est d'au moins 30 secondes."
        if "mailbox_autofile_threshold" in updates and not (0 <= float(updates["mailbox_autofile_threshold"]) <= 1):
            return {}, "Le seuil de confiance est compris entre 0 et 1."
    except (TypeError, ValueError):
        return {}, "Valeur numérique invalide."
    if updates.get("imap_processed_action") not in (None, "seen", "move"):
        return {}, "Action après collecte invalide."
    return updates, None


@app.route("/api/settings/mailbox", methods=["POST"])
@admin_required
def api_settings_mailbox():
    updates, err = _mailbox_updates(request.get_json(force=True) or {})
    if err:
        return jsonify({"error": err}), 400
    cfg.apply(updates)
    return jsonify({"status": "saved", "mailbox": cfg.mailbox_public()})


@app.route("/api/settings/mailbox/test", methods=["POST"])
@admin_required
def api_settings_mailbox_test():
    """Teste la connexion IMAP avec les valeurs du formulaire, SANS les enregistrer."""
    import dataclasses
    import mail_daemon
    updates, err = _mailbox_updates(request.get_json(force=True) or {})
    if err:
        return jsonify({"ok": False, "error": err})
    probe = dataclasses.replace(cfg)
    for k, v in updates.items():
        setattr(probe, k, probe._coerce(k, v))
    if not probe.imap_host or not probe.imap_user:
        return jsonify({"ok": False, "error": "Renseignez le serveur et l'identifiant."})
    if not probe.imap_password:
        return jsonify({"ok": False, "error": "Renseignez le mot de passe."})
    try:
        result = mail_daemon.imap_test(probe)
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}".replace(probe.imap_password, "***")
        return jsonify({"ok": False, "error": _sanitize_provider_error(msg)[:300]})
    return jsonify({"ok": True, "messages": result["messages"],
                    "warning": None if probe.allowed_senders() else
                    "Aucun expéditeur autorisé : tous les messages reçus iront en quarantaine."})


def _chroma_probe(payload: dict) -> dict:
    """Interroge une base ChromaDB (celle du formulaire, ou l'actuelle) sans rien modifier."""
    from rag.index import CHUNKS_COLLECTION, DOCUMENTS_COLLECTION, get_client
    host = (payload.get("host") or "").strip()
    auth = payload.get("auth_header") or None
    try:
        if host:
            client = get_client(host=host, port=int(payload.get("port") or 8000),
                                ssl=bool(payload.get("ssl")),
                                auth_header=auth if auth else cfg.chroma_auth_header)
        else:
            client = get_client()
        client.heartbeat()
        counts = {}
        for name in (DOCUMENTS_COLLECTION, CHUNKS_COLLECTION):
            try:
                counts[name] = client.get_collection(name).count()
            except Exception:
                counts[name] = 0          # collection pas encore créée : base vide
        try:
            version = client.get_version()
        except Exception:
            version = None
        return {"ok": True, "counts": counts, "version": version}
    except Exception as exc:
        return {"ok": False, "error": _sanitize_provider_error(f"{type(exc).__name__}: {exc}")[:300]}


@app.route("/api/settings/chroma/test", methods=["POST"])
@admin_required
def api_settings_chroma_test():
    return jsonify(_chroma_probe(request.get_json(silent=True) or {}))


@app.route("/api/settings/chroma", methods=["POST"])
@admin_required
def api_settings_chroma():
    """Enregistre l'adresse de la base vectorielle. `host` vide = retour au réglage du
    .env / docker-compose. Refuse une adresse injoignable (sauf `force`)."""
    payload = request.get_json(force=True) or {}
    host = (payload.get("host") or "").strip()
    if state.is_running():
        return jsonify({"error": "Une indexation est en cours : réessayez une fois celle-ci terminée."}), 409
    if not host:
        cfg.reset_fields(["chroma_host", "chroma_port", "chroma_ssl", "chroma_auth_header"])
        return jsonify({"status": "reset", "chroma": cfg.public_dict()["chroma"], "probe": _chroma_probe({})})
    try:
        port = int(payload.get("port") or 8000)
        if not (0 < port < 65536):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Port invalide."}), 400
    probe = _chroma_probe(payload)
    if not probe["ok"] and not payload.get("force"):
        return jsonify({"error": "Base injoignable à cette adresse : " + probe["error"], "probe": probe}), 400
    updates = {"chroma_host": host, "chroma_port": port, "chroma_ssl": bool(payload.get("ssl"))}
    if payload.get("auth_header"):
        updates["chroma_auth_header"] = payload["auth_header"]
    cfg.apply(updates)
    return jsonify({"status": "saved", "chroma": cfg.public_dict()["chroma"], "probe": probe})


@app.route("/api/settings/chroma/copy", methods=["POST"])
@admin_required
def api_settings_chroma_copy():
    """Copie les vecteurs de la base actuelle vers une autre (sans recalcul), sous le
    bail d'indexation. La base actuelle n'est pas modifiée."""
    import migrate
    payload = request.get_json(force=True) or {}
    host = (payload.get("host") or "").strip()
    if not host:
        return jsonify({"error": "Indiquez l'adresse de la base de destination."}), 400
    owner = state.new_lease_owner()
    if not state.acquire_lease(state.INDEX_LEASE, owner, kind="copy"):
        return jsonify({"error": "Une indexation est en cours : réessayez une fois celle-ci terminée."}), 409
    keeper = state.LeaseKeeper(state.INDEX_LEASE, owner).start()
    try:
        report = migrate.copy_vectors(host, int(payload.get("port") or 8000), ssl=bool(payload.get("ssl")),
                                      auth_header=payload.get("auth_header") or cfg.chroma_auth_header)
    except Exception as exc:
        return jsonify({"error": _sanitize_provider_error(f"{type(exc).__name__}: {exc}")[:300]}), 502
    finally:
        keeper.stop()
        state.release_lease(state.INDEX_LEASE, owner)
    return jsonify(report)


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
