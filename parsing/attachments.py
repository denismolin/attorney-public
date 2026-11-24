"""Extraction du texte des pièces jointes.

Jalon 1 : PDF natif (PyMuPDF), DOCX, RTF, TXT.
Jalon 2 : PDF scanné → OCR via Claude Vision (parsing/ocr.py) si ANTHROPIC_API_KEY est configurée.
Jalon 3 : si DOCLING_URL est réglé, Docling (parsing/docling.py) extrait les PDF natifs
(structure + tableaux, repli PyMuPDF) et sert de repli OCR local pour les scans quand
Claude Vision n'est pas configuré ou échoue.

L'OCR (appel payant à Claude Vision) est mis en cache sur disque sous
EXTRACTED_TEXT_DIR, indexé par le hash du PDF source : une réindexation d'une
pièce jointe scannée déjà OCRisée et inchangée ne refait pas l'appel API.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

from config import EXTRACTED_TEXT_DIR, GENERATED_DIR, cfg
from parsing.eml_parser import Attachment

# Seuil : en dessous de N caractères extraits d'un PDF, on considère que c'est
# un scan (texte non sélectionnable) → needs_ocr.
_PDF_MIN_CHARS = 40


def _ocr_cache_path(pdf_hash: str) -> Path:
    return EXTRACTED_TEXT_DIR / f"{pdf_hash}.json"


def _load_ocr_cache(pdf_hash: str) -> list[str] | None:
    cache_path = _ocr_cache_path(pdf_hash)
    if not cache_path.exists():
        return None
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_ocr_cache(pdf_hash: str, parts: list[str]) -> None:
    EXTRACTED_TEXT_DIR.mkdir(parents=True, exist_ok=True)
    _ocr_cache_path(pdf_hash).write_text(
        json.dumps(parts, ensure_ascii=False), encoding="utf-8"
    )


def _abs_path(att: Attachment) -> Path:
    """stored_path est relatif au parent de GENERATED_DIR ; on reconstruit l'absolu."""
    return (GENERATED_DIR.parent / att.stored_path).resolve()


def _extract_pdf(path: Path) -> tuple[str, bool]:
    """Retourne (texte, needs_ocr). PyMuPDF lit le texte natif."""
    import fitz  # PyMuPDF

    parts: list[str] = []
    with fitz.open(path) as doc:
        for page in doc:
            parts.append(page.get_text("text"))
    text = "\n".join(parts).strip()
    needs_ocr = len(text) < _PDF_MIN_CHARS
    return ("" if needs_ocr else text), needs_ocr


def _extract_docx(path: Path) -> str:
    import docx  # python-docx

    document = docx.Document(str(path))
    paras = [p.text for p in document.paragraphs]
    # Tableaux DOCX éventuels (le détail tabulaire fin viendra au jalon 2).
    for table in document.tables:
        for row in table.rows:
            paras.append("\t".join(cell.text for cell in row.cells))
    return "\n".join(paras).strip()


def _extract_rtf(path: Path) -> str:
    from striprtf.striprtf import rtf_to_text

    raw = path.read_text(encoding="utf-8", errors="replace")
    return rtf_to_text(raw).strip()


def _extract_txt(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def extract_text(
    att: Attachment,
    api_key: str | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[str, bool]:
    """Extrait le texte d'une PJ. Retourne (texte, needs_ocr).

    Si api_key est fournie et que le PDF est scanné, tente l'OCR via Claude Vision.
    Si l'OCR réussit, needs_ocr est posé à False (le texte est indexable).
    Les types non pris en charge renvoient ("", False) sans erreur.
    """
    parts = extract_text_parts(att, api_key=api_key, log=log)
    if not parts:
        return "", False
    first_text, needs_ocr = parts[0]
    # Concat all parts for callers that don't support multi-part (legacy path).
    if len(parts) > 1:
        combined = "\n\n".join(t for t, _ in parts if t)
        return combined, needs_ocr
    return first_text, needs_ocr


def extract_text_parts(
    att: Attachment,
    api_key: str | None = None,
    log: Callable[[str], None] | None = None,
) -> list[tuple[str, bool]]:
    """Extrait le texte d'une PJ. Retourne une liste de (texte, needs_ocr).

    Pour les PDF scannés de plus de 30 pages, retourne une entrée par tranche de
    30 pages. Pour tous les autres cas, retourne une liste à un seul élément.
    Les types non pris en charge retournent [("", False)].
    """
    path = _abs_path(att)
    if not path.exists():
        return [("", False)]

    suffix = path.suffix.lower()
    mime = att.mime.lower()

    try:
        if suffix == ".pdf" or mime == "application/pdf":
            text, needs_ocr = _extract_pdf(path)
            docling_url = cfg.docling_url
            if not needs_ocr:
                # PDF natif : Docling apporte la structure (titres, tableaux) ; repli PyMuPDF.
                if docling_url:
                    from parsing.docling import convert_pdf
                    md = convert_pdf(path, docling_url, log=log)
                    if len(md) >= _PDF_MIN_CHARS:
                        return [(md, False)]
                return [(text, False)]
            # Scan : Claude Vision lit nettement mieux nos scans (manuscrit, mauvaise
            # qualité : jusqu'à 2x plus de mots que RapidOCR) ; Docling sert de repli.
            if api_key:
                pdf_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                cached = _load_ocr_cache(pdf_hash)
                if cached is not None:
                    if log:
                        log(f"  ↷ OCR {path.name} : déjà en cache ({len(cached)} partie(s))")
                    ocr_parts = cached
                else:
                    from parsing.ocr import extract_text_vision_parts
                    ocr_parts = extract_text_vision_parts(path, api_key, log=log)
                    if ocr_parts:
                        _save_ocr_cache(pdf_hash, ocr_parts)
                valid = [(t, False) for t in ocr_parts if len(t) >= _PDF_MIN_CHARS]
                if valid:
                    return valid
            if docling_url:
                from parsing.docling import convert_pdf
                md = convert_pdf(path, docling_url, log=log)
                if len(md) >= _PDF_MIN_CHARS:
                    return [(md, False)]
            return [(text, needs_ocr)]
        if suffix == ".docx" or "wordprocessingml" in mime:
            return [(_extract_docx(path), False)]
        if suffix == ".rtf" or mime in ("application/rtf", "text/rtf"):
            return [(_extract_rtf(path), False)]
        if suffix in (".txt", ".md") or mime.startswith("text/"):
            return [(_extract_txt(path), False)]
    except Exception as exc:  # pragma: no cover - robustesse indexation
        print(f"[attachments] extraction échouée {path.name}: {exc}")
        return [("", False)]

    return [("", False)]
