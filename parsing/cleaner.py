"""Nettoyage des corps d'emails : sépare le message neuf des citations.

Beaucoup d'emails Outlook citent tout l'historique (redondances). On détecte le
début du bloc cité (en-têtes Outlook FR/EN, lignes `>`, séparateurs) et on retire
le disclaimer récurrent de Me Dubois. Le template affiche `new` par défaut et
`quoted` repliable.
"""
from __future__ import annotations

import re

# Marqueurs de début d'un bloc cité / historique.
_QUOTE_HEADERS = [
    # En-têtes Outlook FR (un bloc "De : ... Envoyé : ... À : ... Objet :")
    r"^\s*De\s*:.*$",
    r"^\s*Envoyé\s*:.*$",
    r"^\s*De la part de\s*:.*$",
    # En-têtes Outlook EN
    r"^\s*From\s*:.*$",
    r"^\s*Sent\s*:.*$",
    # Séparateurs classiques
    r"^-{2,}\s*Message d['’]origine\s*-{2,}.*$",
    r"^-{2,}\s*Original Message\s*-{2,}.*$",
    r"^_{5,}\s*$",
    r"^\s*Le\s+.+\s+a\s+écrit\s*:\s*$",          # "Le 12 mai 2024 à ..., X a écrit :"
    r"^\s*On\s+.+\s+wrote\s*:\s*$",
]
_QUOTE_HEADER_RE = re.compile("|".join(_QUOTE_HEADERS), re.IGNORECASE)

# Disclaimer récurrent de Me Dubois (début de bloc à retirer en pied).
_DISCLAIMER_MARKERS = [
    "La rapidité actuelle des moyens de transmission",
    "CONFIDENTIEL - Les informations contenues",
    "CONFIDENTIEL – Les informations contenues",
    "Si vous receviez ce message par erreur",
]


def _strip_disclaimer(text: str) -> str:
    lowered = text
    cut = len(text)
    for marker in _DISCLAIMER_MARKERS:
        idx = lowered.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut].rstrip()


def clean_body(text: str) -> dict[str, str]:
    """Retourne {'new': message neuf, 'quoted': historique replié}.

    Heuristique : la première ligne qui ressemble à un en-tête de citation
    (ou la première ligne commençant par '>') marque le début de l'historique.
    """
    if not text:
        return {"new": "", "quoted": ""}

    lines = text.splitlines()
    split_at: int | None = None
    consecutive_quoted = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if _QUOTE_HEADER_RE.match(stripped):
            split_at = i
            break
        if stripped.startswith(">"):
            consecutive_quoted += 1
            if consecutive_quoted >= 2:  # deux lignes citées d'affilée = historique
                split_at = i - 1
                break
        else:
            consecutive_quoted = 0

    if split_at is None:
        new_part = text
        quoted_part = ""
    else:
        new_part = "\n".join(lines[:split_at])
        quoted_part = "\n".join(lines[split_at:])

    new_part = _strip_disclaimer(new_part).strip()
    quoted_part = quoted_part.strip()
    return {"new": new_part, "quoted": quoted_part}


if __name__ == "__main__":
    sample = (
        "Chère Madame,\n\nVoici le point sur le dossier.\n\nBien à vous,\n\n"
        "De : Me Dubois\nEnvoyé : lundi 12 mai\nÀ : Marie\nObjet : Succession\n\n"
        "> ancien message cité\n> suite\n"
    )
    res = clean_body(sample)
    print("NEW:\n", res["new"])
    print("\nQUOTED:\n", res["quoted"])
