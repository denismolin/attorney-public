"""Relecture des transcriptions manuscrites : restaure les mots mal lus.

Un modèle vision lit l'écriture manuscrite mot à mot ; il écorche surtout les noms
propres (personnes, rues, communes) et quelques mots courants. Cette passe donne la
lecture brute à un modèle de texte (celui de l'enrichissement, Mistral par défaut),
avec le vocabulaire du dossier (noms et lieux déjà extraits des autres documents), et
lui demande de corriger les seules erreurs de lecture, sans rien ajouter.

Garde-fous : si la longueur du texte corrigé s'écarte de plus de 20 % de la lecture
brute, ou si l'appel échoue, on garde la lecture brute. La lecture brute reste en cache
à côté (parsing/vlm_ocr.py), la version relue est mise en cache par hash.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Callable

from config import DB_PATH, EXTRACTED_TEXT_DIR

NOTE = ("[Transcription d'un document manuscrit, relue automatiquement : "
        "vérifier noms, dates et montants sur l'original.]")
_CACHE_VERSION = "v1"
_VOCAB_LIMIT = 300
_MAX_DRIFT = 0.20

_SYSTEM = (
    "Tu relis la transcription automatique d'un document juridique français manuscrit. "
    "La lecture contient des erreurs de déchiffrage : mots écorchés, lettres confondues, "
    "noms propres mal lus. Corrige UNIQUEMENT ces erreurs de lecture, en t'aidant du "
    "contexte et de la liste des noms et lieux connus du dossier.\n"
    "Règles strictes :\n"
    "- ne reformule pas, ne résume pas, ne change pas l'ordre ;\n"
    "- n'ajoute aucune information et ne complète pas un passage manquant ;\n"
    "- remplace un nom propre par un nom de la liste seulement si la ressemblance est évidente ;\n"
    "- ne modifie un chiffre (date, montant, numéro) que si l'erreur de lecture est certaine ;\n"
    "- garde [illisible], la ponctuation d'origine et les sauts de ligne.\n"
    "Réponds uniquement avec le texte corrigé, sans commentaire."
)


def case_vocabulary(limit: int = _VOCAB_LIMIT) -> list[str]:
    """Noms des parties les plus citées dans le dossier (doc_enrichment.parties_json,
    rempli par l'enrichissement), du plus fréquent au moins fréquent."""
    import json
    from collections import Counter

    counts: Counter = Counter()
    try:
        with sqlite3.connect(DB_PATH) as c:
            rows = c.execute("SELECT parties_json FROM doc_enrichment WHERE parties_json IS NOT NULL").fetchall()
    except sqlite3.Error:
        return []
    for (raw,) in rows:
        try:
            parties = json.loads(raw) or []
        except (TypeError, ValueError):
            continue
        for p in parties if isinstance(parties, list) else []:
            name = (p.get("nom") if isinstance(p, dict) else None) or ""
            name = " ".join(name.split())
            if 2 < len(name) <= 80:
                counts[name] += 1
    return [n for n, _ in counts.most_common(limit)]


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```[a-z]*\n(.*)\n```$", text, re.S)
    return m.group(1).strip() if m else text


def restore(text: str, chat=None, vocabulary: list[str] | None = None,
            log: Callable[[str], None] | None = None) -> str:
    """Texte relu, ou `text` inchangé si la relecture échoue ou dérive."""
    if not text.strip():
        return text
    vocab = case_vocabulary() if vocabulary is None else vocabulary
    key = hashlib.sha256((text + "\x00" + "\n".join(vocab)).encode()).hexdigest()
    cache = EXTRACTED_TEXT_DIR / f"{key}.restore-{_CACHE_VERSION}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8")

    from parsing.enrich import _retry, get_enrich_chat
    try:
        chat = chat or get_enrich_chat()
    except Exception as exc:  # noqa: BLE001 - pas de clé / provider absent
        if log:
            log(f"  ⚠ Relecture impossible ({type(exc).__name__}) : lecture brute conservée")
        return text
    names = "\n".join(f"- {v}" for v in vocab) or "(aucun)"
    user = f"Noms et lieux connus du dossier :\n{names}\n\nTranscription à relire :\n<<<\n{text}\n>>>"
    resp = _retry(lambda: chat.complete(system=_SYSTEM, messages=[{"role": "user", "content": user}],
                                        tools=None), log=log)
    out = _strip_fences((resp or {}).get("text", "") if isinstance(resp, dict) else "")
    out = out.removeprefix("<<<").removesuffix(">>>").strip()
    n_in, n_out = len(text.split()), len(out.split())
    if not out or abs(n_out - n_in) > _MAX_DRIFT * max(n_in, 1):
        if log:
            log(f"  ⚠ Relecture écartée ({n_in} → {n_out} mots) : lecture brute conservée")
        return text
    EXTRACTED_TEXT_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(out, encoding="utf-8")
    if log:
        log(f"  ✓ Relecture : {sum(a != b for a, b in zip(text.split(), out.split()))} mot(s) corrigé(s)")
    return out
