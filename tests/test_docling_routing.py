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


def setup(monkeypatch, *, scanned, docling_url, docling_md=DOCLING_MD, claude=CLAUDE_TEXT,
          vision_url=None, vlm_text=None):
    calls = []
    monkeypatch.setattr(A, "_extract_pdf", lambda p: ("", True) if scanned else (NATIVE_TEXT, False))
    monkeypatch.setattr(A.cfg, "docling_url", docling_url)
    monkeypatch.setattr(A.cfg, "vision_url", vision_url)
    monkeypatch.setattr(D, "convert_pdf", lambda p, url, log=None: calls.append("docling") or docling_md)
    import parsing.ocr as ocr
    monkeypatch.setattr(ocr, "extract_text_vision_parts",
                        lambda p, key, log=None: calls.append("claude") or ([claude] if claude else []))
    from parsing import vlm_ocr
    monkeypatch.setattr(vlm_ocr, "transcribe_pdf",
                        lambda p, url, key, log=None: calls.append("vision") or (vlm_text or ""))
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


def test_handwritten_scan_goes_to_vision_when_docling_reads_little(pdf, monkeypatch):
    calls = setup(monkeypatch, scanned=True, docling_url="http://docling:5001", claude=None,
                  docling_md="Testament quelques mots", vision_url="http://vision:8000/v1",
                  vlm_text=HANDWRITTEN)
    assert A.extract_text_parts(pdf, api_key="k") == [(HANDWRITTEN, False)]
    assert calls == ["claude", "docling", "vision"]


def test_vision_not_used_when_docling_reads_enough(pdf, monkeypatch):
    many = "mot " * 200
    calls = setup(monkeypatch, scanned=True, docling_url="http://docling:5001", claude=None,
                  docling_md=many, vision_url="http://vision:8000/v1", vlm_text=HANDWRITTEN)
    assert A.extract_text_parts(pdf, api_key=None) == [(many, False)]
    assert "vision" not in calls


def test_trim_loops_cuts_model_repetition():
    from parsing.vlm_ocr import trim_loops
    real = "Article 1 la somme de mille euros est due au créancier. " * 2
    loop = real + "et le présent acte est fait et passé à Nice le " * 40
    out = trim_loops(loop)
    assert out.endswith("[boucle coupée]") and len(out.split()) < 60
    assert trim_loops(real) == real
