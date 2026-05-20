"""AUTH-03 / Alembic 0003 — pre-migration assertion (Pitfall H).

Wave 0 регрессионный тест: миграция 0003 проходит на ПУСТОЙ api_keys
и явно falls на непустой (manual cleanup signal).
"""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine


async def _reset_schema(pg_url: str) -> None:
    eng = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        await conn.execute(sa.text("CREATE SCHEMA public"))
    await eng.dispose()


@pytest.fixture(autouse=True)
def _clean_schema(pg_url):
    asyncio.run(_reset_schema(pg_url))
    yield


async def _check_lookup_hash_column(pg_url: str) -> str | None:
    eng = create_async_engine(pg_url)
    async with eng.connect() as conn:
        cols = await conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='api_keys' AND column_name='lookup_hash'"
            )
        )
        result = cols.scalar_one_or_none()
    await eng.dispose()
    return result


def test_0003_upgrade_succeeds_on_empty_api_keys(pg_url: str, app_env) -> None:
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    assert asyncio.run(_check_lookup_hash_column(pg_url)) == "lookup_hash"


def test_0003_upgrade_fails_on_seeded_api_keys(pg_url: str, app_env, alembic_at_0002) -> None:
    cfg = Config("alembic.ini")
    with pytest.raises(RuntimeError, match=r"api_keys has \d+ row"):
        command.upgrade(cfg, "0003")
