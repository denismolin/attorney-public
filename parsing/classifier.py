"""Classement de chaque email par affaire (mots-clés sur sujet + corps).

Affaires : succession (défaut large), dette (reconnaissance de dette DURAND),
pro (activités professionnelles du défunt / autres dossiers : BANIN…), autre.
"""
from __future__ import annotations

import re

# Ordre d'évaluation : dette et pro d'abord (plus spécifiques), puis succession.
_PRO_PATTERNS = [
    r"\bBANIN\b",
    r"\bABEILLE\b",
    r"comptes?\s+sociaux",
    r"\bINPI\b",
    r"statuts?\s+constitutifs?",
    r"fiches?\s+de\s+paie",
    r"bodacc",
    r"\bFacture\b",
]
_DETTE_PATTERNS = [
    r"reconnaissance\s+de\s+dette",
    r"\bdette[s]?\b",
    r"\bDURAND\b",
    r"98\s?500",
    r"prescription",
]
_SUCCESSION_PATTERNS = [
    r"succession",
    r"\bDUPONT\b",
    r"partage",
    r"notaire",
    r"\blegs\b",
    r"usufruit",
    r"assignation",
    r"h[ée]riti",
]

_PRO_RE = re.compile("|".join(_PRO_PATTERNS), re.IGNORECASE)
_DETTE_RE = re.compile("|".join(_DETTE_PATTERNS), re.IGNORECASE)
_SUCCESSION_RE = re.compile("|".join(_SUCCESSION_PATTERNS), re.IGNORECASE)


def classify_all(subject: str, body: str = "") -> list[str]:
    """Renvoie TOUTES les affaires détectées (un doc peut couvrir plusieurs volets).

    Un même email/PJ peut concerner à la fois la dette ET la succession (ex. un
    projet de partage qui intègre la créance DURAND). On ne force donc pas un
    classement exclusif : on retourne la liste des affaires pertinentes.
    """
    # On pondère le sujet (signal plus fort) en le répétant.
    hay = f"{subject} {subject} {body}"
    affaires: list[str] = []

    if _DETTE_RE.search(hay):
        affaires.append("dette")
    if _SUCCESSION_RE.search(hay):
        affaires.append("succession")
    # "pro" n'est ajouté que s'il n'y a pas de signal succession dans le SUJET
    # (évite que tout mail de succession touchant aux biens soit tagué "pro").
    if _PRO_RE.search(hay) and not _SUCCESSION_RE.search(subject):
        affaires.append("pro")

    return affaires or ["autre"]


def classify(subject: str, body: str = "") -> str:
    """Affaire PRINCIPALE (pour l'affichage compact / la couleur de la frise).

    Priorité : succession > dette > pro > autre (la succession est le fil
    directeur du dossier). Pour le filtrage, utiliser plutôt `classify_all`.
    """
    found = set(classify_all(subject, body))
    for affaire in ("succession", "dette", "pro", "autre"):
        if affaire in found:
            return affaire
    return "autre"


if __name__ == "__main__":
    cases = [
        ("Succession DUPONT / MARTIN", "partage et legs"),
        ("Dossier DUPONT MARTIN _ DURAND - dettes", "reconnaissance de dette 98 500"),
        ("Dossier BANIN", "comptes sociaux 2022 INPI"),
        ("test", ""),
    ]
    for subj, body in cases:
        print(f"{classify(subj, body):12s} <- {subj!r}")
