"""AUTH-03 / Alembic 0003 — pre-migration assertion (Pitfall H).

Wave 0 регрессионный тест: миграция 0003 проходит на ПУСТОЙ api_keys
и явно falls на непустой (manual cleanup signal).
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_0003_upgrade_succeeds_on_empty_api_keys(pg_url: str, app_env) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url.replace("+asyncpg", ""))
    command.upgrade(cfg, "head")
    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        cols = await conn.execute(sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='api_keys' AND column_name='lookup_hash'"
        ))
        assert cols.scalar_one_or_none() == "lookup_hash"
    await engine.dispose()


def test_0003_upgrade_fails_on_seeded_api_keys(
    pg_url: str, app_env, alembic_at_0002
) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url.replace("+asyncpg", ""))
    with pytest.raises(RuntimeError, match=r"api_keys has \d+ row"):
        command.upgrade(cfg, "0003")
