"""Docling pour les PDF natifs ; Claude Vision d'abord pour les scans, Docling en repli."""
import pytest

from parsing import attachments as A
from parsing import docling as D
from parsing.eml_parser import Attachment

NATIVE_TEXT = "texte natif " * 20
DOCLING_MD = "# Titre\n\n| Échéance | Montant |\n|---|---|\n| 2024 | 1 000 € |\n" * 3
CLAUDE_TEXT = "transcription Claude " * 20


@pytest.fixture
def pdf(tmp_path, monkeypatch):
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(A, "_abs_path", lambda att: path)
    monkeypatch.setattr(A, "EXTRACTED_TEXT_DIR", tmp_path / "cache")
    return Attachment("d1", "doc.pdf", "application/pdf", 10, "x", "e1")


def setup(monkeypatch, *, scanned, docling_url, docling_md=DOCLING_MD, claude=CLAUDE_TEXT):
    calls = []
    monkeypatch.setattr(A, "_extract_pdf", lambda p: ("", True) if scanned else (NATIVE_TEXT, False))
    monkeypatch.setattr(A.cfg, "docling_url", docling_url)
    monkeypatch.setattr(D, "convert_pdf", lambda p, url, log=None: calls.append("docling") or docling_md)
    import parsing.ocr as ocr
    monkeypatch.setattr(ocr, "extract_text_vision_parts",
                        lambda p, key, log=None: calls.append("claude") or ([claude] if claude else []))
    return calls


def test_native_pdf_uses_docling(pdf, monkeypatch):
    calls = setup(monkeypatch, scanned=False, docling_url="http://docling:5001")
    assert A.extract_text_parts(pdf, api_key="k") == [(DOCLING_MD, False)]
    assert calls == ["docling"]


def test_native_pdf_falls_back_to_pymupdf(pdf, monkeypatch):
    setup(monkeypatch, scanned=False, docling_url="http://docling:5001", docling_md="")
    assert A.extract_text_parts(pdf, api_key="k") == [(NATIVE_TEXT, False)]


def test_native_pdf_without_docling_unchanged(pdf, monkeypatch):
    calls = setup(monkeypatch, scanned=False, docling_url=None)
    assert A.extract_text_parts(pdf, api_key="k") == [(NATIVE_TEXT, False)]
    assert calls == []


def test_scan_prefers_claude(pdf, monkeypatch):
    calls = setup(monkeypatch, scanned=True, docling_url="http://docling:5001")
    assert A.extract_text_parts(pdf, api_key="k") == [(CLAUDE_TEXT, False)]
    assert calls == ["claude"]


def test_scan_falls_back_to_docling_without_key(pdf, monkeypatch):
    calls = setup(monkeypatch, scanned=True, docling_url="http://docling:5001")
    assert A.extract_text_parts(pdf, api_key=None) == [(DOCLING_MD, False)]
    assert calls == ["docling"]


def test_scan_falls_back_to_docling_when_claude_fails(pdf, monkeypatch):
    calls = setup(monkeypatch, scanned=True, docling_url="http://docling:5001", claude=None)
    assert A.extract_text_parts(pdf, api_key="k") == [(DOCLING_MD, False)]
    assert calls == ["claude", "docling"]
