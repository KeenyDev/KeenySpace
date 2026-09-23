"""Helpers shared by the alembic-driving integration tests.

These three modules (test_compile_alembic, test_migration_0005,
test_models_migrations_parity) shell out to ``uv run alembic`` and need the
server package root as cwd plus an empty ``public`` schema to start from.
"""

from __future__ import annotations

import os
from pathlib import Path

PG_URL = os.environ.get("KEENYSPACE_DB__URL")

SERVER_DIR = Path(__file__).resolve().parents[2]
"""packages/server — the cwd `alembic.ini` is resolved against."""


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
