from __future__ import annotations

import asyncio
import io
import os
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

    gen = await build_workspace_zip(ws, tmp_root=tmp_path / ".tmp")
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

    gen = await build_workspace_zip(ws, tmp_root=tmp_path / ".tmp")
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
        await build_workspace_zip(ws, enforce_size_cap=True, tmp_root=tmp_path / ".tmp")


@pytest.mark.asyncio
async def test_build_workspace_zip_completes_within_timeout(tmp_path):
    ws = _seed_ws(tmp_path)
    gen = await asyncio.wait_for(
        build_workspace_zip(ws, tmp_root=tmp_path / ".tmp"), timeout=10.0
    )
    blob = b"".join([c async for c in gen])
    assert len(blob) > 0


@pytest.mark.asyncio
async def test_build_workspace_zip_leaves_no_temp_file_on_disk(tmp_path):
    ws = _seed_ws(tmp_path)
    tmp_root = tmp_path / ".tmp"
    gen = await build_workspace_zip(ws, tmp_root=tmp_root)
    assert list(tmp_root.iterdir()) == []
    blob = b"".join([c async for c in gen])
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert zf.read("index.md") == b"# index\n"
    assert list(tmp_root.iterdir()) == []


@pytest.mark.asyncio
async def test_cancelled_export_holds_slot_until_build_ends_and_closes_handle(
    monkeypatch, tmp_path
):
    import threading

    import keenyspace_server.ws.export as export_mod
    from keenyspace_server.ws.thread_slots import LoopLocalSemaphore

    real_build = export_mod._build_zip_sync
    release_build = threading.Event()
    handles = []

    def _blocking_build(ws_dir, tmp_root):
        release_build.wait(timeout=5)
        fh, size = real_build(ws_dir, tmp_root)
        handles.append(fh)
        return fh, size

    slots = LoopLocalSemaphore(1)
    monkeypatch.setattr(export_mod, "_build_zip_sync", _blocking_build)
    monkeypatch.setattr(export_mod, "_EXPORT_BUILD_SLOTS", slots)
    ws = _seed_ws(tmp_path)

    task = asyncio.create_task(build_workspace_zip(ws, tmp_root=tmp_path / ".tmp"))
    while not slots.get().locked():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slots.get().locked()

    release_build.set()
    async with asyncio.timeout(5):
        while slots.get().locked() or not handles or not handles[0].closed:
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_build_workspace_zip_closes_handle_on_early_disconnect(monkeypatch, tmp_path):
    import keenyspace_server.ws.export as export_mod

    real_build = export_mod._build_zip_sync
    handles = []

    def _capture(ws_dir, tmp_root):
        fh, size = real_build(ws_dir, tmp_root)
        handles.append(fh)
        return fh, size

    monkeypatch.setattr(export_mod, "_build_zip_sync", _capture)
    ws = _seed_ws(tmp_path)
    (ws / "raw" / "big.bin").write_bytes(os.urandom(512 * 1024))
    gen = await build_workspace_zip(ws, tmp_root=tmp_path / ".tmp")
    assert await anext(gen)
    assert not handles[0].closed
    await gen.aclose()
    assert handles[0].closed


@pytest.mark.asyncio
async def test_export_builds_are_limited_to_two_concurrent(monkeypatch, tmp_path):
    import threading
    import time

    import keenyspace_server.ws.export as export_mod
    from keenyspace_server.ws.thread_slots import LoopLocalSemaphore

    real_build = export_mod._build_zip_sync
    lock = threading.Lock()
    active = 0
    peak = 0

    def _slow_build(ws_dir, tmp_root):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        try:
            return real_build(ws_dir, tmp_root)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(export_mod, "_build_zip_sync", _slow_build)
    monkeypatch.setattr(export_mod, "_EXPORT_BUILD_SLOTS", LoopLocalSemaphore(2))
    ws = _seed_ws(tmp_path)
    gens = await asyncio.gather(
        *(build_workspace_zip(ws, tmp_root=tmp_path / ".tmp") for _ in range(6))
    )
    for gen in gens:
        await gen.aclose()
    assert peak == 2
