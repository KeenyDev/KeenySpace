from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from keenyspace_server.compile.scheduler import build_scheduler


def test_build_scheduler_returns_async_io_scheduler() -> None:
    s = build_scheduler()
    assert isinstance(s, AsyncIOScheduler)
    assert s._job_defaults.get("misfire_grace_time") == 30


def test_build_scheduler_can_register_jobs_and_shutdown() -> None:
    s = build_scheduler()

    async def _noop() -> None:
        return None

    s.add_job(_noop, "interval", minutes=15, id="test_backstop", replace_existing=True)
    s.add_job(_noop, "cron", hour=0, minute=0, timezone="UTC", id="test_cron", replace_existing=True)

    job_ids = {j.id for j in s.get_jobs()}
    assert {"test_backstop", "test_cron"} <= job_ids


def test_main_app_lifespan_registers_compile_jobs() -> None:
    import os

    if not os.environ.get("KEENYSPACE_DB__URL"):
        import pytest

        pytest.skip("postgres unavailable")

    import asyncio

    from keenyspace_server.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    from keenyspace_server.main import build_app

    app = build_app()

    async def _drive_lifespan() -> set[str]:
        async with app.router.lifespan_context(app):
            sched = app.state.scheduler
            ids = {j.id for j in sched.get_jobs()}
            return ids

    ids = asyncio.run(_drive_lifespan())
    assert "compile_backstop" in ids
    assert "compile_daily_reset" in ids
