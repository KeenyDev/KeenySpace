from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path

import pytest
from keenyspace_server.ws.export import (
    ExportTooLargeError,
    build_workspace_zip,
    iter_workspace_files,
)


def _seed_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / ".keenyspace").mkdir(parents=True)
    (ws / "concepts").mkdir()
    (ws / "raw").mkdir()
    (ws / "_templates").mkdir()
    (ws / "index.md").write_text("# index\n")
    (ws / "concepts" / "foo.md").write_text("# foo\n")
    (ws / "raw" / "img.png").write_bytes(b"\x89PNGfake")
    (ws / "_templates" / "concept.md").write_text("# tpl\n")
    (ws / ".keenyspace" / "config.yaml").write_text("uuid: abc\n")
    return ws


def test_iter_workspace_files_includes_md_raw_templates_and_keenyspace(tmp_path):
    ws = _seed_ws(tmp_path)
    rels = {rel.as_posix() for _, rel in iter_workspace_files(ws)}
    for expected in (
        "index.md",
        "concepts/foo.md",
        "raw/img.png",
        "_templates/concept.md",
        ".keenyspace/config.yaml",
    ):
        assert expected in rels, f"{expected} missing from {rels}"


def test_iter_workspace_files_excludes_obsidian_and_logs(tmp_path):
    ws = _seed_ws(tmp_path)
    (ws / ".obsidian").mkdir()
    (ws / ".obsidian" / "workspace.json").write_text("{}")
    (ws / "logs").mkdir()
    (ws / "logs" / "2026-05-21.md").write_text("entry\n")

    rels = {rel.as_posix() for _, rel in iter_workspace_files(ws)}
    assert "index.md" in rels
    assert not any(r.startswith(".obsidian/") for r in rels), rels
    assert not any(r.startswith("logs/") for r in rels), rels


def test_iter_workspace_files_includes_instructions_when_present(tmp_path):
    ws = _seed_ws(tmp_path)
    (ws / ".keenyspace" / "instructions").mkdir()
    (ws / ".keenyspace" / "instructions" / "ingest.md").write_text("---\n---\nbody\n")

    rels = {rel.as_posix() for _, rel in iter_workspace_files(ws)}
    assert ".keenyspace/instructions/ingest.md" in rels


@pytest.mark.asyncio
async def test_build_workspace_zip_yields_bytes_and_reconstructs(tmp_path):
    ws = _seed_ws(tmp_path)
    (ws / ".keenyspace" / "instructions").mkdir()
    (ws / ".keenyspace" / "instructions" / "ingest.md").write_text("body\n")

    gen = await build_workspace_zip(ws)
    chunks = [c async for c in gen]
    assert chunks, "expected at least one chunk"
    blob = b"".join(chunks)

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
        assert "index.md" in names
        assert "concepts/foo.md" in names
        assert "raw/img.png" in names
        assert "_templates/concept.md" in names
        assert ".keenyspace/config.yaml" in names
        assert ".keenyspace/instructions/ingest.md" in names
        assert zf.read("index.md") == b"# index\n"


@pytest.mark.asyncio
async def test_build_workspace_zip_excludes_obsidian_and_logs(tmp_path):
    ws = _seed_ws(tmp_path)
    (ws / ".obsidian").mkdir()
    (ws / ".obsidian" / "workspace.json").write_text("{}")
    (ws / "logs").mkdir()
    (ws / "logs" / "2026-05-21.md").write_text("entry\n")

    gen = await build_workspace_zip(ws)
    blob = b"".join([c async for c in gen])
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
    assert not any(n.startswith(".obsidian/") for n in names), names
    assert not any(n.startswith("logs/") for n in names), names


@pytest.mark.asyncio
async def test_build_workspace_zip_raises_when_over_cap(monkeypatch, tmp_path):
    ws = _seed_ws(tmp_path)
    monkeypatch.setattr(
        "keenyspace_server.ws.export.MAX_EXPORT_UNCOMPRESSED_BYTES", 1
    )
    with pytest.raises(ExportTooLargeError):
        await build_workspace_zip(ws, enforce_size_cap=True)


@pytest.mark.asyncio
async def test_build_workspace_zip_completes_within_timeout(tmp_path):
    ws = _seed_ws(tmp_path)
    gen = await asyncio.wait_for(build_workspace_zip(ws), timeout=10.0)
    blob = b"".join([c async for c in gen])
    assert len(blob) > 0
