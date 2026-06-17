from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PG_URL = os.environ.get("KEENYSPACE_DB__URL")
SERVER_DIR = Path(__file__).parent.parent


def _reset_schema() -> None:
    """Start from an empty public schema.

    These tests drive alembic against the shared CI database. Other tests seed
    rows (e.g. api_keys, which migration 0003 guards against being non-empty)
    and leave partial schema state, so a bare upgrade/downgrade cycle is not
    reproducible without first wiping the schema.
    """
    import asyncio

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _drop() -> None:
        eng = create_async_engine(PG_URL or "", isolation_level="AUTOCOMMIT")
        async with eng.connect() as conn:
            await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
            await conn.execute(sa.text("CREATE SCHEMA public"))
        await eng.dispose()

    asyncio.run(_drop())


@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
def test_alembic_upgrade_creates_compile_runs_and_workspace_columns() -> None:
    _reset_schema()
    env = {**os.environ, "KEENYSPACE_DB__URL": PG_URL}
    up = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=SERVER_DIR, env=env, capture_output=True, text=True,
    )
    assert up.returncode == 0, up.stderr

    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _check() -> None:
        eng = create_async_engine(PG_URL)
        async with eng.connect() as conn:
            r = await conn.execute(text("SELECT to_regclass('public.compile_runs')"))
            assert r.scalar() == "compile_runs"
            cols = await conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='workspaces' AND column_name LIKE 'compile_%'"
            ))
            col_names = {row[0] for row in cols.fetchall()}
            assert {"compile_state", "compile_paused_reason", "compile_paused_at"} <= col_names
        await eng.dispose()

    asyncio.run(_check())


@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
def test_alembic_downgrade_reverts_phase2() -> None:
    _reset_schema()
    env = {**os.environ, "KEENYSPACE_DB__URL": PG_URL}
    subprocess.run(["uv", "run", "alembic", "upgrade", "head"], cwd=SERVER_DIR, env=env, check=True)
    # Revert to the pre-phase-2 baseline (0001). A relative "-1" would only
    # undo whatever currently sits at head (now 0003/auth), leaving the phase-2
    # compile_runs table in place; target the baseline revision explicitly so
    # this stays correct as later migrations are added.
    down = subprocess.run(
        ["uv", "run", "alembic", "downgrade", "0001"],
        cwd=SERVER_DIR, env=env, capture_output=True, text=True,
    )
    assert down.returncode == 0, down.stderr

    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _check() -> None:
        eng = create_async_engine(PG_URL)
        async with eng.connect() as conn:
            r = await conn.execute(text("SELECT to_regclass('public.compile_runs')"))
            assert r.scalar() is None
        await eng.dispose()

    asyncio.run(_check())

    subprocess.run(["uv", "run", "alembic", "upgrade", "head"], cwd=SERVER_DIR, env=env, check=True)
