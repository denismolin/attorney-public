"""Lecture des scans manuscrits par un modèle vision local (Qwen3-VL, service `vision`).

Repli quand Claude Vision n'a rien rendu et que Docling n'a presque rien lu : sur
nos dossiers, un testament manuscrit donne 3 à 37 mots avec Docling (RapidOCR ne
lit pas l'écriture manuscrite) contre ~173 avec Claude et autant avec Qwen3-VL 4B.

Le service parle l'API OpenAI (llama.cpp). Sur CPU, compter 1 à 6 min par page ;
sur GPU (quand le modèle de chat est arrêté), quelques secondes. Même adresse
dans les deux cas : c'est le service qui change de mode, pas cette application.

Garde-fous propres aux petits modèles vision : une page peut partir en boucle
(la même phrase répétée jusqu'à la limite) ou revenir vide ; on coupe les boucles
et on réessaie une page vide à une autre résolution.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Callable

from config import EXTRACTED_TEXT_DIR

_DPI = 150
_RETRY_DPI = 200
_MAX_SIDE = 1600          # px : garde l'image sous ~2 000 jetons (contexte 8 192)
_MAX_TOKENS = 2048        # une page dense fait ~700 jetons ; au-delà c'est une boucle
_TIMEOUT_S = 600.0        # CPU : jusqu'à ~6 min par page
_MAX_PAGES = 40
_CACHE_VERSION = "v1"
_PROMPT = (
    "Transcris ce document juridique français mot à mot, y compris l'écriture "
    "manuscrite, sans reformuler ni ajouter de ponctuation. Conserve la mise en page "
    "(sauts de ligne, tirets, numéros). Si une zone est illisible, écris [illisible]. "
    "Réponds uniquement avec le texte transcrit, sans commentaire."
)


def _cache_path(pdf_hash: str) -> Path:
    return EXTRACTED_TEXT_DIR / f"{pdf_hash}.vlm-{_CACHE_VERSION}.json"


def trim_loops(text: str, n: int = 8, max_repeats: int = 2) -> str:
    """Coupe une répétition en boucle : dès qu'une suite de `n` mots apparaît plus de
    `max_repeats` fois, le texte s'arrête avant sa (max_repeats+1)-ième occurrence.

    Un vrai document répète rarement 8 mots à l'identique plus de deux fois ; une
    boucle de modèle les répète des dizaines de fois.
    """
    words = text.split()
    if len(words) < n * (max_repeats + 1):
        return text
    seen: dict[tuple, int] = {}
    for i in range(len(words) - n + 1):
        key = tuple(w.lower() for w in words[i:i + n])
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > max_repeats:
            return " ".join(words[:i]).rstrip() + " [boucle coupée]"
    return text


def _render(path: Path, dpi: int) -> list[bytes]:
    import fitz  # PyMuPDF

    out: list[bytes] = []
    with fitz.open(str(path)) as doc:
        for page in list(doc)[:_MAX_PAGES]:
            zoom = dpi / 72
            longest = max(page.rect.width, page.rect.height) * zoom
            if longest > _MAX_SIDE:
                zoom *= _MAX_SIDE / longest
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB)
            out.append(pix.tobytes("png"))
    return out


def _ask(client, url: str, api_key: str, png: bytes) -> str:
    b64 = base64.standard_b64encode(png).decode()
    resp = client.post(
        url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        json={
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": _PROMPT},
            ]}],
            "temperature": 0,
            "max_tokens": _MAX_TOKENS,
            # llama.cpp : pénalité DRY légère contre les boucles.
            "dry_multiplier": 0.5,
        },
        timeout=_TIMEOUT_S,
    )
    resp.raise_for_status()
    choice = (resp.json().get("choices") or [{}])[0]
    return ((choice.get("message") or {}).get("content") or "").strip()


def transcribe_pdf(
    path: Path,
    url: str,
    api_key: str | None,
    log: Callable[[str], None] | None = None,
) -> str:
    """Texte de toutes les pages (séparées par une ligne vide), ou "" si échec."""
    data = path.read_bytes()
    cache = _cache_path(hashlib.sha256(data).hexdigest())
    if cache.exists():
        if log:
            log(f"  ↷ Lecture vision {path.name} : déjà en cache")
        return "\n\n".join(json.loads(cache.read_text(encoding="utf-8")))

    import httpx  # fourni avec le SDK openai

    try:
        pages = _render(path, _DPI)
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"  ⚠ Lecture vision {path.name} : rendu impossible ({exc})")
        return ""

    texts: list[str] = []
    with httpx.Client() as client:
        for i, png in enumerate(pages):
            try:
                text = trim_loops(_ask(client, url, api_key or "", png))
                if not text:
                    retry = _render_page(path, i, _RETRY_DPI)
                    text = trim_loops(_ask(client, url, api_key or "", retry)) if retry else ""
            except Exception as exc:  # noqa: BLE001 - service absent : on abandonne
                msg = (f"  ⚠ Lecture vision indisponible pour {path.name} "
                       f"(page {i + 1}, {type(exc).__name__})")
                print(msg, flush=True)
                if log:
                    log(msg)
                return ""
            texts.append(text)
            if log:
                log(f"  [vision] {path.name} page {i + 1}/{len(pages)} → {len(text.split())} mots")

    if not any(texts):
        return ""
    EXTRACTED_TEXT_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")
    return "\n\n".join(t for t in texts if t)


def _render_page(path: Path, index: int, dpi: int) -> bytes | None:
    import fitz  # PyMuPDF

    try:
        with fitz.open(str(path)) as doc:
            page = doc[index]
            zoom = dpi / 72
            longest = max(page.rect.width, page.rect.height) * zoom
            if longest > _MAX_SIDE * 1.25:   # le réessai monte un peu plus haut
                zoom *= _MAX_SIDE * 1.25 / longest
            return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB).tobytes("png")
    except Exception:  # noqa: BLE001
        return None
