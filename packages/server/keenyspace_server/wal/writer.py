from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from ulid import ULID

from .framing import format_entry
from .locks import WorkspaceLockRegistry
from .parser import parse_wal

if TYPE_CHECKING:
    from keenyspace_server.config import Settings


class PayloadTooLargeError(ValueError):
    pass


PayloadTooLarge = PayloadTooLargeError


class WorkspaceArchivedError(ValueError):
    pass


class EmptyContentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AppendResult:
    entry_id: ULID
    ts: datetime


def _newest_logged_id(logs_dir: Path) -> ULID | None:
    log_files = sorted(logs_dir.glob("*.md"))
    if not log_files:
        return None
    entries = parse_wal(log_files[-1].read_text(encoding="utf-8"))
    return max((e.id for e in entries), key=int, default=None)


def _next_entry_id(ts: datetime, last_id: ULID | None) -> ULID:
    # Compile advances its cursor with `id > last_wal_id`; a same-millisecond
    # append (random low bits) or a backward clock step must never mint an id
    # below one already issued, or that entry is silently never compiled.
    candidate = ULID.from_datetime(ts)
    if last_id is not None and int(candidate) <= int(last_id):
        return ULID.from_int(int(last_id) + 1)
    return candidate


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
    settings: Settings,
    locks: WorkspaceLockRegistry,
) -> AppendResult:
    """Append one framed entry to the workspace's daily WAL file.

    Entry ids are strictly increasing per workspace within the process, so a
    compile cursor filtering `id > last_wal_id` never skips an entry.

    Raises EmptyContentError, PayloadTooLargeError or WorkspaceArchivedError.
    """
    if not content.strip():
        raise EmptyContentError("WAL entry content must not be empty")
    max_bytes = settings.wal.max_entry_bytes
    if len(content.encode()) > max_bytes:
        raise PayloadTooLarge(f"Entry content exceeds maximum size of {max_bytes} bytes")
    multi_worker = settings.auth.multi_worker

    # D-01 / D-03: pre-flight Workspace.status check BEFORE lock acquisition. The
    # TOCTOU window (archive flips between this check and lock acquisition) is
    # acceptable per D-03 (DB = source of truth; one stray append after archive
    # has negligible impact and coordinator will be paused within milliseconds).
    # Skip when DB engine hasn't been initialized (unit-test environments without lifespan).
    from keenyspace_server.db.session import get_engine as _get_engine
    if _get_engine() is not None:
        from sqlalchemy import select as _select

        from keenyspace_server.db.models import Workspace as _Workspace
        from keenyspace_server.db.session import get_db_session as _get_db_session
        async with _get_db_session() as _session:
            _status = (await _session.execute(
                _select(_Workspace.status).where(_Workspace.uuid == ws_uuid)
            )).scalar_one_or_none()
        if _status == "archived":
            raise WorkspaceArchivedError(
                f"workspace {ws_uuid} is archived; unarchive before appending"
            )

    ws_lock = await locks.for_workspace(ws_uuid)
    async with ws_lock:
        ts = datetime.now(UTC)
        logs_dir = ws_root / "logs"
        wal_path = logs_dir / f"{ts.date().isoformat()}.md"
        last_id = locks.last_id(ws_uuid)
        if last_id is None:
            last_id = await asyncio.to_thread(_newest_logged_id, logs_dir)
        entry_id = _next_entry_id(ts, last_id)
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
        locks.record_id(ws_uuid, entry_id)

    from keenyspace_server.observability.metrics import WAL_APPENDS_TOTAL
    WAL_APPENDS_TOTAL.labels(workspace=str(ws_uuid), source=source).inc()

    # Phase 2: notify compile coordinator outside the workspace lock scope.
    # Lazy import avoids circular dependency at module init time and keeps
    # Phase 1 tests passing when the compile module is not yet wired into Settings.
    if hasattr(settings, "compile"):
        try:
            from keenyspace_server.compile.coordinator import get_coordinator
        except ImportError:
            pass
        else:
            coordinator = get_coordinator()
            if coordinator is not None:
                coordinator.notify_dirty(ws_uuid)

    return AppendResult(entry_id=entry_id, ts=ts)
