"""Extraction robuste d'un objet JSON depuis une réponse LLM.

Gère les cas courants : JSON direct, balises markdown ```json …```, texte autour,
et balises de raisonnement <think>…</think>. Partagé entre la génération de
dataset (eval/generator.py) et l'enrichissement (parsing/enrich.py).
"""
from __future__ import annotations

import json
import re


def parse_json_response(text: str) -> dict | None:
    if not text:
        return None
    # Retirer les balises <think>...</think> si présentes
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Essai direct
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Bloc JSON entre ``` ou ```json
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Premier { ... } dans le texte
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None
