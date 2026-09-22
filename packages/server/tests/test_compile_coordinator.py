from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from keenyspace_server.compile.coordinator import (
    CompileCoordinator,
    get_coordinator,
    set_coordinator,
)
from keenyspace_server.compile.settings import CompileSettings


def test_singleton_set_and_get() -> None:
    try:
        assert get_coordinator() is None
        c = CompileCoordinator(CompileSettings())
        set_coordinator(c)
        assert get_coordinator() is c
    finally:
        set_coordinator(None)


def test_notify_dirty_records_workspace() -> None:
    c = CompileCoordinator(CompileSettings())
    ws = uuid4()
    c.notify_dirty(ws)
    assert ws in c._dirty


@pytest.mark.asyncio
async def test_trigger_returns_paused_when_workspace_state_is_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    c = CompileCoordinator(CompileSettings())

    async def _fake_root(self: CompileCoordinator, ws_uuid: UUID) -> Path:
        return Path("/tmp")

    async def _fake_state(self: CompileCoordinator, ws_uuid: UUID) -> str:
        return "paused"

    monkeypatch.setattr(CompileCoordinator, "_workspace_root", _fake_root)
    monkeypatch.setattr(CompileCoordinator, "_workspace_state", _fake_state)

    resp = await c.trigger(uuid4(), source="http_api")
    assert resp.status == "paused"


@pytest.mark.asyncio
async def test_trigger_refuses_archived_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    c = CompileCoordinator(CompileSettings())

    async def _fake_root(self: CompileCoordinator, ws_uuid: UUID) -> Path:
        return Path("/tmp")

    async def _fake_state(self: CompileCoordinator, ws_uuid: UUID) -> str:
        return "archived"

    monkeypatch.setattr(CompileCoordinator, "_workspace_root", _fake_root)
    monkeypatch.setattr(CompileCoordinator, "_workspace_state", _fake_state)

    resp = await c.trigger(uuid4(), source="backstop")
    assert resp.status == "paused"
    assert c._tasks == set()


@pytest.mark.asyncio
async def test_trigger_raises_when_workspace_root_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    c = CompileCoordinator(CompileSettings())

    async def _fake_root_none(self: CompileCoordinator, ws_uuid: UUID) -> Path | None:
        return None

    monkeypatch.setattr(CompileCoordinator, "_workspace_root", _fake_root_none)

    with pytest.raises(ValueError, match="filesystem directory does not exist"):
        await c.trigger(uuid4(), source="http_api")


@pytest.mark.asyncio
async def test_concurrent_trigger_reuses_inflight_job_id() -> None:
    c = CompileCoordinator(CompileSettings())
    ws = uuid4()
    c._inflight[ws] = "in-flight-run-id"
    c._locks[ws]  # prime the registry
    async with c._locks[ws]:
        assert c._locks[ws].locked()
        assert c._inflight.get(ws) == "in-flight-run-id"


@pytest.mark.asyncio
async def test_trigger_racing_shutdown_spawns_no_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    c = CompileCoordinator(CompileSettings())
    state_read_started = asyncio.Event()
    release_state_read = asyncio.Event()

    async def _fake_root(self: CompileCoordinator, ws_uuid: UUID) -> Path:
        return Path("/tmp")

    async def _slow_state(self: CompileCoordinator, ws_uuid: UUID) -> str:
        state_read_started.set()
        await release_state_read.wait()
        return "idle"

    monkeypatch.setattr(CompileCoordinator, "_workspace_root", _fake_root)
    monkeypatch.setattr(CompileCoordinator, "_workspace_state", _slow_state)

    pending_trigger = asyncio.create_task(c.trigger(uuid4(), source="http_api"))
    await state_read_started.wait()
    await c.aclose()
    release_state_read.set()

    with pytest.raises(ValueError, match="shutting down"):
        await pending_trigger
    assert c._tasks == set()
