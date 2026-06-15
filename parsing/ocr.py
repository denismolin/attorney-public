"""OCR des PDF scannés via Claude Vision (claude-haiku-4-5).

Flux :
  1. Convertit les pages du PDF en images PNG via PyMuPDF (fitz) — pas de
     dépendance système Poppler nécessaire.
  2. Envoie chaque page à Claude claude-haiku-4-5 avec un prompt de transcription
     stricte (pas de reformulation).
  3. Concatène les textes de toutes les pages.

Retourne une chaîne vide si la clé API est absente ou si toutes les pages échouent.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Callable

_PDF_DPI = 150       # résolution de rendu (150 dpi = bon équilibre qualité/coût)
_MAX_PAGES = 30      # pages par tranche (les docs plus longs sont découpés en tranches)
_HAIKU_MODEL = "claude-haiku-4-5-20251001"

_TRANSCRIPTION_PROMPT = (
    "Transcris ce document juridique français mot à mot, sans reformuler ni "
    "ajouter de ponctuation. Conserve la mise en page (sauts de ligne, tirets, "
    "numéros). Si une zone est illisible, écris [illisible]. Réponds uniquement "
    "avec le texte transcrit, sans commentaire."
)


def _render_pdf_page_chunks(path: Path) -> list[list[bytes]]:
    """Rend toutes les pages du PDF en PNG et les découpe en tranches de _MAX_PAGES."""
    import fitz

    pages: list[bytes] = []
    with fitz.open(str(path)) as doc:
        for page in doc:
            mat = fitz.Matrix(_PDF_DPI / 72, _PDF_DPI / 72)
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
            pages.append(pix.tobytes("png"))
    return [pages[i:i + _MAX_PAGES] for i in range(0, max(len(pages), 1), _MAX_PAGES)]


def _render_pdf_pages(path: Path) -> list[bytes]:
    """Rend les pages d'un PDF en PNG via PyMuPDF (fitz). Retourne des bytes PNG."""
    import fitz  # PyMuPDF

    pages_png: list[bytes] = []
    with fitz.open(str(path)) as doc:
        n = min(len(doc), _MAX_PAGES)
        for i in range(n):
            page = doc[i]
            mat = fitz.Matrix(_PDF_DPI / 72, _PDF_DPI / 72)
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
            pages_png.append(pix.tobytes("png"))
    return pages_png


def extract_text_vision(
    path: Path,
    api_key: str,
    log: Callable[[str], None] | None = None,
) -> str:
    """Extrait le texte d'un PDF scanné via Claude Vision.

    Args:
        path: Chemin vers le PDF.
        api_key: Clé Anthropic.
        log: Callback facultatif pour les messages de progression.

    Returns:
        Texte extrait (concaténation de toutes les pages) ou "" si échec.
    """
    if not api_key:
        return ""

    try:
        import anthropic
    except ImportError:
        if log:
            log("  [OCR] module anthropic non disponible", "error")
        return ""

    try:
        pages = _render_pdf_pages(path)
    except Exception as exc:
        if log:
            log(f"  [OCR] rendu PDF échoué : {exc}", "error")
        return ""

    if not pages:
        return ""

    client = anthropic.Anthropic(api_key=api_key)
    parts: list[str] = []

    for i, png_bytes in enumerate(pages, 1):
        b64 = base64.standard_b64encode(png_bytes).decode()
        try:
            resp = client.messages.create(
                model=_HAIKU_MODEL,
                max_tokens=4096,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": b64,
                                },
                            },
                            {"type": "text", "text": _TRANSCRIPTION_PROMPT},
                        ],
                    }
                ],
            )
            text = resp.content[0].text if resp.content else ""
            if text.strip():
                parts.append(text.strip())
            if log:
                log(f"  [OCR] page {i}/{len(pages)} → {len(text)} chars")
        except Exception as exc:
            if log:
                log(f"  [OCR] page {i}/{len(pages)} échouée : {exc}", "error")

    return "\n\n".join(parts)


def extract_text_vision_parts(
    path: Path,
    api_key: str,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """Comme extract_text_vision mais retourne une liste de textes, un par tranche de
    _MAX_PAGES pages. Les documents courts (≤ _MAX_PAGES pages) retournent une liste
    à un seul élément. Retourne [] si la clé API est absente ou si tout échoue.
    """
    if not api_key:
        return []

    try:
        import anthropic
    except ImportError:
        if log:
            log("  [OCR] module anthropic non disponible", "error")
        return []

    try:
        chunks = _render_pdf_page_chunks(path)
    except Exception as exc:
        if log:
            log(f"  [OCR] rendu PDF échoué : {exc}", "error")
        return []

    if not chunks:
        return []

    total_pages = sum(len(c) for c in chunks)
    client = anthropic.Anthropic(api_key=api_key)
    results: list[str] = []
    page_offset = 0

    for chunk_pages in chunks:
        parts: list[str] = []
        for i, png_bytes in enumerate(chunk_pages, 1):
            b64 = base64.standard_b64encode(png_bytes).decode()
            try:
                resp = client.messages.create(
                    model=_HAIKU_MODEL,
                    max_tokens=4096,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": b64,
                                    },
                                },
                                {"type": "text", "text": _TRANSCRIPTION_PROMPT},
                            ],
                        }
                    ],
                )
                text = resp.content[0].text if resp.content else ""
                if text.strip():
                    parts.append(text.strip())
                if log:
                    log(f"  [OCR] page {page_offset + i}/{total_pages} → {len(text)} chars")
            except Exception as exc:
                if log:
                    log(f"  [OCR] page {page_offset + i}/{total_pages} échouée : {exc}", "error")
        page_offset += len(chunk_pages)
        results.append("\n\n".join(parts))

    return results
