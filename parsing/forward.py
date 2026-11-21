"""Reconstitution de l'email d'origine à partir d'un message transféré.

Un utilisateur transfère un email à la boîte de collecte. Ce que l'on veut indexer,
c'est l'email d'ORIGINE (son expéditeur, sa date, son texte), pas l'enveloppe du
transfert. Trois formes, par ordre de fiabilité :

1. `rfc822`         : l'original est joint en `message/rfc822` (« transférer en pièce
                      jointe ») -> on le reprend tel quel.
2. `eml_attachment` : un fichier `.eml` est joint -> idem.
3. `inline`         : l'original est cité dans le corps, précédé d'un bloc d'en-têtes
                      (De / Envoyé / À / Objet, en français, anglais ou italien)
                      -> on reconstruit un email avec ces en-têtes et le texte cité.
                      Les pièces jointes du transfert sont reportées dessus.
4. `none`           : aucun transfert reconnu -> le message reçu est gardé tel quel.

Le résultat est un fichier `.eml` autonome, indexé ensuite par le pipeline habituel
(`parse_eml`) sans traitement particulier : une réindexation complète donne le même
résultat. Le mot d'accompagnement de la personne qui transfère n'est PAS indexé.
"""
from __future__ import annotations

import email
import email.policy
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, getaddresses, parseaddr, parsedate_to_datetime


@dataclass
class UnwrapResult:
    eml: bytes                 # email à indexer (original reconstitué)
    mode: str                  # rfc822 | eml_attachment | inline | none
    forwarder: str = ""        # adresse de la personne qui a transféré
    forwarder_note: str = ""   # son mot d'accompagnement
    wrapper_subject: str = ""
    wrapper_to: str = ""


# Libellés d'en-têtes dans un transfert cité (fr / en / it / de usuels).
_H_FROM = r"(?:De|From|Da|Von|Expéditeur)"
_H_DATE = r"(?:Envoyé|Envoyé le|Date|Sent|Inviato|Data|Gesendet|Datum)"
_H_TO = r"(?:À|A|To|Pour|An|Destinataire)"
_H_CC = r"(?:Cc|Copie à)"
_H_SUBJECT = r"(?:Objet|Subject|Oggetto|Betreff|Sujet)"
_H_ANY = rf"(?:{_H_FROM}|{_H_DATE}|{_H_TO}|{_H_CC}|{_H_SUBJECT})"

_FROM_LINE = re.compile(rf"^[>\s*]*{_H_FROM}\s*:\s*(.+?)\s*\**$", re.IGNORECASE)
_HEADER_LINE = re.compile(rf"^[>\s*]*({_H_ANY})\s*\**:\**\s*(.*?)\s*$", re.IGNORECASE)
_MARKER_LINE = re.compile(
    r"^[\s>]*(?:-{2,}.*(?:forward|transf|origin|inoltrat|weitergeleitet).*-{2,}"
    r"|début du message (?:transféré|réexpédié)\s*:?"
    r"|begin forwarded message\s*:?"
    r"|inizio messaggio inoltrato\s*:?)\s*$",
    re.IGNORECASE,
)
_FWD_PREFIX = re.compile(r"^\s*(?:(?:TR|Fwd?|FW|I|WG|Transf[ée]r[ée]?)\s*:\s*)+", re.IGNORECASE)

_MONTHS = {
    # fr
    "janvier": 1, "janv": 1, "février": 2, "fevrier": 2, "févr": 2, "fevr": 2, "mars": 3, "avril": 4,
    "avr": 4, "mai": 5, "juin": 6, "juillet": 7, "juil": 7, "août": 8, "aout": 8, "septembre": 9,
    "sept": 9, "octobre": 10, "oct": 10, "novembre": 11, "nov": 11, "décembre": 12, "decembre": 12,
    "déc": 12, "dec": 12,
    # en
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sep": 9, "october": 10, "november": 11, "december": 12,
    # it
    "gennaio": 1, "gen": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "mag": 5,
    "giugno": 6, "giu": 6, "luglio": 7, "lug": 7, "agosto": 8, "ago": 8, "settembre": 9, "set": 9,
    "ottobre": 10, "ott": 10, "dicembre": 12, "dic": 12,
}
_DATE_WORDS = re.compile(
    r"(\d{1,2})(?:er)?\.?\s+([A-Za-zÀ-ÿ]+)\.?,?\s+(\d{4})(?:\D{1,6}?(\d{1,2})\s*[:hH.]\s*(\d{2}))?"
)
_DATE_WORDS_US = re.compile(
    r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})(?:\D{1,6}?(\d{1,2})\s*[:.]\s*(\d{2})\s*([AaPp][Mm])?)?"
)
_DATE_NUM = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?:\D{1,4}?(\d{1,2})\s*[:hH.]\s*(\d{2}))?")
_DATE_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T\s]+(\d{1,2}):(\d{2}))?")


def parse_loose_date(text: str) -> datetime | None:
    """Date d'un en-tête cité, quel que soit le client mail (formats usuels fr/en/it)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass

    def build(y, mo, d, h=None, mi=None, ampm=None):
        try:
            h, mi = int(h or 0), int(mi or 0)
            if ampm and ampm.lower() == "pm" and h < 12:
                h += 12
            if ampm and ampm.lower() == "am" and h == 12:
                h = 0
            return datetime(int(y), int(mo), int(d), h, mi, tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    m = _DATE_ISO.search(text)
    if m:
        return build(m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
    m = _DATE_WORDS.search(text)
    if m and m.group(2).lower() in _MONTHS:
        return build(m.group(3), _MONTHS[m.group(2).lower()], m.group(1), m.group(4), m.group(5))
    m = _DATE_WORDS_US.search(text)
    if m and m.group(1).lower() in _MONTHS:
        return build(m.group(3), _MONTHS[m.group(1).lower()], m.group(2), m.group(4), m.group(5), m.group(6))
    m = _DATE_NUM.search(text)
    if m:
        # Convention européenne (jour/mois).
        return build(m.group(3), m.group(2), m.group(1), m.group(4), m.group(5))
    return None


def strip_forward_prefix(subject: str) -> str:
    return _FWD_PREFIX.sub("", subject or "").strip()


def _body_text(msg) -> str:
    from parsing.eml_parser import _extract_body_text
    try:
        return _extract_body_text(msg) or ""
    except Exception:
        return ""


def _kind(label: str) -> str:
    for kind, pattern in (("from", _H_FROM), ("date", _H_DATE), ("to", _H_TO),
                          ("cc", _H_CC), ("subject", _H_SUBJECT)):
        if re.fullmatch(pattern, label.strip(), re.IGNORECASE):
            return kind
    return ""


def _find_inline_forward(body: str) -> tuple[str, dict, str] | None:
    """Repère le premier bloc d'en-têtes cité. Retourne (note, en-têtes, texte d'origine)."""
    lines = body.replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if not _FROM_LINE.match(line):
            continue
        headers: dict[str, str] = {}
        last = ""
        j = i
        while j < len(lines) and j < i + 14:
            cur = lines[j]
            m = _HEADER_LINE.match(cur)
            if m and _kind(m.group(1)):
                last = _kind(m.group(1))
                headers.setdefault(last, m.group(2).strip().strip("*").strip())
            elif cur.strip() and last and cur[:1] in (" ", "\t"):
                headers[last] = (headers[last] + " " + cur.strip()).strip()   # en-tête replié
            else:
                break
            j += 1
        # Un vrai bloc d'en-têtes : expéditeur + (objet ou date).
        if "from" not in headers or not ("subject" in headers or "date" in headers):
            continue
        note_lines = lines[:i]
        while note_lines and (not note_lines[-1].strip() or _MARKER_LINE.match(note_lines[-1])):
            note_lines.pop()
        original = "\n".join(lines[j:]).strip("\n")
        # Citation « > » sur tout le bloc : on retire un niveau.
        orig_lines = original.split("\n")
        if orig_lines and all(l.startswith(">") or not l.strip() for l in orig_lines):
            original = "\n".join(re.sub(r"^> ?", "", l) for l in orig_lines)
        return "\n".join(note_lines).strip(), headers, original.strip()
    return None


def _address_header(value: str) -> str:
    """Nettoie un en-tête d'adresses cité (« Nom <a@b> », « Nom [mailto:a@b] », …)."""
    value = re.sub(r"\[mailto:([^\]]+)\]", r"<\1>", value or "")
    value = value.replace(";", ",")
    pairs = [(n.strip().strip('"'), a.strip()) for n, a in getaddresses([value])]
    out = []
    for name, addr in pairs:
        if "@" in addr:
            out.append(f"{name} <{addr}>" if name and name != addr else addr)
        elif name or addr:
            out.append(name or addr)      # nom sans adresse (Outlook) : gardé tel quel
    return ", ".join(out)


def _build_inline_message(wrapper, headers: dict, original: str, forwarder: str) -> bytes:
    orig_from = _address_header(headers.get("from", ""))
    subject = headers.get("subject") or strip_forward_prefix(str(wrapper.get("Subject") or ""))
    date = parse_loose_date(headers.get("date", ""))
    if date is None:
        try:
            date = parsedate_to_datetime(str(wrapper.get("Date")))
        except Exception:
            date = datetime.now(timezone.utc)

    msg = EmailMessage(policy=email.policy.default)
    msg["From"] = orig_from or "inconnu@invalid"
    if headers.get("to"):
        msg["To"] = _address_header(headers["to"])
    if headers.get("cc"):
        msg["Cc"] = _address_header(headers["cc"])
    msg["Subject"] = subject
    msg["Date"] = format_datetime(date)
    # Identifiant stable : le même original transféré deux fois (par deux personnes,
    # avec deux mots d'accompagnement) donne le même email -> détecté comme doublon.
    basis = "|".join((parseaddr(orig_from)[1].lower() or orig_from.lower(),
                      date.strftime("%Y-%m-%dT%H:%M"), subject.strip().lower(),
                      re.sub(r"\s+", " ", original).strip()[:2000]))
    msg["Message-ID"] = f"<fwd-{hashlib.sha1(basis.encode('utf-8', 'replace')).hexdigest()[:24]}@transfert.local>"
    msg["X-Forwarded-By"] = forwarder
    if wrapper.get("Message-ID"):
        msg["X-Forwarded-Message-ID"] = str(wrapper.get("Message-ID")).strip()
    msg.set_content(original or "(message transféré sans texte)")

    # Les pièces jointes de l'original voyagent sur le message de transfert.
    for part in wrapper.iter_attachments():
        if part.get_content_type() == "message/rfc822":
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if not payload:
            continue
        maintype, _, subtype = part.get_content_type().partition("/")
        msg.add_attachment(payload, maintype=maintype or "application",
                           subtype=subtype or "octet-stream",
                           filename=part.get_filename() or "piece")
    return msg.as_bytes()


def unwrap(raw: bytes) -> list[UnwrapResult]:
    """Email(s) d'origine contenu(s) dans un message reçu. Toujours au moins un résultat."""
    wrapper = email.message_from_bytes(raw, policy=email.policy.default)
    forwarder = parseaddr(str(wrapper.get("From") or ""))[1].strip().lower()
    wrapper_subject = str(wrapper.get("Subject") or "").strip()
    wrapper_to = ", ".join(a for _n, a in getaddresses(
        [str(wrapper.get(h) or "") for h in ("To", "Cc", "Delivered-To", "X-Original-To")]) if a)
    body = _body_text(wrapper)
    common = dict(forwarder=forwarder, wrapper_subject=wrapper_subject, wrapper_to=wrapper_to)

    results: list[UnwrapResult] = []
    # 1 et 2 : original(aux) joint(s) au transfert.
    for part in wrapper.walk():
        ctype = part.get_content_type()
        filename = (part.get_filename() or "").lower()
        try:
            if ctype == "message/rfc822":
                inner = part.get_payload()
                inner = inner[0] if isinstance(inner, list) and inner else inner
                if inner is not None and hasattr(inner, "as_bytes"):
                    results.append(UnwrapResult(eml=inner.as_bytes(), mode="rfc822",
                                                forwarder_note=body.strip(), **common))
            elif filename.endswith(".eml"):
                payload = part.get_payload(decode=True)
                if payload and email.message_from_bytes(payload).get("From"):
                    results.append(UnwrapResult(eml=payload, mode="eml_attachment",
                                                forwarder_note=body.strip(), **common))
        except Exception:
            continue
    if results:
        return results

    # 3 : original cité dans le corps.
    found = _find_inline_forward(body)
    if found:
        note, headers, original = found
        try:
            eml = _build_inline_message(wrapper, headers, original, forwarder)
            return [UnwrapResult(eml=eml, mode="inline", forwarder_note=note, **common)]
        except Exception:
            pass

    # 4 : pas un transfert reconnu.
    return [UnwrapResult(eml=raw, mode="none", **common)]


# --------------------------------------------------------------------------- #
# Authentification de l'expéditeur du transfert
# --------------------------------------------------------------------------- #
def sender_authenticated(raw: bytes) -> bool:
    """Le serveur de réception atteste-t-il l'expéditeur (DMARC, ou DKIM/SPF alignés) ?

    Seul le PREMIER en-tête `Authentication-Results` compte : il est ajouté par le
    serveur qui a reçu le message. Ceux qui suivent ont pu être forgés par l'émetteur.
    """
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    results = msg.get_all("Authentication-Results") or []
    if not results:
        return False
    top = re.sub(r"\s+", " ", str(results[0])).lower()
    from_domain = parseaddr(str(msg.get("From") or ""))[1].rpartition("@")[2].lower()
    if not from_domain:
        return False
    if re.search(r"\bdmarc=pass\b", top):
        return True

    def aligned(domain: str) -> bool:
        domain = domain.strip().lstrip("@").lower()
        return bool(domain) and (domain == from_domain or from_domain.endswith("." + domain)
                                 or domain.endswith("." + from_domain))

    for m in re.finditer(r"\bdkim=pass\b[^;]*?header\.(?:d|i)=@?([^\s;]+)", top):
        if aligned(m.group(1).rpartition("@")[2]):
            return True
    for m in re.finditer(r"\bspf=pass\b[^;]*?smtp\.mailfrom=([^\s;]+)", top):
        if aligned(m.group(1).rpartition("@")[2]):
            return True
    return False


def sender_allowed(address: str, allowed: list[str]) -> bool:
    """`allowed` : adresses exactes ou domaines (« @cabinet.fr » ou « cabinet.fr »)."""
    address = (address or "").strip().lower()
    if not address or "@" not in address:
        return False
    domain = address.rpartition("@")[2]
    for entry in allowed:
        entry = entry.strip().lower()
        if not entry:
            continue
        if "@" in entry and not entry.startswith("@"):
            if entry == address:
                return True
        elif entry.lstrip("@") == domain:
            return True
    return False
