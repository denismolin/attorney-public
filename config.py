"""Configuration centrale : lecture du .env et chemins du projet.

Toutes les valeurs sensibles (clés API) et les choix de provider passent par
les variables d'environnement (cf .env.example). Aucun défaut câblé pour le
provider — il est choisi au lancement.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Racine du projet (ce fichier est à la racine).
ROOT = Path(__file__).resolve().parent
APP_VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "0.0.0"
# data/ se scinde en deux racines indépendantes (configurables via .env) :
#   - SOURCE_DIR    : entrée fournie par l'utilisateur (emails "mails de */", uploads/).
#   - GENERATED_DIR : sorties techniques (PJ extraites, texte OCR, SQLite, settings.json).
# Les deux défaultent sous data/ pour ne rien casser en place, mais peuvent pointer
# n'importe où (ex. disques séparés). DATA_DIR reste défini comme parent commun par
# défaut, utilisé uniquement pour calculer ces défauts et pour de vieux chemins stockés
# en base (cf. app.py._resolve_path, parsing/attachments.py._abs_path).
DATA_DIR = ROOT / "data"
SOURCE_DIR = Path(os.environ.get("SOURCE_DIR") or (DATA_DIR / "sources"))
GENERATED_DIR = Path(os.environ.get("GENERATED_DIR") or (DATA_DIR / "generated"))

UPLOADS_DIR = SOURCE_DIR / "uploads"
ATTACHMENTS_DIR = GENERATED_DIR / "attachments"
EXTRACTED_TEXT_DIR = GENERATED_DIR / "extracted_text"
DB_PATH = GENERATED_DIR / "app.sqlite"
# Réglages saisis via l'UI (/settings). Surcharge le .env. Sous GENERATED_DIR → gitignoré
# et EXCLU de l'archive de migration (cf. migrate.py). Contient des secrets en clair.
SETTINGS_PATH = GENERATED_DIR / "settings.json"

# Champs pilotés par le menu /settings (providers, modèles, clés). L'infra
# (Chroma, Flask, chunking) reste réservée au .env / docker-compose.
MANAGED_FIELDS = (
    "embed_provider", "chat_provider", "embed_model", "chat_model", "mistral_chat_model",
    "openai_api_key", "anthropic_api_key", "mistral_api_key",
    "vllm_base_url", "vllm_chat_base_url", "vllm_embed_base_url", "vllm_api_key",
)
# Réglages enregistrés SEULEMENT s'ils ont été saisis dans l'UI. Contrairement aux
# champs ci-dessus, ils ne sont jamais figés dans settings.json par un enregistrement
# sans rapport : tant que l'UI n'y touche pas, le .env / docker-compose fait foi
# (ex. CHROMA_HOST=chromadb posé par docker-compose).
EXPLICIT_FIELDS = (
    "docling_url", "vision_url",
    "chroma_host", "chroma_port", "chroma_ssl", "chroma_auth_header",
    "imap_enabled", "imap_host", "imap_port", "imap_ssl", "imap_user", "imap_password",
    "imap_folder", "imap_poll_interval", "imap_processed_action", "imap_processed_folder",
    "mailbox_address", "mailbox_allowed_senders", "mailbox_require_auth_results",
    "mailbox_autofile", "mailbox_autofile_threshold", "mailbox_max_bytes",
)
# Secrets hors clés de provider : masqués en lecture, jamais renvoyés au navigateur.
SECRET_FIELDS = ("chroma_auth_header", "imap_password")
# Clés API : (champ_config, libellé provider) — masquées en lecture.
KEY_FIELDS = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "mistral": "mistral_api_key",
    "vllm": "vllm_api_key",
}

# Charge le .env s'il existe (sinon on s'appuie sur l'environnement réel,
# ce qui est le cas en conteneur via env_file).
load_dotenv(ROOT / ".env")


def _get(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    if val is not None:
        val = val.strip()
    return val or default


def _get_bool(name: str, default: bool = False) -> bool:
    val = _get(name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on", "oui")


@dataclass
class Config:
    # Providers
    embed_provider: str = field(default_factory=lambda: _get("EMBED_PROVIDER", "openai"))
    chat_provider: str = field(default_factory=lambda: _get("CHAT_PROVIDER", "openai"))
    embed_model: str = field(default_factory=lambda: _get("EMBED_MODEL", "text-embedding-3-large"))
    chat_model: str = field(default_factory=lambda: _get("CHAT_MODEL", "gpt-4o"))

    # Clés
    openai_api_key: str | None = field(default_factory=lambda: _get("OPENAI_API_KEY"))
    anthropic_api_key: str | None = field(default_factory=lambda: _get("ANTHROPIC_API_KEY"))
    mistral_api_key: str | None = field(default_factory=lambda: _get("MISTRAL_API_KEY"))

    # Modèle Mistral pour l'enrichissement à l'indexation (extraction structurée).
    # Mistral est économique mais rate-limité -> retry/backoff côté parsing/enrich.py.
    mistral_chat_model: str = field(
        default_factory=lambda: _get("MISTRAL_CHAT_MODEL", "mistral-small-latest")
    )

    # vLLM (OpenAI-compatible). Le service de l'utilisateur expose des base_url
    # DIFFÉRENTES pour le chat et l'embedding -> on les sépare. VLLM_BASE_URL
    # sert de fallback commun si l'une des deux n'est pas précisée.
    vllm_base_url: str | None = field(default_factory=lambda: _get("VLLM_BASE_URL"))
    vllm_chat_base_url: str | None = field(
        default_factory=lambda: _get("VLLM_CHAT_BASE_URL") or _get("VLLM_BASE_URL")
    )
    vllm_embed_base_url: str | None = field(
        default_factory=lambda: _get("VLLM_EMBED_BASE_URL") or _get("VLLM_BASE_URL")
    )
    # Token : VLLM_API_KEY sinon LOCAL_TOKEN (variable d'environnement de l'utilisateur).
    vllm_api_key: str | None = field(
        default_factory=lambda: _get("VLLM_API_KEY") or _get("LOCAL_TOKEN") or "EMPTY"
    )

    # Docling (extraction structurée + OCR local des PDF). Vide = désactivé.
    docling_url: str | None = field(default_factory=lambda: _get("DOCLING_URL"))
    # Lecture des scans manuscrits (modèle vision local, API OpenAI). Vide = désactivé.
    vision_url: str | None = field(default_factory=lambda: _get("VISION_URL"))
    vision_api_key: str | None = field(
        default_factory=lambda: _get("VISION_API_KEY") or _get("LOCAL_TOKEN")
    )
    # Embeddings « local » : services TEI de la pile (poste de travail), un URL par
    # modèle (TEI sert un seul modèle par URL). Posés par docker-compose.
    local_embed_urls: dict = field(default_factory=lambda: {
        k: v for k, v in {
            "intfloat/multilingual-e5-large": _get("EMBED_URL_E5"),
            "BAAI/bge-m3": _get("EMBED_URL_BGE_M3"),
        }.items() if v
    })

    # ChromaDB
    chroma_host: str = field(default_factory=lambda: _get("CHROMA_HOST", "localhost"))
    chroma_port: int = field(default_factory=lambda: int(_get("CHROMA_PORT", "8000")))
    # Base vectorielle sur une autre machine : HTTPS et en-tête d'authentification
    # (« Nom: valeur », ou un jeton seul envoyé en « Authorization: Bearer … »)
    # quand un reverse proxy la protège.
    chroma_ssl: bool = field(default_factory=lambda: _get_bool("CHROMA_SSL"))
    chroma_auth_header: str | None = field(default_factory=lambda: _get("CHROMA_AUTH_HEADER"))

    # Boîte mail de collecte (IMAP). Désactivée par défaut : sans réglage, rien ne change.
    imap_enabled: bool = field(default_factory=lambda: _get_bool("IMAP_ENABLED"))
    imap_host: str = field(default_factory=lambda: _get("IMAP_HOST", "") or "")
    imap_port: int = field(default_factory=lambda: int(_get("IMAP_PORT", "993")))
    imap_ssl: bool = field(default_factory=lambda: _get_bool("IMAP_SSL", True))
    imap_user: str = field(default_factory=lambda: _get("IMAP_USER", "") or "")
    imap_password: str | None = field(default_factory=lambda: _get("IMAP_PASSWORD"))
    imap_folder: str = field(default_factory=lambda: _get("IMAP_FOLDER", "INBOX"))
    imap_poll_interval: int = field(default_factory=lambda: int(_get("IMAP_POLL_INTERVAL", "300")))
    # Après collecte : "seen" (marquer comme lu) ou "move" (déplacer dans un dossier IMAP).
    imap_processed_action: str = field(default_factory=lambda: _get("IMAP_PROCESSED_ACTION", "seen"))
    imap_processed_folder: str = field(default_factory=lambda: _get("IMAP_PROCESSED_FOLDER", "Traites"))
    # Adresse affichée aux utilisateurs (« transférez vos emails à … »).
    mailbox_address: str = field(default_factory=lambda: _get("MAILBOX_ADDRESS", "") or "")
    # Expéditeurs autorisés à transférer (adresses ou @domaines, séparés par virgule/retour ligne).
    mailbox_allowed_senders: str = field(default_factory=lambda: _get("MAILBOX_ALLOWED_SENDERS", "") or "")
    # Exiger un résultat SPF/DKIM « pass » posé par le serveur de réception.
    mailbox_require_auth_results: bool = field(default_factory=lambda: _get_bool("MAILBOX_REQUIRE_AUTH_RESULTS"))
    # Classement automatique (sans relecture) : désactivé par défaut.
    mailbox_autofile: bool = field(default_factory=lambda: _get_bool("MAILBOX_AUTOFILE"))
    mailbox_autofile_threshold: float = field(default_factory=lambda: float(_get("MAILBOX_AUTOFILE_THRESHOLD", "0.85")))
    mailbox_max_bytes: int = field(default_factory=lambda: int(_get("MAILBOX_MAX_BYTES", str(30 * 1024 * 1024))))

    # Flask
    flask_host: str = field(default_factory=lambda: _get("FLASK_HOST", "0.0.0.0"))
    flask_port: int = field(default_factory=lambda: int(_get("FLASK_PORT", "5000")))

    # Indexation
    chunk_size: int = field(default_factory=lambda: int(_get("CHUNK_SIZE", "1400")))
    chunk_overlap: int = field(default_factory=lambda: int(_get("CHUNK_OVERLAP", "200")))
    # Garde-fou : tronque tout texte avant embedding (le modèle e5 limite à 512 tokens).
    embed_max_chars: int = field(default_factory=lambda: int(_get("EMBED_MAX_CHARS", "1600")))
    # Taille de batch max acceptée par le service d'embedding (e5/TEI = 8).
    embed_batch_size: int = field(default_factory=lambda: int(_get("EMBED_BATCH_SIZE", "8")))
    # Pause entre deux batchs d'embedding (throttle anti-crash de TEI).
    embed_batch_delay: float = field(default_factory=lambda: float(_get("EMBED_BATCH_DELAY", "0.3")))

    # mtime de settings.json lors de la dernière lecture (cf. refresh_if_changed).
    _loaded_mtime: int = field(default=0, init=False, repr=False, compare=False)

    def ensure_dirs(self) -> None:
        """Crée les répertoires source/générés s'ils n'existent pas."""
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
        EXTRACTED_TEXT_DIR.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------------- #
    # Réglages runtime (menu /settings) — surcouche persistée sur le .env.
    # ----------------------------------------------------------------------- #
    @staticmethod
    def _read_settings_file() -> dict:
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _settings_mtime() -> int:
        try:
            return SETTINGS_PATH.stat().st_mtime_ns
        except OSError:
            return 0

    def _coerce(self, key: str, val):
        """Ramène une valeur (souvent une chaîne venue du JSON/UI) au type du champ."""
        current = getattr(self, key, None)
        try:
            if isinstance(current, bool):
                if isinstance(val, str):
                    return val.strip().lower() in ("1", "true", "yes", "on", "oui")
                return bool(val)
            if isinstance(current, int):
                return int(val)
            if isinstance(current, float):
                return float(val)
        except (TypeError, ValueError):
            return current
        return val.strip() if isinstance(val, str) else val

    def load_overrides(self) -> None:
        """Applique les réglages de GENERATED_DIR/settings.json par-dessus le .env."""
        self._loaded_mtime = self._settings_mtime()
        if not self._loaded_mtime:
            return
        data = self._read_settings_file()
        for k in MANAGED_FIELDS + EXPLICIT_FIELDS:
            if k in data and data[k] is not None:
                setattr(self, k, self._coerce(k, data[k]))

    def refresh_if_changed(self) -> bool:
        """Recharge settings.json s'il a changé sur disque depuis la dernière lecture.

        Chaque processus (les workers gunicorn, le démon mail) a sa propre copie de
        `cfg` : sans cela, un réglage enregistré via l'UI n'atteindrait que le worker
        qui a servi la requête. Coût : un `stat` par appel.
        """
        if self._settings_mtime() == self._loaded_mtime:
            return False
        self.load_overrides()
        return True

    def apply(self, updates: dict) -> None:
        """Met à jour les champs gérés (en place → effet immédiat) et persiste.

        Les champs absents ou à `None` sont ignorés (un champ clé laissé vide côté UI
        ne doit pas effacer une clé existante). Aligne les base_url vLLM dérivées sur
        `vllm_base_url` quand il est (re)défini sans surcharge explicite.
        """
        # Repart de l'état sur disque : un autre processus a pu enregistrer entre-temps,
        # et notre copie en mémoire ne doit pas écraser ses changements.
        self.refresh_if_changed()
        for k in MANAGED_FIELDS + EXPLICIT_FIELDS:
            if k in updates and updates[k] is not None:
                setattr(self, k, self._coerce(k, updates[k]))
        # Fallback commun : si on change vllm_base_url sans préciser les variantes,
        # propage (même sémantique que les default_factory du .env).
        if updates.get("vllm_base_url"):
            base = self.vllm_base_url
            if not updates.get("vllm_chat_base_url"):
                self.vllm_chat_base_url = base
            if not updates.get("vllm_embed_base_url"):
                self.vllm_embed_base_url = base
        self._save_overrides(explicit=[k for k in EXPLICIT_FIELDS
                                       if k in updates and updates[k] is not None])

    def reset_fields(self, keys: list[str]) -> None:
        """Retire des réglages saisis dans l'UI : la valeur du .env / par défaut reprend."""
        keys = [k for k in keys if k in EXPLICIT_FIELDS]
        if not keys:
            return
        self.refresh_if_changed()
        fresh = Config()
        for k in keys:
            setattr(self, k, getattr(fresh, k))
        self._save_overrides(drop=keys)

    def _save_overrides(self, explicit: list[str] | None = None, drop: list[str] | None = None) -> None:
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        # Conserve les clés inconnues de cette version (fichier écrit par une version
        # plus récente), puis écriture atomique : jamais de fichier à moitié écrit.
        snapshot = self._read_settings_file()
        snapshot.update({k: getattr(self, k) for k in MANAGED_FIELDS})
        snapshot.update({k: getattr(self, k) for k in (explicit or [])})
        for k in drop or []:
            snapshot.pop(k, None)
        tmp = SETTINGS_PATH.with_name(SETTINGS_PATH.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_PATH)
        self._loaded_mtime = self._settings_mtime()

    @staticmethod
    def _mask(secret: str | None) -> dict:
        # "EMPTY" = sentinelle vLLM (aucun token réel) → considéré non configuré.
        if not secret or secret == "EMPTY":
            return {"configured": False, "hint": ""}
        tail = secret[-4:] if len(secret) >= 4 else "•" * len(secret)
        return {"configured": True, "hint": "••••" + tail}

    @staticmethod
    def _mask_opaque(secret: str | None) -> dict:
        """Masque total (mot de passe) : contrairement à une clé d'API, en révéler la fin
        n'aide pas à l'identifier et affaiblit un secret court."""
        return {"configured": bool(secret), "hint": "••••••" if secret else ""}

    def public_dict(self) -> dict:
        """Vue sûre de la config gérée (clés masquées) pour l'UI /settings.

        Le champ est nommé `key_status` (pas `keys`) : dans un template Jinja,
        `dict.keys` résout d'abord vers la méthode `dict.keys()` avant l'entrée
        `"keys"` du dict, ce qui casse `settings.keys[prov]`.
        """
        return {
            "embed_provider": self.embed_provider,
            "chat_provider": self.chat_provider,
            "embed_model": self.embed_model,
            "chat_model": self.chat_model,
            "mistral_chat_model": self.mistral_chat_model,
            "vllm_base_url": self.vllm_base_url or "",
            "docling_url": self.docling_url or "",
            "vision_url": self.vision_url or "",
            "local_embed_models": sorted(self.local_embed_urls),
            "key_status": {prov: self._mask(getattr(self, field_))
                           for prov, field_ in KEY_FIELDS.items()},
            "chroma": {
                "host": self.chroma_host, "port": self.chroma_port, "ssl": self.chroma_ssl,
                "auth_header": self._mask_opaque(self.chroma_auth_header),
            },
            "mailbox": self.mailbox_public(),
        }

    def mailbox_public(self) -> dict:
        return {
            "imap_enabled": self.imap_enabled, "imap_host": self.imap_host,
            "imap_port": self.imap_port, "imap_ssl": self.imap_ssl, "imap_user": self.imap_user,
            "imap_password": self._mask_opaque(self.imap_password), "imap_folder": self.imap_folder,
            "imap_poll_interval": self.imap_poll_interval,
            "imap_processed_action": self.imap_processed_action,
            "imap_processed_folder": self.imap_processed_folder,
            "mailbox_address": self.mailbox_address,
            "mailbox_allowed_senders": self.mailbox_allowed_senders,
            "mailbox_require_auth_results": self.mailbox_require_auth_results,
            "mailbox_autofile": self.mailbox_autofile,
            "mailbox_autofile_threshold": self.mailbox_autofile_threshold,
        }

    def allowed_senders(self) -> list[str]:
        """Liste normalisée des expéditeurs autorisés (adresses ou « @domaine »)."""
        import re
        return [v.strip().lower() for v in re.split(r"[,;\s]+", self.mailbox_allowed_senders or "") if v.strip()]


# Instance partagée
cfg = Config()
# Surcouche des réglages saisis via l'UI (prioritaire sur le .env).
cfg.load_overrides()
