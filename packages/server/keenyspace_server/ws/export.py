from __future__ import annotations

import asyncio
import os
import secrets
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import BinaryIO

import structlog

log = structlog.get_logger(__name__)

_STREAM_CHUNK_BYTES = 64 * 1024

MAX_EXPORT_UNCOMPRESSED_BYTES = 200 * 1024 * 1024

# G-4: shared with ws/import_.py via direct import. Editing this set updates
# both export's skip rule and import's top-level user-state reject rule —
# keeping the export/import dotfile policy symmetric by construction.
EXPORT_SKIP_TOP_LEVEL: frozenset[str] = frozenset({".obsidian", "logs"})

_EXPORT_BUILD_SLOTS = asyncio.Semaphore(2)


class ExportTooLargeError(ValueError):
    pass


def iter_workspace_files(ws_dir: Path) -> Iterator[tuple[Path, Path]]:
    """Yield (absolute_path, relative_path) tuples for every file in `ws_dir`
    that belongs in the canonical export per D-06.

    Includes: every regular file at any depth EXCEPT entries whose top-level
    relative component is in `EXPORT_SKIP_TOP_LEVEL`.
    """
    for absolute in ws_dir.rglob("*"):
        if not absolute.is_file():
            continue
        try:
            rel = absolute.relative_to(ws_dir)
        except ValueError:
            continue
        parts = rel.parts
        if not parts:
            continue
        if parts[0] in EXPORT_SKIP_TOP_LEVEL:
            continue
        yield absolute, rel


def _total_uncompressed_bytes(ws_dir: Path) -> int:
    total = 0
    for absolute, _ in iter_workspace_files(ws_dir):
        try:
            total += os.path.getsize(absolute)
        except OSError:
            continue
    return total


def _build_zip_sync(ws_dir: Path, tmp_root: Path) -> tuple[BinaryIO, int]:
    tmp_root.mkdir(parents=True, exist_ok=True)
    path = tmp_root / f"export_{secrets.token_hex(8)}.zip"
    fh = path.open("x+b")
    try:
        # Unlink while the handle is open: the inode lives exactly as long as
        # the handle, so a crash, cancelled build, or client disconnect can
        # never leave a stray zip behind under fs_root.
        path.unlink()
        with zipfile.ZipFile(fh, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for absolute, rel in iter_workspace_files(ws_dir):
                zf.write(absolute, rel.as_posix())
        size = fh.tell()
        fh.seek(0)
    except BaseException:
        fh.close()
        raise
    return fh, size


async def _stream_and_close(fh: BinaryIO) -> AsyncIterator[bytes]:
    try:
        while chunk := await asyncio.to_thread(fh.read, _STREAM_CHUNK_BYTES):
            yield chunk
    finally:
        fh.close()


async def build_workspace_zip(
    ws_dir: Path, *, enforce_size_cap: bool = True, tmp_root: Path | None = None
) -> AsyncIterator[bytes]:
    """Build the workspace zip and yield it as 64 KB chunks.

    The zip is built inside `asyncio.to_thread` into an anonymous temp file
    under `tmp_root` (default `<fs_root>/.tmp`, derived from the
    `<fs_root>/workspaces/<uuid>` layout of `ws_dir`), so memory stays flat
    regardless of workspace size. At most two builds run concurrently. When
    `enforce_size_cap` is true, raises `ExportTooLargeError` BEFORE building
    if the uncompressed total exceeds `MAX_EXPORT_UNCOMPRESSED_BYTES`.
    """
    if enforce_size_cap:
        total = await asyncio.to_thread(_total_uncompressed_bytes, ws_dir)
        if total > MAX_EXPORT_UNCOMPRESSED_BYTES:
            raise ExportTooLargeError(
                f"workspace uncompressed size {total} bytes exceeds "
                f"export cap {MAX_EXPORT_UNCOMPRESSED_BYTES} bytes"
            )

    if tmp_root is None:
        tmp_root = ws_dir.parent.parent / ".tmp"

    async with _EXPORT_BUILD_SLOTS:
        fh, zip_bytes = await asyncio.to_thread(_build_zip_sync, ws_dir, tmp_root)

    log.info(
        "workspace.export.zip_built",
        ws_dir=str(ws_dir),
        zip_bytes=zip_bytes,
    )
    return _stream_and_close(fh)
