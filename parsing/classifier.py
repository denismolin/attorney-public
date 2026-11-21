"""Classement de chaque email par affaire (mots-clés sur sujet + corps).

Affaires : succession (défaut large), dette, pro (activités professionnelles /
autres dossiers), autre.

Les motifs ci-dessous sont génériques. Une installation peut les remplacer par
les siens (noms de parties, montants…) dans son fichier privé, hors du code :
cf. case_private (clé "classifier").
"""
from __future__ import annotations

import re

import case_private

# Ordre d'évaluation : dette et pro d'abord (plus spécifiques), puis succession.
_DEFAULT_PATTERNS = {
    "pro": [
        r"comptes?\s+sociaux",
        r"\bINPI\b",
        r"statuts?\s+constitutifs?",
        r"fiches?\s+de\s+paie",
        r"bodacc",
        r"\bFacture\b",
    ],
    "dette": [
        r"reconnaissance\s+de\s+dette",
        r"\bdette[s]?\b",
        r"prescription",
    ],
    "succession": [
        r"succession",
        r"partage",
        r"notaire",
        r"\blegs\b",
        r"usufruit",
        r"assignation",
        r"h[ée]riti",
    ],
}

_compiled: tuple[tuple, dict] | None = None   # (signature des motifs, regex compilées)


def _regexes() -> dict:
    """Regex par affaire : motifs du fichier privé s'il en fournit, sinon génériques."""
    global _compiled
    patterns = dict(_DEFAULT_PATTERNS)
    patterns.update(case_private.classifier_patterns())
    sig = tuple((k, tuple(patterns[k])) for k in sorted(patterns))
    if _compiled is None or _compiled[0] != sig:
        _compiled = (sig, {k: re.compile("|".join(v), re.IGNORECASE) for k, v in patterns.items()})
    return _compiled[1]


def classify_all(subject: str, body: str = "") -> list[str]:
    """Renvoie TOUTES les affaires détectées (un doc peut couvrir plusieurs volets).

    Un même email/PJ peut concerner à la fois la dette ET la succession (ex. un
    projet de partage qui intègre une créance). On ne force donc pas un
    classement exclusif : on retourne la liste des affaires pertinentes.
    """
    # On pondère le sujet (signal plus fort) en le répétant.
    hay = f"{subject} {subject} {body}"
    rx = _regexes()
    affaires: list[str] = []

    if rx["dette"].search(hay):
        affaires.append("dette")
    if rx["succession"].search(hay):
        affaires.append("succession")
    # "pro" n'est ajouté que s'il n'y a pas de signal succession dans le SUJET
    # (évite que tout mail de succession touchant aux biens soit tagué "pro").
    if rx["pro"].search(hay) and not rx["succession"].search(subject):
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
        ("Succession — projet de partage", "partage et legs"),
        ("Dossier dettes", "reconnaissance de dette"),
        ("Dossier société", "comptes sociaux 2022 INPI"),
        ("test", ""),
    ]
    for subj, body in cases:
        print(f"{classify(subj, body):12s} <- {subj!r}")
