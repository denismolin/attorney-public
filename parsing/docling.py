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
# PDF natif : lecteur par défaut (docling_parse), le meilleur sur les tableaux numériques.
_NATIVE = {
    "to_formats": "md",
    "do_ocr": "true",
    "ocr_engine": "rapidocr",
    "image_export_mode": "placeholder",
    "table_mode": "accurate",
}
# Scan : le lecteur par défaut rend mal certains scans en calques (relevés bancaires :
# lignes absentes de l'image passée à l'OCR) -> pypdfium2 + OCR de toute la page.
# Sortie JSON : le Markdown omet en-têtes et pieds de page (en-tête de lettre, « Édité
# le… »), qu'on réintègre. Mesuré sur un relevé de 13 p. : 44 % -> 93 % du texte de Claude.
_SCAN = {
    "to_formats": "json",
    "do_ocr": "true",
    "ocr_engine": "rapidocr",
    "pdf_backend": "pypdfium2",
    "force_ocr": "true",
    "image_export_mode": "placeholder",
    "table_mode": "accurate",
}
# Change quand les options changent : les extractions en cache sont refaites.
_CACHE_VERSION = "v3"
_LABEL_RANK = {"page_header": 0, "page_footer": 2}


def json_to_text(doc: dict) -> str:
    """Texte lisible d'un DoclingDocument : par page, en-têtes, corps (avec les
    tableaux en lignes « a | b | c »), puis pieds de page."""
    items = []
    for i, t in enumerate(doc.get("texts") or []):
        text = (t.get("text") or "").strip()
        if text:
            items.append(((t.get("prov") or [{}])[0], _LABEL_RANK.get(t.get("label"), 1), i, text))
    n = len(items)
    for j, tab in enumerate(doc.get("tables") or []):
        rows: dict = {}
        for c in (tab.get("data") or {}).get("table_cells") or []:
            rows.setdefault(c.get("start_row_offset_idx", 0), {})[c.get("start_col_offset_idx", 0)] = (c.get("text") or "").strip()
        lines = [" | ".join(r[k] for k in sorted(r)) for _, r in sorted(rows.items())]
        if lines:
            items.append(((tab.get("prov") or [{}])[0], 1, n + j, "\n".join(lines)))

    def key(item):
        prov, rank, idx, _ = item
        bbox = prov.get("bbox") or {}
        top = bbox.get("t", 0) or 0
        y = -top if bbox.get("coord_origin", "BOTTOMLEFT") == "BOTTOMLEFT" else top
        return (prov.get("page_no", 0), rank, y, idx)

    pages: dict = {}
    for item in sorted(items, key=key):
        pages.setdefault(item[0].get("page_no", 0), []).append(item[3])
    return "\n\n".join("\n".join(parts) for _, parts in sorted(pages.items()))


def _cache_path(pdf_hash: str, scanned: bool) -> Path:
    kind = "scan" if scanned else "native"
    return EXTRACTED_TEXT_DIR / f"{pdf_hash}.docling-{kind}-{_CACHE_VERSION}.md"


def convert_pdf(path: Path, base_url: str, log: Callable[[str], None] | None = None,
                scanned: bool = False) -> str:
    """Texte du PDF via Docling (Markdown, ou texte reconstruit pour un scan).
    None si le service n'a pas pu convertir (injoignable, erreur) ; "" s'il n'a rien lu."""
    data = path.read_bytes()
    pdf_hash = hashlib.sha256(data).hexdigest()
    cache = _cache_path(pdf_hash, scanned)
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
            data=_SCAN if scanned else _NATIVE,
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 - toute panne -> repli
        msg = f"  ⚠ Docling indisponible pour {path.name} ({type(exc).__name__}) : repli PyMuPDF/OCR"
        print(msg, flush=True)
        if log:
            log(msg)
        return None

    document = body.get("document") or {}
    if scanned:
        md = json_to_text(document.get("json_content") or {}).strip()
    else:
        md = (document.get("md_content") or "").strip()
    if body.get("status") not in (None, "success", "partial_success") or not md:
        if log:
            log(f"  ⚠ Docling n'a rien extrait de {path.name} (statut {body.get('status')}) : repli")
        return "" if body.get("status") in (None, "success", "partial_success") else None
    EXTRACTED_TEXT_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(md, encoding="utf-8")
    if log:
        log(f"  ✓ Docling {path.name} : {len(md)} caractères")
    return md
