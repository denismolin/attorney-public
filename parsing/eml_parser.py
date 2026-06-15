"""Parsing des fichiers .eml en objets `Email` exploitables.

- Décodage robuste des en-têtes (RFC 2047) et du corps (Windows-1252/qp,
  utf-8/base64, multipart) via `email.policy.default`.
- Reconstruction du fil via In-Reply-To / References.
- `doc_id` stable (hash) pour l'idempotence de l'indexation.
- Extraction des pièces jointes sur disque (sous data/attachments/<doc_id>/).

Le tri chronologique se fait sur la date réelle d'envoi (header Date, UTC).
Les numéros de fichier (001, 002…) ne sont chronologiques QUE par correspondant.
"""
from __future__ import annotations

import email
import email.policy
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path

from config import ATTACHMENTS_DIR, DATA_DIR

# Images inline de signature à ignorer en-dessous de cette taille (octets).
INLINE_IMAGE_MIN_BYTES = 20_000

# Numéro de séquence en tête de nom de fichier : "026 Pièces DURAND.eml" -> 26
_SEQ_RE = re.compile(r"^\s*(\d+)")


def _slugify_correspondent(folder_name: str) -> str:
    """'mails de dubois' -> 'dubois'."""
    name = folder_name.lower()
    name = re.sub(r"^mails?\s+de\s+", "", name).strip()
    return re.sub(r"\s+", "_", name)


def _stable_doc_id(prefix: str, *parts: str) -> str:
    """Hash court et stable à partir d'identifiants (Message-ID, chemin…)."""
    h = hashlib.sha1("||".join(parts).encode("utf-8", "replace")).hexdigest()[:16]
    return f"{prefix}_{h}"


@dataclass
class Attachment:
    doc_id: str                 # id stable de la PJ
    filename: str               # nom décodé
    mime: str
    size: int
    stored_path: str            # chemin relatif sous data/
    parent_email_id: str
    extracted_text: str = ""    # rempli plus tard par attachments.extract_text
    needs_ocr: bool = False     # PDF image détecté (traité au jalon 2)


@dataclass
class Email:
    doc_id: str
    correspondent: str          # dubois, leroy, lefebvre, renard, bernard, sophie
    seq: int                    # numéro de fichier dans le dossier (ordre par correspondant)
    source_path: str
    date: datetime              # UTC
    from_: str
    to: list[str]
    cc: list[str]
    subject: str
    message_id: str
    in_reply_to: str | None
    references: list[str]
    body_text: str              # corps texte brut décodé (avant nettoyage)
    attachments: list[Attachment] = field(default_factory=list)
    affaire: str = "succession"          # affaire principale (affichage/couleur)
    affaires: list[str] = field(default_factory=list)  # toutes les affaires (filtrage)

    @property
    def date_iso(self) -> str:
        return self.date.isoformat()


def _decode_addr_list(value: str | None) -> list[str]:
    if not value:
        return []
    # policy.default a déjà décodé ; on split sur les virgules en gardant l'adresse.
    return [a.strip() for a in str(value).split(",") if a.strip()]


def _extract_body_text(msg: EmailMessage) -> str:
    """Privilégie text/plain ; sinon convertit text/html en texte simple."""
    body = msg.get_body(preferencelist=("plain",))
    if body is not None:
        try:
            return body.get_content()
        except Exception:
            payload = body.get_payload(decode=True) or b""
            return payload.decode("utf-8", "replace")

    html = msg.get_body(preferencelist=("html",))
    if html is not None:
        try:
            content = html.get_content()
        except Exception:
            payload = html.get_payload(decode=True) or b""
            content = payload.decode("utf-8", "replace")
        return _html_to_text(content)
    return ""


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n\s*\n\s*\n+")


def _html_to_text(html: str) -> str:
    """Conversion HTML -> texte simple, suffisante pour ce corpus Word/Outlook."""
    import html as html_mod

    # Retire style/script
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    # Les sauts de bloc deviennent des retours ligne
    text = re.sub(r"(?i)<(br|/p|/div|/tr|/li)\s*/?>", "\n", text)
    text = _TAG_RE.sub("", text)
    text = html_mod.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


def _extract_attachments(msg: EmailMessage, email_id: str) -> list[Attachment]:
    out: list[Attachment] = []
    dest_dir = ATTACHMENTS_DIR / email_id
    for part in msg.iter_attachments():
        filename = part.get_filename()
        content_type = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()

        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        size = len(payload)

        # Ignore les petites images inline (signatures, logos).
        if (
            content_type.startswith("image/")
            and disposition != "attachment"
            and size < INLINE_IMAGE_MIN_BYTES
        ):
            continue

        if not filename:
            # PJ sans nom : on en fabrique un à partir du type.
            ext = content_type.split("/")[-1]
            filename = f"piece.{ext}"

        att_id = _stable_doc_id("att", email_id, filename, str(size))
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Nom de fichier sûr sur disque (on garde le nom d'origine en métadonnée).
        safe = re.sub(r"[^\w\-. ]", "_", filename)[:120]
        stored = dest_dir / f"{att_id}_{safe}"
        stored.write_bytes(payload)

        out.append(
            Attachment(
                doc_id=att_id,
                filename=filename,
                mime=content_type,
                size=size,
                stored_path=str(stored.relative_to(DATA_DIR.parent)),
                parent_email_id=email_id,
            )
        )
    return out


def _epoch_fallback() -> datetime:
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_eml(path: Path, correspondent: str) -> Email:
    with path.open("rb") as fh:
        msg: EmailMessage = email.message_from_binary_file(
            fh, policy=email.policy.default
        )

    message_id = (msg.get("Message-ID") or "").strip()
    # doc_id stable : Message-ID si présent, sinon chemin.
    id_basis = message_id if message_id else str(path)
    email_id = _stable_doc_id("eml", id_basis)

    # Date -> UTC
    raw_date = msg.get("Date")
    try:
        dt = parsedate_to_datetime(raw_date) if raw_date else _epoch_fallback()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
    except Exception:
        dt = _epoch_fallback()

    seq_match = _SEQ_RE.match(path.stem)
    seq = int(seq_match.group(1)) if seq_match else 0

    references = str(msg.get("References") or "").split()

    return Email(
        doc_id=email_id,
        correspondent=correspondent,
        seq=seq,
        source_path=str(path),
        date=dt,
        from_=str(msg.get("From") or ""),
        to=_decode_addr_list(msg.get("To")),
        cc=_decode_addr_list(msg.get("Cc")),
        subject=str(msg.get("Subject") or "").strip(),
        message_id=message_id,
        in_reply_to=(str(msg.get("In-Reply-To")).strip() if msg.get("In-Reply-To") else None),
        references=references,
        body_text=_extract_body_text(msg),
        attachments=_extract_attachments(msg, email_id),
    )


def parse_all(data_dir: Path = DATA_DIR) -> list[Email]:
    """Parcourt les 6 dossiers, déduplique par Message-ID, trie par date réelle."""
    emails: list[Email] = []
    seen_message_ids: set[str] = set()
    seen_doc_ids: set[str] = set()

    _SKIP_DIRS = {"attachments", "extracted_text"}
    for folder in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        if folder.name in _SKIP_DIRS:
            continue
        correspondent = _slugify_correspondent(folder.name)
        for eml_path in sorted(folder.glob("*.eml")):
            try:
                em = parse_eml(eml_path, correspondent)
            except Exception as exc:  # pragma: no cover - robustesse
                print(f"[parse_all] échec {eml_path}: {exc}")
                continue

            # Dédup : par Message-ID si présent, sinon par doc_id.
            if em.message_id and em.message_id in seen_message_ids:
                continue
            if em.doc_id in seen_doc_ids:
                continue
            if em.message_id:
                seen_message_ids.add(em.message_id)
            seen_doc_ids.add(em.doc_id)
            emails.append(em)

    emails.sort(key=lambda e: e.date)
    return emails


if __name__ == "__main__":
    # Smoke test : parse tout et affiche un récapitulatif.
    mails = parse_all()
    print(f"Total emails (après dédup) : {len(mails)}")
    by_corr: dict[str, int] = {}
    n_att = 0
    for m in mails:
        by_corr[m.correspondent] = by_corr.get(m.correspondent, 0) + 1
        n_att += len(m.attachments)
    print("Par correspondant :", by_corr)
    print(f"Pièces jointes extraites : {n_att}")
    if mails:
        first, last = mails[0], mails[-1]
        print(f"Plus ancien : {first.date_iso} [{first.correspondent}] {first.subject[:60]}")
        print(f"Plus récent : {last.date_iso} [{last.correspondent}] {last.subject[:60]}")
