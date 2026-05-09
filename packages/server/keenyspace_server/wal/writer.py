from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from ulid import ULID

from .framing import format_entry
from .locks import WorkspaceLockRegistry


class PayloadTooLargeError(ValueError):
    pass


PayloadTooLarge = PayloadTooLargeError


def _blocking_append(wal_file: Path, payload: bytes, multi_worker: bool) -> None:
    wal_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(wal_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        if multi_worker:
            fcntl.flock(fd, fcntl.LOCK_EX)
        pre_size = os.lseek(fd, 0, os.SEEK_END)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        except BaseException:
            os.ftruncate(fd, pre_size)
            raise
    finally:
        os.close(fd)


async def append_log(
    *,
    ws_uuid: UUID,
    ws_root: Path,
    content: str,
    actor: str,
    source: str,
    client_version: str | None,
    parent_id: ULID | None = None,
    settings: object,
    locks: WorkspaceLockRegistry,
) -> ULID:
    max_bytes: int = getattr(getattr(settings, "wal", settings), "max_entry_bytes", 256 * 1024)
    multi_worker: bool = getattr(getattr(settings, "auth", settings), "multi_worker", False)

    ws_lock = await locks.for_workspace(ws_uuid)
    async with ws_lock:
        wal_path = ws_root / "logs" / f"{datetime.now(UTC).date().isoformat()}.md"
        ts = datetime.now(UTC)
        entry_id = ULID.from_datetime(ts)
        content_hash = "sha256:" + hashlib.sha256(content.encode()).hexdigest()

        payload = format_entry(
            entry_id=entry_id,
            ts=ts,
            actor=actor,
            source=source,
            client_version=client_version,
            content_hash=content_hash,
            parent_id=parent_id,
            content=content,
        )

        if len(payload) > max_bytes:
            raise PayloadTooLarge(
                f"Serialised entry exceeds maximum size of {max_bytes} bytes"
            )

        await asyncio.to_thread(
            _blocking_append, wal_path, payload, multi_worker
        )

    from keenyspace_server.observability.metrics import WAL_APPENDS_TOTAL
    WAL_APPENDS_TOTAL.labels(workspace=str(ws_uuid), source=source).inc()

    return entry_id
