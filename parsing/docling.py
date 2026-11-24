"""Extraction des PDF via Docling (service docling-serve de la pile, local, gratuit).

Docling restitue la structure du document en Markdown : titres, ordre de lecture et
surtout les tableaux (échéanciers, montants), là où PyMuPDF ne rend que du texte à
plat. Pour les scans il ne sert que de repli : sur nos dossiers (manuscrit, scans
médiocres) RapidOCR rend jusqu'à 2x moins de mots que Claude Vision.

Activé par DOCLING_URL (ex. http://docling:5001). Absent, ou service injoignable
(poste de travail en veille), l'extraction retombe sur PyMuPDF. Le résultat est mis en cache par hash du PDF.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from config import EXTRACTED_TEXT_DIR

# Un gros PDF scanné prend plusieurs minutes (le service attend jusqu'à 600 s).
_TIMEOUT_S = 660.0
_OPTIONS = {
    "to_formats": "md",
    "do_ocr": "true",
    "ocr_engine": "rapidocr",
    "image_export_mode": "placeholder",
    "table_mode": "accurate",
    # Lecteur PDF : sur nos scans, le lecteur par défaut perd du texte (13 p. : 44 %
    # du texte de Claude) ; pypdfium2 en récupère 82 %, sans perte sur les autres.
    "pdf_backend": "pypdfium2",
}
# Change quand les options changent : les extractions en cache sont refaites.
_CACHE_VERSION = "v2"


def _cache_path(pdf_hash: str) -> Path:
    return EXTRACTED_TEXT_DIR / f"{pdf_hash}.docling-{_CACHE_VERSION}.md"


def convert_pdf(path: Path, base_url: str, log: Callable[[str], None] | None = None) -> str:
    """Markdown du PDF via Docling, ou "" en cas d'échec (l'appelant retombe sur PyMuPDF)."""
    data = path.read_bytes()
    pdf_hash = hashlib.sha256(data).hexdigest()
    cache = _cache_path(pdf_hash)
    if cache.exists():
        if log:
            log(f"  ↷ Docling {path.name} : déjà en cache")
        return cache.read_text(encoding="utf-8")

    import httpx  # fourni avec le SDK openai

    url = base_url.rstrip("/") + "/v1/convert/file"
    try:
        resp = httpx.post(
            url,
            files={"files": (path.name, data, "application/pdf")},
            data=_OPTIONS,
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 - toute panne -> repli
        msg = f"  ⚠ Docling indisponible pour {path.name} ({type(exc).__name__}) : repli PyMuPDF/OCR"
        print(msg, flush=True)
        if log:
            log(msg)
        return ""

    md = ((body.get("document") or {}).get("md_content") or "").strip()
    if body.get("status") not in (None, "success", "partial_success") or not md:
        if log:
            log(f"  ⚠ Docling n'a rien extrait de {path.name} (statut {body.get('status')}) : repli")
        return ""
    EXTRACTED_TEXT_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(md, encoding="utf-8")
    if log:
        log(f"  ✓ Docling {path.name} : {len(md)} caractères")
    return md
