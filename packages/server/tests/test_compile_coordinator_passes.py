from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from keenyspace_server.compile import coordinator as coordinator_module
from keenyspace_server.compile.coordinator import CompileCoordinator, set_coordinator
from keenyspace_server.compile.models import CompileDeps, CompilePlan, PageOp
from keenyspace_server.compile.settings import CompileSettings
from keenyspace_server.config import get_settings
from keenyspace_server.db.models import CompileCursor, CompileRun, Workspace
from keenyspace_server.db.session import get_db_session
from keenyspace_shared.loop_detector import LoopDetector
from sqlalchemy import select, text, update

from tests.conftest import _reset_schema

PG_URL = os.environ.get("KEENYSPACE_DB__URL")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"),
]


@dataclass
class FakeCompileAgent:
    """Stands in for the LLM: one page per call, optionally held open by `gate`."""

    output_tokens: int = 10
    gate: asyncio.Event | None = None
    wal_texts: list[str] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(
        self, deps: CompileDeps, *, loop_detector: LoopDetector | None = None, **_: Any
    ) -> tuple[CompilePlan, LoopDetector, int]:
        self.wal_texts.append(deps.wal_text)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        plan = CompilePlan(ops=[
            PageOp(action="create", path=f"notes/pass-{len(self.wal_texts)}.md", body="compiled\n"),
        ])
        return plan, loop_detector or LoopDetector(), self.output_tokens


@pytest.fixture
def fake_agent(monkeypatch: pytest.MonkeyPatch) -> FakeCompileAgent:
    # The coordinator calls the module-level agent entry point directly; there is no
    # injection seam for the LLM, so the name it resolves at call time is replaced.
    agent = FakeCompileAgent()
    monkeypatch.setattr(coordinator_module, "run_compile_agent", agent)
    return agent


async def _seed_api_key() -> str:
    pepper = get_settings().auth.api_key_pepper.get_secret_value()
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    user_sub = f"coord-{uuid4().hex[:8]}"
    now = datetime.now(UTC)
    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :sub, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, lookup_hash, "
                "created_at) VALUES (:id, :sub, 'coord', 'ks_live_', :h, :lh, :now)"
            ),
            {
                "id": uuid4(),
                "sub": user_sub,
                "h": PasswordHasher().hash(body),
                "lh": hashlib.sha256(f"{body}{pepper}".encode()).hexdigest(),
                "now": now,
            },
        )
        await session.commit()
    return f"ks_live_{body}"


@contextlib.asynccontextmanager
async def _serving(
    app: Any, pg_url: str, **settings: Any
) -> AsyncIterator[tuple[AsyncClient, CompileCoordinator]]:
    """Run the app and hand out a coordinator under test with zero debounce.

    The app's own coordinator is detached so WAL appends notify nobody unless a test
    installs the coordinator under test via set_coordinator().
    """
    await _reset_schema(pg_url)
    async with app.router.lifespan_context(app):
        set_coordinator(None)
        coordinator = CompileCoordinator(CompileSettings(**{"debounce_seconds": 0, **settings}))
        app.state.compile_coordinator = coordinator
        api_key = await _seed_api_key()
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
                headers={"Authorization": f"Bearer {api_key}"},
            ) as client:
                yield client, coordinator
        finally:
            await coordinator.aclose()
            set_coordinator(None)


async def _create_workspace(client: AsyncClient) -> tuple[str, UUID]:
    slug = f"coord-{uuid4().hex[:8]}"
    resp = await client.post("/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"})
    assert resp.status_code == 201, resp.text
    return slug, UUID(resp.json()["uuid"])


async def _append(client: AsyncClient, slug: str, content: str) -> str:
    resp = await client.post(
        f"/v1/api/workspaces/{slug}/logs", json={"workspace": slug, "content": content}
    )
    assert resp.status_code == 201, resp.text
    entry_id: str = resp.json()["entry_id"]
    return entry_id


async def _settle(c: CompileCoordinator, *, timeout: float = 10.0) -> None:
    """Wait until the coordinator has no pending timers and no running tasks."""
    async with asyncio.timeout(timeout):
        while c._tasks or c._pending_debounce:
            await asyncio.sleep(0.01)


async def _workspace(ws_uuid: UUID) -> Workspace:
    async with get_db_session() as session:
        return (await session.execute(select(Workspace).where(Workspace.uuid == ws_uuid))).scalar_one()


async def _runs(ws_uuid: UUID) -> list[CompileRun]:
    async with get_db_session() as session:
        return list((await session.execute(
            select(CompileRun)
            .where(CompileRun.workspace_uuid == ws_uuid)
            .order_by(CompileRun.started_at)
        )).scalars().all())


async def _cursor(ws_uuid: UUID) -> str | None:
    async with get_db_session() as session:
        return (await session.execute(
            select(CompileCursor.last_wal_id).where(CompileCursor.workspace_uuid == ws_uuid)
        )).scalar_one_or_none()


async def test_archive_during_inflight_pass_keeps_workspace_archived(
    app: Any, pg_url: str, fake_agent: FakeCompileAgent,
) -> None:
    # Output tokens over the space budget make the pass attempt its post-success pause,
    # which must not overwrite the archive reason.
    fake_agent.output_tokens = 50
    fake_agent.gate = asyncio.Event()
    async with _serving(app, pg_url, max_output_tokens_per_space=5) as (client, coordinator):
        slug, ws_uuid = await _create_workspace(client)
        await _append(client, slug, "fact before archive")

        await coordinator.trigger(ws_uuid, source="test")
        await asyncio.wait_for(fake_agent.entered.wait(), timeout=5)
        archive = await client.post(f"/v1/api/workspaces/{slug}/archive")
        assert archive.status_code == 200, archive.text
        fake_agent.gate.set()
        await _settle(coordinator)
        await coordinator.reset_daily_ceiling()

        ws = await _workspace(ws_uuid)
        assert (ws.status, ws.compile_state, ws.compile_paused_reason) == ("archived", "paused", "archived")
        assert [(r.status, r.completed_at is not None) for r in await _runs(ws_uuid)] == [("success", True)]


async def test_pass_on_paused_workspace_is_skipped_without_run_row(
    app: Any, pg_url: str, fake_agent: FakeCompileAgent,
) -> None:
    async with _serving(app, pg_url) as (client, coordinator):
        slug, ws_uuid = await _create_workspace(client)
        await _append(client, slug, "fact")
        ws_root = await coordinator._workspace_root(ws_uuid)
        assert ws_root is not None
        async with get_db_session() as session:
            await session.execute(
                update(Workspace).where(Workspace.uuid == ws_uuid)
                .values(compile_state="paused", compile_paused_reason="loop_abort")
            )
            await session.commit()

        # Drives the locked pass directly: the pause lands between trigger() and the lock.
        await coordinator._run_locked_pass(ws_uuid, ws_root, str(uuid4()), "test")

        ws = await _workspace(ws_uuid)
        assert (ws.compile_state, ws.compile_paused_reason) == ("paused", "loop_abort")
        assert await _runs(ws_uuid) == []
        assert fake_agent.wal_texts == []


async def test_apply_plan_os_error_finalizes_run_and_pauses_workspace(
    app: Any, pg_url: str, fs_root: Path, fake_agent: FakeCompileAgent,
) -> None:
    async with _serving(app, pg_url) as (client, coordinator):
        slug, ws_uuid = await _create_workspace(client)
        await _append(client, slug, "fact")
        # A directory where the page file must go makes the atomic rename fail with OSError.
        (fs_root / "workspaces" / str(ws_uuid) / "notes" / "pass-1.md").mkdir(parents=True)

        await coordinator.trigger(ws_uuid, source="test")
        await _settle(coordinator)

        ws = await _workspace(ws_uuid)
        assert (ws.compile_state, ws.compile_paused_reason) == ("paused", "internal_error")
        runs = await _runs(ws_uuid)
        assert [(r.status, r.completed_at is not None) for r in runs] == [("abort_error", True)]
        assert runs[0].error_message is not None and "IsADirectoryError" in runs[0].error_message
        assert await _cursor(ws_uuid) is None


async def test_failure_before_agent_releases_workspace_for_retry(
    app: Any, pg_url: str, fake_agent: FakeCompileAgent, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_extract = coordinator_module.extract_wal_slice
    calls = 0

    def _flaky_extract(*args: Any, **kwargs: Any) -> Any:
        # WAL read failure on the first pass only; there is no seam to inject the
        # filesystem into the slice reader, so the name the coordinator calls is swapped.
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("transient read failure")
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(coordinator_module, "extract_wal_slice", _flaky_extract)
    async with _serving(app, pg_url) as (client, coordinator):
        slug, ws_uuid = await _create_workspace(client)
        entry_id = await _append(client, slug, "fact")

        await coordinator.trigger(ws_uuid, source="test")
        await _settle(coordinator)

        ws = await _workspace(ws_uuid)
        assert (ws.compile_state, ws.compile_paused_reason) == ("idle", None)
        assert fake_agent.wal_texts == []

        await coordinator.trigger(ws_uuid, source="backstop")
        await _settle(coordinator)

        assert await _cursor(ws_uuid) == entry_id
        assert [r.status for r in await _runs(ws_uuid)] == ["success"]


async def test_aclose_interrupts_inflight_pass_and_frees_workspace(
    app: Any, pg_url: str, fake_agent: FakeCompileAgent,
) -> None:
    fake_agent.gate = asyncio.Event()
    async with _serving(app, pg_url) as (client, coordinator):
        slug, ws_uuid = await _create_workspace(client)
        await _append(client, slug, "fact")

        await coordinator.trigger(ws_uuid, source="test")
        await asyncio.wait_for(fake_agent.entered.wait(), timeout=5)
        await coordinator.aclose()

        assert (await _workspace(ws_uuid)).compile_state == "idle"
        assert [(r.status, r.completed_at is not None) for r in await _runs(ws_uuid)] == [
            ("abort_interrupted", True)
        ]
        with pytest.raises(ValueError, match="shutting down"):
            await coordinator.trigger(ws_uuid, source="test")


async def test_reconcile_interrupted_clears_running_leftovers(app: Any, pg_url: str) -> None:
    async with _serving(app, pg_url) as (client, coordinator):
        _slug, ws_uuid = await _create_workspace(client)
        async with get_db_session() as session:
            await session.execute(
                update(Workspace).where(Workspace.uuid == ws_uuid).values(compile_state="running")
            )
            session.add(CompileRun(
                id=uuid4(), workspace_uuid=ws_uuid, started_at=datetime.now(UTC), completed_at=None,
                status="running", trigger_source="test", pages_written=0, tokens_input=0,
                tokens_output=0, duration_ms=None, model="m", error_message=None,
            ))
            await session.commit()

        await coordinator.reconcile_interrupted()

        assert (await _workspace(ws_uuid)).compile_state == "idle"
        assert [(r.status, r.completed_at is not None) for r in await _runs(ws_uuid)] == [
            ("abort_interrupted", True)
        ]


async def test_append_during_running_pass_triggers_follow_up_pass(
    app: Any, pg_url: str, fake_agent: FakeCompileAgent,
) -> None:
    fake_agent.gate = asyncio.Event()
    async with _serving(app, pg_url) as (client, coordinator):
        set_coordinator(coordinator)
        slug, ws_uuid = await _create_workspace(client)

        await _append(client, slug, "first fact")
        await asyncio.wait_for(fake_agent.entered.wait(), timeout=5)
        second_id = await _append(client, slug, "second fact")
        fake_agent.gate.set()
        await _settle(coordinator)

        assert len(fake_agent.wal_texts) == 2
        assert "first fact" in fake_agent.wal_texts[0]
        assert "second fact" not in fake_agent.wal_texts[0]
        assert "second fact" in fake_agent.wal_texts[1]
        assert await _cursor(ws_uuid) == second_id
        assert [r.status for r in await _runs(ws_uuid)] == ["success", "success"]


async def test_backlog_over_slice_budget_compiles_in_chunks(
    app: Any, pg_url: str, fake_agent: FakeCompileAgent,
) -> None:
    async with _serving(app, pg_url, max_slice_bytes=1) as (client, coordinator):
        slug, ws_uuid = await _create_workspace(client)
        entry_ids = [await _append(client, slug, f"fact {i}") for i in range(3)]

        await coordinator.trigger(ws_uuid, source="test")
        await _settle(coordinator)

        assert [t.count("<wal_entry") for t in fake_agent.wal_texts] == [1, 1, 1]
        assert await _cursor(ws_uuid) == entry_ids[-1]
        runs = await _runs(ws_uuid)
        assert [(r.status, r.wal_last_id) for r in runs] == [("success", eid) for eid in entry_ids]
        assert (await _workspace(ws_uuid)).compile_state == "idle"


async def test_empty_slice_pass_writes_no_run_row(
    app: Any, pg_url: str, fake_agent: FakeCompileAgent,
) -> None:
    async with _serving(app, pg_url) as (client, coordinator):
        _slug, ws_uuid = await _create_workspace(client)

        await coordinator.trigger(ws_uuid, source="backstop")
        await _settle(coordinator)

        assert await _runs(ws_uuid) == []
        assert (await _workspace(ws_uuid)).compile_state == "idle"
        assert fake_agent.wal_texts == []


async def test_cursor_moved_mid_pass_pauses_workspace(
    app: Any, pg_url: str, fake_agent: FakeCompileAgent,
) -> None:
    async with _serving(app, pg_url) as (client, coordinator):
        slug, ws_uuid = await _create_workspace(client)
        await _append(client, slug, "first fact")
        await coordinator.trigger(ws_uuid, source="test")
        await _settle(coordinator)
        await _append(client, slug, "second fact")
        fake_agent.gate = asyncio.Event()
        fake_agent.entered.clear()

        await coordinator.trigger(ws_uuid, source="test")
        await asyncio.wait_for(fake_agent.entered.wait(), timeout=5)
        async with get_db_session() as session:
            await session.execute(
                update(CompileCursor).where(CompileCursor.workspace_uuid == ws_uuid)
                .values(last_wal_id="0" * 26)
            )
            await session.commit()
        fake_agent.gate.set()
        await _settle(coordinator)

        ws = await _workspace(ws_uuid)
        assert (ws.compile_state, ws.compile_paused_reason) == ("paused", "internal_error")
        assert [r.status for r in await _runs(ws_uuid)] == ["success", "abort_error"]


async def test_resume_keeps_space_budget_tally(
    app: Any, pg_url: str, fake_agent: FakeCompileAgent,
) -> None:
    fake_agent.output_tokens = 50
    async with _serving(app, pg_url, max_output_tokens_per_space=5) as (client, coordinator):
        slug, ws_uuid = await _create_workspace(client)
        await _append(client, slug, "first fact")
        await coordinator.trigger(ws_uuid, source="test")
        await _settle(coordinator)
        assert (await _workspace(ws_uuid)).compile_paused_reason == "space_budget_exceeded"

        await coordinator.resume(ws_uuid)
        await _append(client, slug, "second fact")
        await coordinator.trigger(ws_uuid, source="test")
        await _settle(coordinator)

        ws = await _workspace(ws_uuid)
        assert (ws.compile_state, ws.compile_paused_reason) == ("paused", "space_budget_exceeded")
        assert [r.status for r in await _runs(ws_uuid)] == ["success", "abort_space_budget"]
        assert len(fake_agent.wal_texts) == 1


async def test_resume_endpoint_refuses_archived_workspace(app: Any, pg_url: str) -> None:
    async with _serving(app, pg_url) as (client, _coordinator):
        slug, ws_uuid = await _create_workspace(client)
        archive = await client.post(f"/v1/api/workspaces/{slug}/archive")
        assert archive.status_code == 200, archive.text

        resp = await client.post(f"/v1/api/workspaces/{slug}/compile/resume")

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "workspace_archived"
        ws = await _workspace(ws_uuid)
        assert (ws.compile_state, ws.compile_paused_reason) == ("paused", "archived")


class _SimulatedCrash(BaseException):
    """Process death between the page writes and the cursor advance."""


async def _pending_intent(ws_uuid: UUID) -> tuple[str | None, str | None]:
    async with get_db_session() as session:
        row = (await session.execute(
            select(CompileCursor.pending_wal_last_id, CompileCursor.pending_plan_hash)
            .where(CompileCursor.workspace_uuid == ws_uuid)
        )).one_or_none()
    return (row[0], row[1]) if row is not None else (None, None)


@pytest.mark.parametrize("prior_pass", [False, True], ids=["first-pass", "after-committed-pass"])
async def test_crash_after_apply_replays_intent_without_agent(
    app: Any, pg_url: str, fs_root: Path, fake_agent: FakeCompileAgent,
    monkeypatch: pytest.MonkeyPatch, prior_pass: bool,
) -> None:
    async with _serving(app, pg_url) as (client, coordinator):
        slug, ws_uuid = await _create_workspace(client)
        ws_root = fs_root / "workspaces" / str(ws_uuid)
        expected_runs: list[str] = []
        if prior_pass:
            await _append(client, slug, "committed fact")
            await coordinator.trigger(ws_uuid, source="test")
            await _settle(coordinator)
            expected_runs.append("success")
        entry_id = await _append(client, slug, "fact")
        agent_calls = len(fake_agent.wal_texts)
        page = ws_root / "notes" / f"pass-{agent_calls + 1}.md"

        real_advance = coordinator._advance_cursor

        async def _crash(*_: Any, **__: Any) -> None:
            raise _SimulatedCrash

        monkeypatch.setattr(coordinator, "_advance_cursor", _crash)
        with pytest.raises(_SimulatedCrash):
            await coordinator._run_compile_pass(ws_uuid, ws_root, str(uuid4()), "test")
        expected_runs.append("abort_interrupted")
        written = page.read_bytes()
        # A crash can also land mid-apply; the replay must restore the page either way.
        page.unlink()
        assert (await _pending_intent(ws_uuid))[0] == entry_id

        monkeypatch.setattr(coordinator, "_advance_cursor", real_advance)
        await coordinator.trigger(ws_uuid, source="backstop")
        await _settle(coordinator)
        expected_runs.append("success")

        assert len(fake_agent.wal_texts) == agent_calls + 1
        assert page.read_bytes() == written
        assert await _cursor(ws_uuid) == entry_id
        assert await _pending_intent(ws_uuid) == (None, None)
        runs = await _runs(ws_uuid)
        assert [r.status for r in runs] == expected_runs
        assert (runs[-1].wal_last_id, runs[-1].pages_written, runs[-1].tokens_output) == (entry_id, 1, 0)
        assert (await _workspace(ws_uuid)).compile_state == "idle"


async def test_replayed_plan_keeps_frontmatter_key_order(
    app: Any, pg_url: str, fs_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontmatter = {"zeta": 1, "alpha": ["x"], "title": "Ordered"}

    async def _agent(
        deps: CompileDeps, *, loop_detector: LoopDetector | None = None, **_: Any
    ) -> tuple[CompilePlan, LoopDetector, int]:
        plan = CompilePlan(ops=[
            PageOp(action="create", path="notes/ordered.md", body="body\n", frontmatter=frontmatter),
        ])
        return plan, loop_detector or LoopDetector(), 1

    monkeypatch.setattr(coordinator_module, "run_compile_agent", _agent)
    async with _serving(app, pg_url) as (client, coordinator):
        slug, ws_uuid = await _create_workspace(client)
        ws_root = fs_root / "workspaces" / str(ws_uuid)
        await _append(client, slug, "fact")

        async def _crash(*_: Any, **__: Any) -> None:
            raise _SimulatedCrash

        real_advance = coordinator._advance_cursor
        monkeypatch.setattr(coordinator, "_advance_cursor", _crash)
        with pytest.raises(_SimulatedCrash):
            await coordinator._run_compile_pass(ws_uuid, ws_root, str(uuid4()), "test")
        page = ws_root / "notes" / "ordered.md"
        written = page.read_bytes()
        page.unlink()

        monkeypatch.setattr(coordinator, "_advance_cursor", real_advance)
        await coordinator._run_compile_pass(ws_uuid, ws_root, str(uuid4()), "test")

        assert page.read_bytes() == written
        assert written.index(b"zeta") < written.index(b"alpha") < written.index(b"title")


async def test_failed_apply_discards_intent_so_resume_recompiles(
    app: Any, pg_url: str, fs_root: Path, fake_agent: FakeCompileAgent,
) -> None:
    async with _serving(app, pg_url) as (client, coordinator):
        slug, ws_uuid = await _create_workspace(client)
        entry_id = await _append(client, slug, "fact")
        blocker = fs_root / "workspaces" / str(ws_uuid) / "notes" / "pass-1.md"
        blocker.mkdir(parents=True)

        await coordinator.trigger(ws_uuid, source="test")
        await _settle(coordinator)
        assert await _pending_intent(ws_uuid) == (None, None)

        blocker.rmdir()
        await coordinator.resume(ws_uuid)
        await coordinator.trigger(ws_uuid, source="test")
        await _settle(coordinator)

        assert len(fake_agent.wal_texts) == 2
        assert await _cursor(ws_uuid) == entry_id
        assert [r.status for r in await _runs(ws_uuid)] == ["abort_error", "success"]


async def test_backstop_caps_concurrent_agent_runs(
    app: Any, pg_url: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    running = 0
    peak = 0
    calls = 0

    async def _agent(
        deps: CompileDeps, *, loop_detector: LoopDetector | None = None, **_: Any
    ) -> tuple[CompilePlan, LoopDetector, int]:
        nonlocal running, peak, calls
        running += 1
        calls += 1
        peak = max(peak, running)
        try:
            await gate.wait()
        finally:
            running -= 1
        plan = CompilePlan(ops=[PageOp(action="create", path="notes/cap.md", body="compiled\n")])
        return plan, loop_detector or LoopDetector(), 1

    monkeypatch.setattr(coordinator_module, "run_compile_agent", _agent)
    async with _serving(app, pg_url, max_concurrent_passes=2) as (client, coordinator):
        workspaces = [await _create_workspace(client) for _ in range(4)]
        entry_ids = {ws_uuid: await _append(client, slug, "fact") for slug, ws_uuid in workspaces}

        await coordinator.backstop_all_workspaces()
        async with asyncio.timeout(5):
            while running < 2:
                await asyncio.sleep(0.01)
        # Give the remaining passes every chance to (wrongly) enter the agent.
        await asyncio.sleep(0.2)
        assert (running, calls) == (2, 2)

        gate.set()
        await _settle(coordinator)

        assert peak == 2
        assert calls == 4
        for ws_uuid, entry_id in entry_ids.items():
            assert await _cursor(ws_uuid) == entry_id


async def _archive_keeping_compile_idle(ws_uuid: UUID) -> None:
    # An archived row that is not compile-paused: the state a lost pause or a manual
    # edit leaves behind, and the one the status gate exists for.
    async with get_db_session() as session:
        await session.execute(
            update(Workspace).where(Workspace.uuid == ws_uuid)
            .values(status="archived", archived_at=datetime.now(UTC), compile_state="idle")
        )
        await session.commit()


async def test_archived_workspace_is_never_claimed(
    app: Any, pg_url: str, fs_root: Path, fake_agent: FakeCompileAgent,
) -> None:
    async with _serving(app, pg_url) as (client, coordinator):
        slug, ws_uuid = await _create_workspace(client)
        await _append(client, slug, "fact")
        await _archive_keeping_compile_idle(ws_uuid)
        ws_root = fs_root / "workspaces" / str(ws_uuid)

        assert (await coordinator.trigger(ws_uuid, source="http_api")).status == "paused"
        await coordinator.backstop_all_workspaces()
        await _settle(coordinator)
        result = await coordinator._run_compile_pass(ws_uuid, ws_root, str(uuid4()), "append")

        assert result.status == "paused"
        assert fake_agent.wal_texts == []
        assert await _runs(ws_uuid) == []
        assert not (ws_root / "notes").exists()
        assert (await _workspace(ws_uuid)).compile_state == "idle"


async def test_pending_intent_is_kept_while_archived_and_replayed_after_unarchive(
    app: Any, pg_url: str, fs_root: Path, fake_agent: FakeCompileAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _serving(app, pg_url) as (client, coordinator):
        slug, ws_uuid = await _create_workspace(client)
        ws_root = fs_root / "workspaces" / str(ws_uuid)
        entry_id = await _append(client, slug, "fact")

        async def _crash(*_: Any, **__: Any) -> None:
            raise _SimulatedCrash

        real_advance, real_claim = coordinator._advance_cursor, coordinator._claim_running
        monkeypatch.setattr(coordinator, "_advance_cursor", _crash)
        with pytest.raises(_SimulatedCrash):
            await coordinator._run_compile_pass(ws_uuid, ws_root, str(uuid4()), "test")
        monkeypatch.setattr(coordinator, "_advance_cursor", real_advance)
        page = ws_root / "notes" / "pass-1.md"
        written = page.read_bytes()
        page.unlink()
        archive = await client.post(f"/v1/api/workspaces/{slug}/archive")
        assert archive.status_code == 200, archive.text

        async def _claim_before_archive(ws: UUID) -> bool:
            # The archive commits between the claim and the replay.
            return True

        monkeypatch.setattr(coordinator, "_claim_running", _claim_before_archive)
        result = await coordinator._run_compile_pass(ws_uuid, ws_root, str(uuid4()), "backstop")
        monkeypatch.setattr(coordinator, "_claim_running", real_claim)

        assert result.status == "paused"
        assert not page.exists()
        assert (await _pending_intent(ws_uuid))[0] == entry_id
        assert await _cursor(ws_uuid) is None
        assert [r.status for r in await _runs(ws_uuid)] == ["abort_interrupted"]
        ws = await _workspace(ws_uuid)
        assert (ws.status, ws.compile_state, ws.compile_paused_reason) == ("archived", "paused", "archived")

        unarchive = await client.post(f"/v1/api/workspaces/{slug}/unarchive")
        assert unarchive.status_code == 200, unarchive.text
        await coordinator.trigger(ws_uuid, source="backstop")
        await _settle(coordinator)

        assert len(fake_agent.wal_texts) == 1
        assert page.read_bytes() == written
        assert await _cursor(ws_uuid) == entry_id
        assert await _pending_intent(ws_uuid) == (None, None)
        assert [r.status for r in await _runs(ws_uuid)] == ["abort_interrupted", "success"]
