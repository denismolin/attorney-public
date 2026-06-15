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
DATA_DIR = ROOT / "data"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
EXTRACTED_TEXT_DIR = DATA_DIR / "extracted_text"
UPLOADS_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "app.sqlite"
# Réglages saisis via l'UI (/settings). Surcharge le .env. Sous data/ → gitignoré
# et EXCLU de l'archive de migration (cf. migrate.py). Contient des secrets en clair.
SETTINGS_PATH = DATA_DIR / "settings.json"

# Champs pilotés par le menu /settings (providers, modèles, clés). L'infra
# (Chroma, Flask, chunking) reste réservée au .env / docker-compose.
MANAGED_FIELDS = (
    "embed_provider", "chat_provider", "embed_model", "chat_model", "mistral_chat_model",
    "openai_api_key", "anthropic_api_key", "mistral_api_key",
    "vllm_base_url", "vllm_chat_base_url", "vllm_embed_base_url", "vllm_api_key",
)
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

    # ChromaDB
    chroma_host: str = field(default_factory=lambda: _get("CHROMA_HOST", "localhost"))
    chroma_port: int = field(default_factory=lambda: int(_get("CHROMA_PORT", "8000")))

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

    def ensure_dirs(self) -> None:
        """Crée les répertoires générés s'ils n'existent pas."""
        ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
        EXTRACTED_TEXT_DIR.mkdir(parents=True, exist_ok=True)
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------------- #
    # Réglages runtime (menu /settings) — surcouche persistée sur le .env.
    # ----------------------------------------------------------------------- #
    def load_overrides(self) -> None:
        """Applique les réglages de data/settings.json par-dessus le .env (au démarrage)."""
        if not SETTINGS_PATH.exists():
            return
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        for k in MANAGED_FIELDS:
            if k in data and data[k] is not None:
                setattr(self, k, data[k])

    def apply(self, updates: dict) -> None:
        """Met à jour les champs gérés (en place → effet immédiat) et persiste.

        Les champs absents ou à `None` sont ignorés (un champ clé laissé vide côté UI
        ne doit pas effacer une clé existante). Aligne les base_url vLLM dérivées sur
        `vllm_base_url` quand il est (re)défini sans surcharge explicite.
        """
        for k in MANAGED_FIELDS:
            if k in updates and updates[k] is not None:
                val = updates[k]
                if isinstance(val, str):
                    val = val.strip()
                setattr(self, k, val)
        # Fallback commun : si on change vllm_base_url sans préciser les variantes,
        # propage (même sémantique que les default_factory du .env).
        if updates.get("vllm_base_url"):
            base = self.vllm_base_url
            if not updates.get("vllm_chat_base_url"):
                self.vllm_chat_base_url = base
            if not updates.get("vllm_embed_base_url"):
                self.vllm_embed_base_url = base
        self._save_overrides()

    def _save_overrides(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = {k: getattr(self, k) for k in MANAGED_FIELDS}
        SETTINGS_PATH.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _mask(secret: str | None) -> dict:
        # "EMPTY" = sentinelle vLLM (aucun token réel) → considéré non configuré.
        if not secret or secret == "EMPTY":
            return {"configured": False, "hint": ""}
        tail = secret[-4:] if len(secret) >= 4 else "•" * len(secret)
        return {"configured": True, "hint": "••••" + tail}

    def public_dict(self) -> dict:
        """Vue sûre de la config gérée (clés masquées) pour l'UI /settings."""
        return {
            "embed_provider": self.embed_provider,
            "chat_provider": self.chat_provider,
            "embed_model": self.embed_model,
            "chat_model": self.chat_model,
            "mistral_chat_model": self.mistral_chat_model,
            "vllm_base_url": self.vllm_base_url or "",
            "keys": {prov: self._mask(getattr(self, field_))
                     for prov, field_ in KEY_FIELDS.items()},
        }


# Instance partagée
cfg = Config()
# Surcouche des réglages saisis via l'UI (prioritaire sur le .env).
cfg.load_overrides()
