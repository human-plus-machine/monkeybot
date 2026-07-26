"""Tests for knowledge PDF / DOCX / image extraction and indexing."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest

from monkeybot.core.knowledge.captions import path_caption, resolve_image_caption
from monkeybot.core.knowledge.config import resolve_knowledge_settings
from monkeybot.core.knowledge.extractors import (
    extract_docx_text,
    extract_pdf_pages,
    is_indexable_file,
    walk_indexable_files,
)
from monkeybot.core.knowledge.indexer import KnowledgeIndexer
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex
from monkeybot.core.knowledge.types import KnowledgeSettings


def _pdf_bytes(text: str | None = "Hello World") -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    if text is not None:
        font = DictionaryObject()
        font[NameObject("/Type")] = NameObject("/Font")
        font[NameObject("/Subtype")] = NameObject("/Type1")
        font[NameObject("/BaseFont")] = NameObject("/Helvetica")
        resources = DictionaryObject()
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = writer._add_object(font)
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources

        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 24 Tf 10 100 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _docx_bytes(paragraphs: list[str]) -> bytes:
    from docx import Document

    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


_MIN_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_walk_indexable_includes_media(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "guide.pdf").write_bytes(_pdf_bytes("Guide"))
    (tmp_path / "spec.docx").write_bytes(_docx_bytes(["Spec body"]))
    (tmp_path / "logo.png").write_bytes(_MIN_PNG)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.pdf").write_bytes(_pdf_bytes("ignored"))

    found = {p.name for p in walk_indexable_files(tmp_path, max_file_bytes=5_000_000)}
    assert found == {"a.py", "guide.pdf", "spec.docx", "logo.png"}


def test_extract_pdf_pages(tmp_path: Path) -> None:
    path = tmp_path / "doc.pdf"
    path.write_bytes(_pdf_bytes("Contract Clause"))
    pages = extract_pdf_pages(path, max_file_bytes=5_000_000)
    assert pages is not None
    assert len(pages) == 1
    assert pages[0].page == 1
    assert "Contract" in pages[0].text or "Clause" in pages[0].text


def test_extract_pdf_soft_fail_without_pypdf(tmp_path: Path) -> None:
    path = tmp_path / "doc.pdf"
    path.write_bytes(_pdf_bytes("x"))
    with patch.dict("sys.modules", {"pypdf": None}):
        # Force ImportError on `from pypdf import PdfReader`
        import builtins

        real_import = builtins.__import__

        def _block_pypdf(name: str, *args: object, **kwargs: object) -> object:
            if name == "pypdf" or name.startswith("pypdf."):
                raise ImportError("blocked")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        with patch("builtins.__import__", side_effect=_block_pypdf):
            assert extract_pdf_pages(path, max_file_bytes=5_000_000) is None


def test_extract_docx_text(tmp_path: Path) -> None:
    path = tmp_path / "doc.docx"
    path.write_bytes(_docx_bytes(["First paragraph", "Second paragraph"]))
    text = extract_docx_text(path, max_file_bytes=5_000_000)
    assert text is not None
    assert "First paragraph" in text
    assert "Second paragraph" in text


def test_path_caption() -> None:
    assert path_caption("public/images/auth-hero.png") == (
        "Image: public/images/auth-hero.png (auth-hero)"
    )


@pytest.mark.asyncio
async def test_resolve_image_caption_modes(tmp_path: Path) -> None:
    img = tmp_path / "hero.png"
    img.write_bytes(_MIN_PNG)
    cache = tmp_path / "captions"
    assert (
        await resolve_image_caption(
            rel_path="public/hero.png",
            file_path=img,
            mode="off",
            cache_dir=cache,
        )
        is None
    )
    path_cap = await resolve_image_caption(
        rel_path="public/hero.png",
        file_path=img,
        mode="path",
        cache_dir=cache,
    )
    assert path_cap is not None
    assert "hero.png" in path_cap

    async def _fake_vision(_path: Path, _stub: str) -> str | None:
        return "Full-bleed landing hero on dark navy"

    llm_cap = await resolve_image_caption(
        rel_path="public/hero.png",
        file_path=img,
        mode="llm",
        cache_dir=cache,
        vision_fn=_fake_vision,
    )
    assert llm_cap is not None
    assert "landing hero" in llm_cap
    # Cache hit
    again = await resolve_image_caption(
        rel_path="public/hero.png",
        file_path=img,
        mode="llm",
        cache_dir=cache,
        vision_fn=_fake_vision,
    )
    assert again == llm_cap


def test_captions_config_defaults(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    workspace = agent / "workspace"
    workspace.mkdir(parents=True)
    cfg = agent / "monkeybot_config"
    cfg.mkdir()
    (cfg / "monkeybot.yaml").write_text("knowledge:\n  enabled: true\n", encoding="utf-8")
    settings = resolve_knowledge_settings(
        agent_root=agent,
        config_path=cfg / "monkeybot.yaml",
        workspace_root=workspace,
    )
    assert settings.captions == "path"
    assert settings.caption_model is None


def test_captions_config_llm(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    workspace = agent / "workspace"
    workspace.mkdir(parents=True)
    cfg = agent / "monkeybot_config"
    cfg.mkdir()
    (cfg / "monkeybot.yaml").write_text(
        "knowledge:\n  captions: llm\n  caption_model: gpt-4o-mini\n",
        encoding="utf-8",
    )
    settings = resolve_knowledge_settings(
        agent_root=agent,
        config_path=cfg / "monkeybot.yaml",
        workspace_root=workspace,
    )
    assert settings.captions == "llm"
    assert settings.caption_model == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_indexer_indexes_pdf_docx_image(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "guide.pdf").write_bytes(_pdf_bytes("Payment Terms"))
    (ws / "spec.docx").write_bytes(_docx_bytes(["Refund policy details"]))
    (ws / "assets").mkdir()
    (ws / "assets" / "logo.png").write_bytes(_MIN_PNG)

    knowledge = tmp_path / ".monkeybot" / "knowledge"
    knowledge.mkdir(parents=True)
    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge),
        index_path=str(knowledge / "index.sqlite"),
        debounce_ms=0,
        startup_scan=True,
        captions="path",
    )
    index = KnowledgeIndex(Path(settings.index_path))
    await index.open()
    try:
        indexer = KnowledgeIndexer(
            index,
            workspace_root=ws,
            knowledge_root=knowledge,
            settings=settings,
        )
        await indexer.ensure_ready()
        paths = await index.list_paths()
        assert "guide.pdf" in paths
        assert "spec.docx" in paths
        assert "assets/logo.png" in paths

        pdf_chunks = await index.list_chunks_for_path("guide.pdf")
        assert pdf_chunks
        assert pdf_chunks[0].start_line == 1
        assert "PDF page" in pdf_chunks[0].text

        hits = await index.fts_search("Refund", limit=5)
        assert any(h["path"] == "spec.docx" for h in hits)

        logo_hits = await index.fts_search("logo", limit=5)
        assert any(h["path"] == "assets/logo.png" for h in logo_hits)
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_indexer_skips_images_when_captions_off(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "logo.png").write_bytes(_MIN_PNG)
    (ws / "readme.md").write_text("# hi\n", encoding="utf-8")

    knowledge = tmp_path / ".monkeybot" / "knowledge"
    knowledge.mkdir(parents=True)
    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge),
        index_path=str(knowledge / "index.sqlite"),
        debounce_ms=0,
        startup_scan=True,
        captions="off",
    )
    index = KnowledgeIndex(Path(settings.index_path))
    await index.open()
    try:
        indexer = KnowledgeIndexer(
            index,
            workspace_root=ws,
            knowledge_root=knowledge,
            settings=settings,
        )
        await indexer.ensure_ready()
        paths = await index.list_paths()
        assert "readme.md" in paths
        assert "logo.png" not in paths
    finally:
        await index.close()


def test_is_indexable_media(tmp_path: Path) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(_pdf_bytes("x"))
    assert is_indexable_file(pdf, max_file_bytes=5_000_000)
