from __future__ import annotations

import asyncio
import io
import os
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

_STREAM_CHUNK_BYTES = 64 * 1024

MAX_EXPORT_UNCOMPRESSED_BYTES = 200 * 1024 * 1024

EXPORT_SKIP_TOP_LEVEL: frozenset[str] = frozenset({".obsidian", "logs"})


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


def _build_zip_sync(ws_dir: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(
        buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as zf:
        for absolute, rel in iter_workspace_files(ws_dir):
            zf.write(absolute, rel.as_posix())
    return buf.getvalue()


async def build_workspace_zip(
    ws_dir: Path, *, enforce_size_cap: bool = True
) -> AsyncIterator[bytes]:
    """Build the workspace zip and yield it as 64 KB chunks.

    Building runs inside `asyncio.to_thread` to avoid blocking the event loop
    during `zipfile.ZIP_DEFLATED` compression (RESEARCH §Pitfall 2, §Anti-
    Patterns). When `enforce_size_cap` is true, raises `ExportTooLargeError`
    BEFORE building if the uncompressed total exceeds
    `MAX_EXPORT_UNCOMPRESSED_BYTES`.
    """
    if enforce_size_cap:
        total = await asyncio.to_thread(_total_uncompressed_bytes, ws_dir)
        if total > MAX_EXPORT_UNCOMPRESSED_BYTES:
            raise ExportTooLargeError(
                f"workspace uncompressed size {total} bytes exceeds "
                f"export cap {MAX_EXPORT_UNCOMPRESSED_BYTES} bytes"
            )

    data = await asyncio.to_thread(_build_zip_sync, ws_dir)

    async def _generate() -> AsyncIterator[bytes]:
        offset = 0
        length = len(data)
        while offset < length:
            yield data[offset : offset + _STREAM_CHUNK_BYTES]
            offset += _STREAM_CHUNK_BYTES

    log.info(
        "workspace.export.zip_built",
        ws_dir=str(ws_dir),
        zip_bytes=len(data),
    )
    return _generate()
