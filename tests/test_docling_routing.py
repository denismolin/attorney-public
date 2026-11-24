"""Docling pour les PDF natifs ; Claude Vision d'abord pour les scans, Docling en repli."""
import pytest

from parsing import attachments as A
from parsing import docling as D
from parsing.eml_parser import Attachment

NATIVE_TEXT = "texte natif " * 20
DOCLING_MD = "# Titre\n\n| Échéance | Montant |\n|---|---|\n| 2024 | 1 000 € |\n" * 3
PRINTED_SCAN = "relevé de compte opération solde " * 40          # Docling lit un scan imprimé
CLAUDE_TEXT = "transcription Claude " * 20
HANDWRITTEN = "Je soussigné lègue à mon neveu la maison de famille " * 15


@pytest.fixture
def pdf(tmp_path, monkeypatch):
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(A, "_abs_path", lambda att: path)
    monkeypatch.setattr(A, "EXTRACTED_TEXT_DIR", tmp_path / "cache")
    return Attachment("d1", "doc.pdf", "application/pdf", 10, "x", "e1")


def setup(monkeypatch, *, scanned, docling_url, docling_md=DOCLING_MD, claude=CLAUDE_TEXT,
          vision_url=None, vlm_text=None, strong=False):
    calls = []
    monkeypatch.setattr(A, "_extract_pdf", lambda p: ("", True) if scanned else (NATIVE_TEXT, False))
    monkeypatch.setattr(A.cfg, "docling_url", docling_url)
    monkeypatch.setattr(A.cfg, "vision_url", vision_url)
    monkeypatch.setattr(D, "convert_pdf",
                        lambda p, url, log=None, scanned=False: calls.append("docling") or docling_md)
    import parsing.ocr as ocr
    monkeypatch.setattr(ocr, "extract_text_vision_parts",
                        lambda p, key, log=None: calls.append("claude") or ([claude] if claude else []))
    from parsing import restore as R
    from parsing import vlm_ocr
    monkeypatch.setattr(vlm_ocr, "transcribe_pdf",
                        lambda p, url, key, log=None: calls.append("vision") or (vlm_text or ""))
    monkeypatch.setattr(vlm_ocr, "vision_is_strong", lambda url, key: strong)
    monkeypatch.setattr(R, "restore", lambda text, log=None: calls.append("restore") or "R:" + text)
    return calls


def restored(text):
    from parsing.restore import NOTE
    return NOTE + "\nR:" + text


def test_native_pdf_uses_docling(pdf, monkeypatch):
    calls = setup(monkeypatch, scanned=False, docling_url="http://docling:5001")
    assert A.extract_text_parts(pdf, api_key="k") == [(DOCLING_MD, False)]
    assert calls == ["docling"]


def test_native_pdf_falls_back_to_pymupdf(pdf, monkeypatch):
    setup(monkeypatch, scanned=False, docling_url="http://docling:5001", docling_md=None)
    assert A.extract_text_parts(pdf, api_key="k") == [(NATIVE_TEXT, False)]


def test_native_pdf_without_docling_unchanged(pdf, monkeypatch):
    calls = setup(monkeypatch, scanned=False, docling_url=None)
    assert A.extract_text_parts(pdf, api_key="k") == [(NATIVE_TEXT, False)]
    assert calls == []


def test_printed_scan_keeps_claude(pdf, monkeypatch):
    calls = setup(monkeypatch, scanned=True, docling_url="http://docling:5001", docling_md=PRINTED_SCAN,
                  vision_url="http://vision:8000/v1", strong=True)
    assert A.extract_text_parts(pdf, api_key="k") == [(CLAUDE_TEXT, False)]
    assert calls == ["docling", "claude"]


def test_printed_scan_without_claude_uses_docling(pdf, monkeypatch):
    calls = setup(monkeypatch, scanned=True, docling_url="http://docling:5001", docling_md=PRINTED_SCAN)
    assert A.extract_text_parts(pdf, api_key=None) == [(PRINTED_SCAN, False)]
    assert calls == ["docling"]


def test_handwritten_goes_to_strong_vision_first_then_restored(pdf, monkeypatch):
    calls = setup(monkeypatch, scanned=True, docling_url="http://docling:5001", docling_md="trois mots lus",
                  vision_url="http://vision:8000/v1", vlm_text=HANDWRITTEN, strong=True)
    assert A.extract_text_parts(pdf, api_key="k") == [(restored(HANDWRITTEN), False)]
    assert calls == ["docling", "vision", "restore"]


def test_handwritten_with_weak_vision_uses_claude_restored(pdf, monkeypatch):
    calls = setup(monkeypatch, scanned=True, docling_url="http://docling:5001", docling_md="trois mots",
                  vision_url="http://vision:8000/v1", vlm_text=HANDWRITTEN, strong=False)
    assert A.extract_text_parts(pdf, api_key="k") == [(restored(CLAUDE_TEXT), False)]
    assert calls == ["docling", "claude", "restore"]


def test_handwritten_without_claude_falls_back_to_any_vision(pdf, monkeypatch):
    calls = setup(monkeypatch, scanned=True, docling_url="http://docling:5001", docling_md="", claude=None,
                  vision_url="http://vision:8000/v1", vlm_text=HANDWRITTEN, strong=False)
    assert A.extract_text_parts(pdf, api_key="k") == [(restored(HANDWRITTEN), False)]
    assert calls == ["docling", "claude", "vision", "restore"]


def test_docling_down_is_not_handwriting(pdf, monkeypatch):
    calls = setup(monkeypatch, scanned=True, docling_url="http://docling:5001", docling_md=None,
                  vision_url="http://vision:8000/v1", vlm_text=HANDWRITTEN, strong=True)
    assert A.extract_text_parts(pdf, api_key="k") == [(CLAUDE_TEXT, False)]
    assert calls == ["docling", "claude"]


def test_restore_rejects_drift(monkeypatch, tmp_path):
    from parsing import restore as R
    monkeypatch.setattr(R, "EXTRACTED_TEXT_DIR", tmp_path)

    class Chat:
        def __init__(self, out): self.out = out
        def complete(self, system, messages, tools=None): return {"text": self.out}

    raw = "Je Sussiqui Nauo lègue la maison de Dracel " * 5
    fixed = raw.replace("Dracel", "Draveil")
    assert R.restore(raw, chat=Chat(fixed), vocabulary=["Draveil"]) == fixed.strip()
    assert R.restore(raw, chat=Chat(raw + " et une phrase inventée " * 10), vocabulary=["x"]) == raw


def test_local_embedder_uses_per_model_url(monkeypatch):
    from dataclasses import replace
    from config import cfg
    from rag import providers as P
    c = replace(cfg, embed_provider="local", embed_model="BAAI/bge-m3",
                local_embed_urls={"BAAI/bge-m3": "http://tei-bge:8080/v1"})
    e = P.get_embedder(c)
    assert str(e._client.base_url).rstrip("/") == "http://tei-bge:8080/v1"
    with pytest.raises(ValueError):
        P.get_embedder(replace(c, embed_model="inconnu"))


HANDWRITTEN = "Je soussigné lègue à mon neveu la maison de famille " * 15


def test_trim_loops_cuts_model_repetition():
    from parsing.vlm_ocr import trim_loops
    real = "Article 1 la somme de mille euros est due au créancier. " * 2
    loop = real + "et le présent acte est fait et passé à Nice le " * 40
    out = trim_loops(loop)
    assert out.endswith("[boucle coupée]") and len(out.split()) < 40
    assert trim_loops(real) == real


def test_trim_loops_keeps_repeated_statement_lines():
    from parsing.vlm_ocr import trim_loops
    lines = "".join(f"PRELEVEMENT SEPA EDF CLIENTS PARTICULIERS REF {i} 45,20 EUR\n" for i in range(30))
    assert trim_loops(lines) == lines


def test_json_to_text_keeps_headers_footers_and_tables():
    doc = {"texts": [
        {"label": "text", "text": "Solde créditeur", "prov": [{"page_no": 1, "bbox": {"t": 500, "coord_origin": "BOTTOMLEFT"}}]},
        {"label": "page_footer", "text": "Édité le 03/01/2019", "prov": [{"page_no": 1, "bbox": {"t": 20}}]},
        {"label": "page_header", "text": "LA BANQUE POSTALE", "prov": [{"page_no": 1, "bbox": {"t": 800}}]},
        {"label": "text", "text": "Page deux", "prov": [{"page_no": 2, "bbox": {"t": 700}}]},
    ], "tables": [{"prov": [{"page_no": 1, "bbox": {"t": 400}}], "data": {"table_cells": [
        {"text": "Date", "start_row_offset_idx": 0, "start_col_offset_idx": 0},
        {"text": "Montant", "start_row_offset_idx": 0, "start_col_offset_idx": 1},
        {"text": "02/01", "start_row_offset_idx": 1, "start_col_offset_idx": 0},
        {"text": "45,20", "start_row_offset_idx": 1, "start_col_offset_idx": 1}]}}]}
    from parsing.docling import json_to_text
    assert json_to_text(doc) == ("LA BANQUE POSTALE\nSolde créditeur\nDate | Montant\n02/01 | 45,20\n"
                                 "Édité le 03/01/2019\n\nPage deux")
