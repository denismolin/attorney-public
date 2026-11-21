"""Textes confidentiels du dossier par défaut, conservés HORS du code.

Le code (et donc l'image Docker) ne contient aucun nom de partie, montant ou
mot-clé propre à une affaire : uniquement des textes neutres. Une installation
qui a besoin de textes spécifiques pour son dossier par défaut (prompts,
exemples affichés, mots-clés du classement par affaire) les fournit dans un
fichier JSON placé avec ses données :

    SOURCE_DIR/_private/default_case.json      (emplacement recommandé)
    GENERATED_DIR/default_case.json            (accepté aussi)
    ou le chemin donné par la variable CASE_PRIVATE_FILE

Format (toutes les clés sont facultatives) :

    {
      "prompts":    {"chat": "...", "synthesis": "...", "advisor": "...",
                     "intent": "...", "enrich_email": "...", "enrich_attachment": "..."},
      "ui":         {"chat_placeholder": "...", "synthesis_lede": "...",
                     "synthesis_placeholder": "...",
                     "synthesis_prompts": [{"label": "...", "prompt": "..."}],
                     "advisor_prompts":   [{"label": "...", "prompt": "..."}]},
      "classifier": {"pro": ["regex", ...], "dette": [...], "succession": [...]}
    }

Sans ce fichier, les textes neutres du code s'appliquent. Le fichier est relu
quand il change (pas de redémarrage nécessaire). Un fichier illisible ou mal
formé est ignoré, avec un message dans les journaux.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from config import GENERATED_DIR, SOURCE_DIR

_PROMPT_KINDS = {"chat", "synthesis", "advisor", "intent", "enrich_email", "enrich_attachment"}
_UI_TEXT_KEYS = ("chat_placeholder", "synthesis_lede", "synthesis_placeholder")
_UI_LIST_KEYS = ("synthesis_prompts", "advisor_prompts")
_CLASSIFIER_KEYS = ("pro", "dette", "succession")

_lock = threading.Lock()
_cache: tuple[str, float, dict] | None = None   # (chemin, mtime, contenu validé)


def candidate_paths() -> list[Path]:
    paths = []
    env = (os.environ.get("CASE_PRIVATE_FILE") or "").strip()
    if env:
        paths.append(Path(env))
    paths.append(SOURCE_DIR / "_private" / "default_case.json")
    paths.append(GENERATED_DIR / "default_case.json")
    return paths


def _validate(raw) -> dict:
    """Ne garde que les clés connues et bien typées (le reste est ignoré)."""
    out: dict = {"prompts": {}, "ui": {}, "classifier": {}}
    if not isinstance(raw, dict):
        return out
    prompts = raw.get("prompts")
    if isinstance(prompts, dict):
        for k, v in prompts.items():
            if k in _PROMPT_KINDS and isinstance(v, str) and v.strip():
                out["prompts"][k] = v
    ui = raw.get("ui")
    if isinstance(ui, dict):
        for k in _UI_TEXT_KEYS:
            if isinstance(ui.get(k), str) and ui[k].strip():
                out["ui"][k] = ui[k]
        for k in _UI_LIST_KEYS:
            items = ui.get(k)
            if isinstance(items, list):
                clean = [
                    {"label": i["label"], "prompt": i["prompt"]}
                    for i in items
                    if isinstance(i, dict) and isinstance(i.get("label"), str)
                    and isinstance(i.get("prompt"), str)
                ]
                if clean:
                    out["ui"][k] = clean
    cls = raw.get("classifier")
    if isinstance(cls, dict):
        for k in _CLASSIFIER_KEYS:
            pats = cls.get(k)
            if isinstance(pats, list) and pats and all(isinstance(p, str) and p for p in pats):
                try:
                    re.compile("|".join(pats), re.IGNORECASE)
                except re.error as exc:
                    print(f"[case_private] motifs « {k} » ignorés (expression invalide : {exc})")
                    continue
                out["classifier"][k] = list(pats)
    return out


def load() -> dict:
    """Contenu validé du fichier privé, ou des sections vides s'il est absent."""
    global _cache
    for path in candidate_paths():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        key = str(path)
        with _lock:
            if _cache and _cache[0] == key and _cache[1] == mtime:
                return _cache[2]
        try:
            data = _validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print(f"[case_private] {path.name} illisible, ignoré : {exc.__class__.__name__}")
            data = _validate(None)
        with _lock:
            _cache = (key, mtime, data)
        return data
    return _validate(None)


def prompt(kind: str) -> str | None:
    return load()["prompts"].get(kind)


def ui() -> dict:
    return dict(load()["ui"])


def classifier_patterns() -> dict:
    return dict(load()["classifier"])


def source() -> str | None:
    """Nom du fichier privé actif (pour l'écran d'état), sans son chemin complet."""
    for path in candidate_paths():
        if path.is_file():
            return path.name
    return None
