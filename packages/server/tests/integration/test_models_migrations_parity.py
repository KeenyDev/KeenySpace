"""ORM models must describe exactly the schema the Alembic migrations build."""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Any

import pytest

from tests.integration.conftest import SERVER_DIR, _reset_schema

PG_URL = os.environ.get("KEENYSPACE_DB__URL")


def _diff_against_migrated_db() -> list[Any]:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from keenyspace_server.db.models import Base
    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import create_async_engine

    def _compare(sync_conn: Connection) -> list[Any]:
        ctx = MigrationContext.configure(
            sync_conn,
            opts={"compare_type": True, "compare_server_default": True},
        )
        return list(compare_metadata(ctx, Base.metadata))

    async def _run() -> list[Any]:
        eng = create_async_engine(PG_URL or "")
        try:
            async with eng.connect() as conn:
                return await conn.run_sync(_compare)
        finally:
            await eng.dispose()

    return asyncio.run(_run())


@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
def test_models_match_migrated_schema() -> None:
    _reset_schema()
    env = {**os.environ, "KEENYSPACE_DB__URL": PG_URL or ""}
    up = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=SERVER_DIR, env=env, capture_output=True, text=True,
    )
    assert up.returncode == 0, up.stderr

    assert _diff_against_migrated_db() == []
