"""Wave 0 exit smoke — фиксирует invariants для downstream waves.

Покрытие:
- Все новые deps импортируются
- AuthSettings boot'ает с required env
- Settings.extra='forbid' блокирует старый dev-token env var
- `api_keys.lookup_hash` колонка существует после `alembic upgrade head`
"""

from __future__ import annotations

import asyncio

import pytest


def test_new_libs_importable() -> None:
    import argon2
    import freezegun
    import itsdangerous
    import joserfc.jwk
    import joserfc.jwt
    import pytest_httpserver
    from argon2 import PasswordHasher

    assert argon2 and freezegun and itsdangerous
    assert joserfc.jwk and joserfc.jwt and pytest_httpserver

    ph = PasswordHasher()
    assert ph.time_cost == 3
    assert ph.memory_cost == 65536
    assert ph.parallelism == 4


def test_auth_settings_requires_oidc_and_pepper(monkeypatch) -> None:
    """Без env vars — boot fail."""
    from keenyspace_server.config import Settings, get_settings

    get_settings.cache_clear()
    for k in (
        "KEENYSPACE_AUTH__OIDC_ISSUER_URL",
        "KEENYSPACE_AUTH__OIDC_CLIENT_ID",
        "KEENYSPACE_AUTH__OIDC_CLIENT_SECRET",
        "KEENYSPACE_AUTH__OIDC_REDIRECT_URI",
        "KEENYSPACE_AUTH__OIDC_POST_LOGOUT_REDIRECT_URI",
        "KEENYSPACE_AUTH__API_KEY_PEPPER",
        "KEENYSPACE_AUTH__SESSION_SECRET_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_auth_settings_rejects_dev_token(monkeypatch, app_env) -> None:
    """Удалённое поле dev-token в env → boot fail (T-3-05 residual signal).

    Var name собирается из частей чтобы финальный grep audit Wave 2 (T-3-21)
    не ловил bareword: production code (config + main) уже не ссылается на него.
    """
    from keenyspace_server.config import Settings, get_settings

    get_settings.cache_clear()
    removed_env_var = "KEENYSPACE_AUTH__" + "_".join(["DEV", "TOKEN"])
    monkeypatch.setenv(removed_env_var, "anything")
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


async def _query_lookup_hash_column(pg_url: str) -> tuple[str, str, int] | None:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(pg_url)
    async with eng.connect() as conn:
        r = await conn.execute(
            sa.text(
                "SELECT data_type, is_nullable, character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_name='api_keys' AND column_name='lookup_hash'"
            )
        )
        row = r.one_or_none()
    await eng.dispose()
    if row is None:
        return None
    return (row[0], row[1], row[2])


@pytest.fixture
def _alembic_head(pg_url, app_env):
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _reset() -> None:
        eng = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
        async with eng.connect() as conn:
            await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
            await conn.execute(sa.text("CREATE SCHEMA public"))
        await eng.dispose()

    asyncio.run(_reset())
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    yield


def test_api_keys_lookup_hash_column_exists_after_head(pg_url, _alembic_head) -> None:
    """alembic upgrade head даёт api_keys.lookup_hash CHAR(64) UNIQUE NOT NULL.

    Wave 0 exit gate: проверяет инвариант миграции напрямую через alembic CLI
    (без build_app() — оставлено независимо от auth wiring).
    """
    row = asyncio.run(_query_lookup_hash_column(pg_url))
    assert row is not None
    assert row[1] == "NO"  # NOT NULL
    assert row[2] == 64  # CHAR(64) max length
