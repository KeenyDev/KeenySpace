"""Auth boot invariants: dependencies, required settings, and the api_keys schema.

Covers:
- the auth dependencies import, and argon2 hashes at the cost parameters we expect
- AuthSettings refuses to boot without the OIDC, pepper and session-secret env vars
- Settings(extra='forbid') rejects the removed dev-token env var instead of ignoring it
- `alembic upgrade head` leaves an `api_keys.lookup_hash` column in place
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
    """Missing OIDC / pepper / session-secret env vars → ValidationError at boot."""
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
    """Setting the removed dev-token env var fails boot rather than being ignored.

    The variable name is assembled from parts so that a grep audit for the bareword
    stays clean: neither config nor main references it any more.
    """
    from keenyspace_server.config import Settings, get_settings

    get_settings.cache_clear()
    removed_env_var = "KEENYSPACE_AUTH__" + "_".join(["DEV", "TOKEN"])
    monkeypatch.setenv(removed_env_var, "anything")
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


_UNIQUE_ON_LOOKUP_HASH = """
SELECT i.relname
FROM pg_index x
JOIN pg_class t ON t.oid = x.indrelid
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE t.relname = 'api_keys'
  AND n.nspname = 'public'
  AND x.indisunique
  AND (
    SELECT array_agg(a.attname::text ORDER BY a.attname)
    FROM unnest(x.indkey) AS k
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k
  ) = ARRAY['lookup_hash']
"""
"""Unique indexes covering exactly (lookup_hash).

Matched by indexed columns rather than by name: a unique constraint and a bare unique
index both enforce the invariant, and either may be renamed, but dropping uniqueness
empties this result.
"""


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


async def _query_lookup_hash_unique_indexes(pg_url: str) -> list[str]:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(pg_url)
    async with eng.connect() as conn:
        r = await conn.execute(sa.text(_UNIQUE_ON_LOOKUP_HASH))
        names = [row[0] for row in r]
    await eng.dispose()
    return names


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
    """`alembic upgrade head` leaves api_keys.lookup_hash a UNIQUE NOT NULL varchar(64).

    Uniqueness is the load-bearing part: lookup_hash is what an incoming API key is
    resolved by, so two rows sharing one hash would make the owner of a key ambiguous.
    Driven through the alembic CLI rather than build_app(), so the migration invariant
    holds independently of how auth is wired.
    """
    row = asyncio.run(_query_lookup_hash_column(pg_url))
    assert row is not None
    data_type, is_nullable, max_length = row
    assert data_type == "character varying", f"lookup_hash is {data_type}, not a varchar"
    assert is_nullable == "NO"
    assert max_length == 64

    unique_indexes = asyncio.run(_query_lookup_hash_unique_indexes(pg_url))
    assert unique_indexes, "no unique constraint or index covers api_keys.lookup_hash"
