"""Parsing des fichiers .eml en objets `Email` exploitables.

- Décodage robuste des en-têtes (RFC 2047) et du corps (Windows-1252/qp,
  utf-8/base64, multipart) via `email.policy.default`.
- Reconstruction du fil via In-Reply-To / References.
- `doc_id` stable (hash) pour l'idempotence de l'indexation.
- Extraction des pièces jointes sur disque (sous data/generated/attachments/<doc_id>/).

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

from config import ATTACHMENTS_DIR, GENERATED_DIR, SOURCE_DIR

# Images inline de signature à ignorer en-dessous de cette taille (octets).
INLINE_IMAGE_MIN_BYTES = 20_000

# Numéro de séquence en tête de nom de fichier : "026 Pièces.eml" -> 26
_SEQ_RE = re.compile(r"^\s*(\d+)")


def _slugify_correspondent(folder_name: str) -> str:
    """'mails de untel' -> 'untel'."""
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
    correspondent: str          # nom du dossier d'emails, sans « mails de »
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
    case_id: str = "default"             # dossier, déduit de l'emplacement du fichier
    source: str = "folder"               # folder | upload | mailbox

    @property
    def from_addr(self) -> str:
        """Adresse seule de l'expéditeur, en minuscules ('' si illisible)."""
        from email.utils import parseaddr
        return parseaddr(self.from_ or "")[1].strip().lower()

    @staticmethod
    def _addrs(values: list[str]) -> str:
        from email.utils import getaddresses
        seen: list[str] = []
        for _name, addr in getaddresses([v for v in values if v]):
            addr = addr.strip().lower()
            if "@" in addr and addr not in seen:
                seen.append(addr)
        return ",".join(seen)

    @property
    def to_addrs(self) -> str:
        return self._addrs(self.to)

    @property
    def cc_addrs(self) -> str:
        return self._addrs(self.cc)

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


def _iter_all_attachments(msg: EmailMessage):
    """Like iter_attachments() but also recurses into forwarded message/rfc822 parts."""
    for part in msg.iter_attachments():
        if part.get_content_type() == "message/rfc822":
            for sub in part.get_payload():
                yield from _iter_all_attachments(sub)
        else:
            yield part
    # Also check message/rfc822 parts that aren't flagged as attachments
    # (some mailers embed forwarded messages as inline, not attached).
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822" and part.get_content_disposition() != "attachment":
            for sub in part.get_payload():
                yield from _iter_all_attachments(sub)


def _extract_attachments(msg: EmailMessage, email_id: str) -> list[Attachment]:
    out: list[Attachment] = []
    dest_dir = ATTACHMENTS_DIR / email_id
    seen: set[str] = set()
    for part in _iter_all_attachments(msg):
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
        if att_id in seen:
            continue
        seen.add(att_id)
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
                stored_path=str(stored.relative_to(GENERATED_DIR.parent)),
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
    subject = str(msg.get("Subject") or "").strip()
    from_ = str(msg.get("From") or "")
    body_text = _extract_body_text(msg)

    # doc_id stable : Message-ID si présent. Sinon, hash du contenu (From/Date/
    # Sujet/corps) plutôt que le chemin fichier — un même mail réimporté ou
    # déplacé (nom de fichier différent, suffixe anti-collision d'upload, etc.)
    # garde ainsi le même doc_id au lieu d'être ré-indexé comme un doublon.
    raw_date_header = str(msg.get("Date") or "")
    id_basis = message_id if message_id else "|".join((from_, raw_date_header, subject, body_text))
    email_id = _stable_doc_id("eml", id_basis)

    # Date -> UTC
    try:
        dt = parsedate_to_datetime(raw_date_header) if raw_date_header else _epoch_fallback()
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
        from_=from_,
        to=_decode_addr_list(msg.get("To")),
        cc=_decode_addr_list(msg.get("Cc")),
        subject=subject,
        message_id=message_id,
        in_reply_to=(str(msg.get("In-Reply-To")).strip() if msg.get("In-Reply-To") else None),
        references=references,
        body_text=body_text,
        attachments=_extract_attachments(msg, email_id),
    )


_SKIP_DIRS = {"attachments", "extracted_text"}
CASES_DIRNAME = "cases"
DEFAULT_CASE = "default"


def _is_source_folder(folder: Path) -> bool:
    """Dossier d'emails à indexer ? Exclut les dossiers techniques et ceux préfixés
    par `_` (quarantaine de la boîte mail : rien n'y est indexé avant tri)."""
    return folder.is_dir() and folder.name not in _SKIP_DIRS and not folder.name.startswith("_")


def iter_source_folders(data_dir: Path = SOURCE_DIR):
    """Énumère (dossier_fichiers, correspondant, case_id).

    - `<data_dir>/<dossier>/` (ex. « mails de <nom> », « uploads ») -> dossier par défaut ;
    - `<data_dir>/cases/<case_id>/<dossier>/` -> dossier `case_id`.
    L'emplacement fait foi : c'est lui qui détermine le dossier d'un email.
    """
    if not data_dir.exists():
        return
    for folder in sorted(p for p in data_dir.iterdir() if _is_source_folder(p)):
        if folder.name == CASES_DIRNAME:
            continue
        yield folder, _slugify_correspondent(folder.name), DEFAULT_CASE
    cases_root = data_dir / CASES_DIRNAME
    if cases_root.is_dir():
        for case_dir in sorted(p for p in cases_root.iterdir() if _is_source_folder(p)):
            for folder in sorted(p for p in case_dir.iterdir() if _is_source_folder(p)):
                yield folder, _slugify_correspondent(folder.name), case_dir.name


def locate(path: Path, data_dir: Path = SOURCE_DIR) -> tuple[str, str]:
    """(case_id, correspondant) d'un fichier .eml d'après son emplacement."""
    path = Path(path)
    correspondent = _slugify_correspondent(path.parent.name)
    try:
        rel = path.resolve().relative_to(data_dir.resolve())
    except ValueError:
        return DEFAULT_CASE, correspondent
    if len(rel.parts) >= 4 and rel.parts[0] == CASES_DIRNAME:
        return rel.parts[1], correspondent
    return DEFAULT_CASE, correspondent


def parse_located(path: Path, data_dir: Path = SOURCE_DIR) -> Email:
    """Parse un .eml en renseignant dossier et origine d'après son emplacement."""
    case_id, correspondent = locate(path, data_dir)
    em = parse_eml(Path(path), correspondent)
    em.case_id = case_id
    em.source = "upload" if Path(path).parent.name == "uploads" else "folder"
    return em


def parse_all(data_dir: Path = SOURCE_DIR) -> list[Email]:
    """Parcourt tous les dossiers d'emails, déduplique par Message-ID, trie par date réelle."""
    emails: list[Email] = []
    seen_message_ids: set[str] = set()
    seen_doc_ids: set[str] = set()

    for folder, correspondent, case_id in iter_source_folders(data_dir):
        for eml_path in sorted(folder.glob("*.eml")):
            try:
                em = parse_eml(eml_path, correspondent)
                em.case_id = case_id
                em.source = "upload" if folder.name == "uploads" else "folder"
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
