from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from keenyspace_server.compile.coordinator import (
    CompileCoordinator,
    get_coordinator,
    set_coordinator,
)
from keenyspace_server.compile.settings import CompileSettings


@pytest.fixture(autouse=True)
def _clear_coordinator_singleton():
    set_coordinator(None)
    yield
    set_coordinator(None)


def test_notify_dirty_records_workspace_in_sync_context() -> None:
    c = CompileCoordinator(CompileSettings(debounce_seconds=10))
    ws = uuid4()
    c.notify_dirty(ws)
    assert ws in c._dirty
    assert c._pending_debounce.get(ws) is None


@pytest.mark.asyncio
async def test_notify_dirty_schedules_debounce_in_async_context() -> None:
    c = CompileCoordinator(CompileSettings(debounce_seconds=60))
    ws = uuid4()
    c.notify_dirty(ws)
    assert ws in c._dirty
    assert ws in c._pending_debounce


@pytest.mark.asyncio
async def test_notify_dirty_reschedules_on_repeat_call() -> None:
    c = CompileCoordinator(CompileSettings(debounce_seconds=60))
    ws = uuid4()
    c.notify_dirty(ws)
    handle_1 = c._pending_debounce[ws]
    c.notify_dirty(ws)
    handle_2 = c._pending_debounce[ws]
    assert handle_1 is not handle_2
    assert handle_1.cancelled()


@pytest.mark.asyncio
async def test_wal_writer_calls_notify_dirty_outside_lock(tmp_path: Path, monkeypatch) -> None:
    c = CompileCoordinator(CompileSettings(debounce_seconds=60))
    recorded: list = []
    original = c.notify_dirty

    def _spy(ws_uuid):
        recorded.append(ws_uuid)
        original(ws_uuid)

    c.notify_dirty = _spy  # type: ignore[method-assign]
    set_coordinator(c)

    class _FakeSettings:
        class wal:  # noqa: N801 - mimics settings.wal attribute path
            max_entry_bytes = 256 * 1024

        class auth:  # noqa: N801 - mimics settings.auth attribute path
            multi_worker = False

        compile = CompileSettings()

    from keenyspace_server.wal.locks import WorkspaceLockRegistry
    from keenyspace_server.wal.writer import append_log

    ws_uuid = uuid4()
    ws_root = tmp_path / "ws"
    (ws_root / "logs").mkdir(parents=True)
    locks = WorkspaceLockRegistry()
    await append_log(
        ws_uuid=ws_uuid,
        ws_root=ws_root,
        content="hello",
        actor="dev:test",
        source="api",
        client_version=None,
        settings=_FakeSettings(),
        locks=locks,
    )
    assert ws_uuid in recorded


@pytest.mark.asyncio
async def test_wal_writer_no_op_when_coordinator_singleton_none(tmp_path: Path) -> None:
    set_coordinator(None)
    assert get_coordinator() is None

    class _FakeSettings:
        class wal:  # noqa: N801 - mimics settings.wal attribute path
            max_entry_bytes = 256 * 1024

        class auth:  # noqa: N801 - mimics settings.auth attribute path
            multi_worker = False

        compile = CompileSettings()

    from keenyspace_server.wal.locks import WorkspaceLockRegistry
    from keenyspace_server.wal.writer import append_log

    ws_root = tmp_path / "ws"
    (ws_root / "logs").mkdir(parents=True)
    await append_log(
        ws_uuid=uuid4(),
        ws_root=ws_root,
        content="hello-no-coord",
        actor="dev:test",
        source="api",
        client_version=None,
        settings=_FakeSettings(),
        locks=WorkspaceLockRegistry(),
    )
