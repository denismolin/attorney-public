"""Enrichissement LLM des documents à l'indexation (provider Mistral).

Pour chaque mail et chaque pièce jointe, on demande au LLM une extraction
structurée (intention, objectif, faits, entités, codes, résumé). On infère aussi
les relations entre un mail et ses PJ (document principal, annexes).

Mistral est économique mais rate-limité : tous les appels passent par
`_mistral_retry` (max 5 tentatives, backoff linéaire +5 s). En cas d'échec
persistant, on retourne None — l'indexation continue sans enrichissement plutôt
que de planter.
"""
from __future__ import annotations

import dataclasses
import time

from config import cfg
from parsing.jsonparse import parse_json_response as _parse_json_response
from rag.providers import get_chat

_MAX_ATTEMPTS = 5
_BACKOFF_STEP = 5  # secondes : 5, 10, 15, 20, 25


EMAIL_SYSTEM = """Tu es un assistant juridique qui analyse la correspondance d'un \
dossier juridique.
À partir de l'email fourni, extrais une analyse structurée.
Retourne UNIQUEMENT un objet JSON valide, sans texte ni balise markdown autour :
{
  "intention": "ce que l'auteur cherche à faire (informer, demander, relancer, contester, notifier…)",
  "objectif": "l'objectif concret du message en une phrase",
  "resume": "résumé du message en une phrase",
  "faits": ["fait marquant 1", "fait marquant 2"],
  "dates_evenements": [
    {"label": "description de l'événement", "date": "AAAA-MM-JJ ou AAAA-MM ou AAAA si incertaine"}
  ],
  "parties": [
    {"role": "émetteur|destinataire|cité", "nom": "nom complet", "qualite": "avocat|notaire|héritier|partenaire PACS|autre"}
  ],
  "declarations": [
    {"auteur": "nom de la personne", "contenu": "ce qu'elle affirme, conteste, demande ou reconnaît"}
  ],
  "codes": {
    "cadastre": ["références cadastrales : section, parcelle, lieu-dit"],
    "comptes_iban": ["IBAN ou références de comptes bancaires"],
    "fiscaux": ["codes fiscaux italiens (codice fiscale), NIR/NIF français, numéros TVA"],
    "dossiers": ["n° de dossier, n° de pratique, n° de rôle, n° de procédure, réf. acte notarial"],
    "autres": ["tout autre code ou référence alphanumérique structurée non classée ci-dessus"]
  }
}
Règles :
- Sois factuel, n'invente rien.
- N'inclus une date que si elle désigne un événement précis (pas la date d'envoi de l'email).
- Pour les parties, ne liste que celles nommées explicitement dans le corps du message.
- Si un champ est vide, mets une liste vide [] ou un objet vide {} selon le type."""


ATTACHMENT_SYSTEM = """Tu es un assistant juridique qui analyse une pièce jointe d'un \
dossier juridique.
À partir du texte du document fourni, extrais une analyse structurée.
Retourne UNIQUEMENT un objet JSON valide, sans texte ni balise markdown autour :
{
  "description": "ce que représente ce document, en une à deux phrases",
  "doc_type": "type du document (acte notarial, déclaration de succession, état hypothécaire, planimétrie, facture, attestation, relevé de compte, procuration, testament, jugement…)",
  "parties": [
    {"role": "signataire|bénéficiaire|cité", "nom": "nom complet", "qualite": "notaire|héritier|partenaire PACS|banque|administration|autre"}
  ],
  "dates_evenements": [
    {"label": "description de l'événement", "date": "AAAA-MM-JJ ou AAAA-MM ou AAAA si incertaine"}
  ],
  "declarations": [
    {"auteur": "nom ou entité", "contenu": "ce qui est affirmé, certifié ou reconnu dans le document"}
  ],
  "codes": {
    "cadastre": ["références cadastrales : commune, section, n° de parcelle, lieu-dit, superficie"],
    "comptes_iban": ["IBAN, BIC, n° de compte"],
    "fiscaux": ["codice fiscale, code SIREN/SIRET, NIR, NIF, n° TVA intracommunautaire"],
    "dossiers": ["n° de dossier notarial, n° de volume/folio, n° de rôle, réf. hypothécaire, n° de pratique"],
    "autres": ["tout autre identifiant alphanumérique structuré (n° de bien, matricule, n° d'ordre…)"]
  }
}
Règles :
- Sois factuel, n'invente rien.
- Extrais toutes les références alphanumériques structurées que tu repères, même si tu n'es pas sûr de leur type.
- Si un champ est vide, mets une liste vide [] ou un objet vide {} selon le type."""


LINKS_SYSTEM = """Tu es un assistant juridique. On te donne un email et la liste de ses \
pièces jointes (chacune avec une brève description).
Détermine les relations entre ces pièces jointes.
Retourne UNIQUEMENT un objet JSON valide, sans texte ni balise markdown autour :
{
  "principal": "att_id de la pièce qui est le document principal de l'envoi, ou null",
  "annexes": ["att_id des pièces qui sont des annexes accompagnant le document principal"],
  "pj_links": [{"src": "att_id", "dst": "att_id", "relation": "accompanies"}]
}
Règles : n'utilise que les att_id fournis. Si tu n'es pas sûr, laisse les listes vides et principal à null."""


def get_enrich_chat(provider: str | None = None, model: str | None = None):
    """Instancie un client de chat pour l'enrichissement.

    Par défaut utilise Mistral. Passer provider/model pour utiliser un autre provider.
    """
    if provider and provider != "mistral":
        config = dataclasses.replace(cfg, chat_provider=provider, chat_model=model or "auto")
    else:
        config = dataclasses.replace(
            cfg, chat_provider="mistral", chat_model=model or cfg.mistral_chat_model
        )
    return get_chat(config)


def _retry(fn, *, log=None):
    """Exécute fn() avec retry linéaire (max 5×, +5 s). Retourne None si échec persistant."""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — rate-limit, réseau, 5xx…
            if attempt == _MAX_ATTEMPTS - 1:
                if log:
                    log(f"Enrichissement abandonné après {_MAX_ATTEMPTS} essais : {exc}",
                        level="error")
                return None
            wait = _BACKOFF_STEP * (attempt + 1)
            if log:
                log(f"Erreur LLM (essai {attempt + 1}/{_MAX_ATTEMPTS}), attente {wait}s… [{exc}]")
            time.sleep(wait)
    return None


def _complete_json(chat, system: str, user: str, log=None) -> dict | None:
    """Appel LLM + parsing JSON, avec retry."""
    def _call():
        resp = chat.complete(system=system, messages=[{"role": "user", "content": user}], tools=None)
        return resp.get("text", "")

    text = _retry(_call, log=log)
    if text is None:
        return None
    return _parse_json_response(text)


def enrich_email(subject: str, mail_text: str, chat, log=None) -> dict | None:
    """{intention, objectif, faits[], entites[], codes[], resume} ou None."""
    user = f"Objet : {subject}\n\nCorps de l'email :\n{mail_text[:6000]}"
    import case_state
    return _complete_json(chat, case_state.prompt_for("enrich_email", EMAIL_SYSTEM), user, log=log)


def enrich_attachment(filename: str, text: str, parent_subject: str, chat, log=None) -> dict | None:
    """{description, doc_type, entites[], codes[]} ou None."""
    user = (
        f"Nom du fichier : {filename}\n"
        f"Transmis par l'email : {parent_subject}\n\n"
        f"Texte du document :\n{text[:6000]}"
    )
    import case_state
    return _complete_json(chat, case_state.prompt_for("enrich_attachment", ATTACHMENT_SYSTEM), user, log=log)


def infer_links(subject: str, att_briefs: list[dict], chat, log=None) -> dict | None:
    """att_briefs : [{att_id, filename, description}]. Retourne {principal, annexes, pj_links} ou None.

    Inutile d'appeler le LLM s'il y a moins de 2 pièces jointes.
    """
    if len(att_briefs) < 2:
        return None
    lines = [
        f"- {b['att_id']} | {b.get('filename', '')} | {b.get('description', '')}"
        for b in att_briefs
    ]
    user = f"Email : {subject}\n\nPièces jointes :\n" + "\n".join(lines)
    return _complete_json(chat, LINKS_SYSTEM, user, log=log)
