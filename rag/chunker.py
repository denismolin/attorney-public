"""Découpage des documents en chunks avec métadonnées, pour le niveau 2 du RAG.

Découpage simple par caractères avec recouvrement, en essayant de couper sur des
frontières de paragraphe/phrase quand c'est possible. Suffisant pour ce corpus ;
un tokenizer fin n'est pas nécessaire au jalon 1.
"""
from __future__ import annotations

from dataclasses import dataclass

from config import cfg


@dataclass
class Chunk:
    chunk_id: str          # f"{parent_doc_id}#{index}"
    parent_doc_id: str
    text: str
    metadata: dict


def _split_text(text: str, size: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        # Essaie de couper sur une frontière propre (saut de paragraphe, point, espace).
        if end < n:
            window = text[start:end]
            for sep in ("\n\n", "\n", ". ", " "):
                idx = window.rfind(sep)
                if idx > size * 0.5:  # ne coupe pas trop tôt
                    end = start + idx + len(sep)
                    break
        chunks.append(text[start:end].strip())
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def chunk_document(
    parent_doc_id: str,
    text: str,
    base_metadata: dict,
    size: int | None = None,
    overlap: int | None = None,
) -> list[Chunk]:
    size = size or cfg.chunk_size
    overlap = overlap or cfg.chunk_overlap
    pieces = _split_text(text, size, overlap)
    out: list[Chunk] = []
    for i, piece in enumerate(pieces):
        meta = dict(base_metadata)
        meta["parent_doc_id"] = parent_doc_id
        meta["chunk_index"] = i
        out.append(
            Chunk(
                chunk_id=f"{parent_doc_id}#{i}",
                parent_doc_id=parent_doc_id,
                text=piece,
                metadata=meta,
            )
        )
    return out


if __name__ == "__main__":
    txt = "Paragraphe un.\n\n" + ("phrase. " * 400)
    cs = chunk_document("eml_test", txt, {"type": "mail"}, size=500, overlap=80)
    print(f"{len(cs)} chunks")
    for c in cs[:3]:
        print(f"- {c.chunk_id}: {len(c.text)} chars")
