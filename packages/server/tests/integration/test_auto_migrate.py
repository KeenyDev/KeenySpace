"""
Postgres-gated regression test for the KEENYSPACE_AUTO_MIGRATE gate (DB-02).

Positive case:  auto_migrate=true on an empty schema -> 7 baseline tables exist.
Negative case:  auto_migrate=false on an empty schema -> no tables created.

Both tests skip cleanly when Postgres is unavailable. They drop+recreate the public
schema before booting the lifespan to guarantee a hermetic empty starting state.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio


EXPECTED_TABLES = {
    "workspaces",
    "users",
    "sessions",
    "api_keys",
    "audit_log",
    "blueprints",
    "compile_cursors",
}


def _asyncpg_url(sa_url: str) -> str:
    return sa_url.replace("postgresql+asyncpg://", "postgresql://")


def _set_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__OIDC_ISSUER_URL",
        "http://localhost:9999/application/o/test/",
    )
    monkeypatch.setenv("KEENYSPACE_AUTH__OIDC_CLIENT_ID", "test-client")
    monkeypatch.setenv("KEENYSPACE_AUTH__OIDC_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__OIDC_REDIRECT_URI",
        "http://localhost:8000/v1/api/auth/callback",
    )
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__OIDC_POST_LOGOUT_REDIRECT_URI",
        "http://localhost:8000/",
    )
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__API_KEY_PEPPER",
        "test-pepper-32chars-padded-here!",
    )
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__SESSION_SECRET_KEY",
        "test-session-secret-32chars-pad!",
    )
    monkeypatch.setenv("KEENYSPACE_AUTH__COOKIE_SECURE", "false")


async def _try_connect(asyncpg_url: str) -> asyncpg.Connection | None:
    try:
        return await asyncio.wait_for(asyncpg.connect(asyncpg_url), timeout=2.0)
    except OSError, TimeoutError, asyncpg.exceptions.PostgresError:
        return None


async def _reset_public_schema(conn: asyncpg.Connection) -> None:
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


async def _list_public_tables(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    )
    return {r["table_name"] for r in rows}


@pytest.mark.timeout(60)
async def test_auto_migrate_creates_baseline_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sa_url = os.environ.get(
        "KEENYSPACE_DB__URL",
        "postgresql+asyncpg://postgres:x@localhost:55432/postgres",
    )
    asyncpg_url = _asyncpg_url(sa_url)

    conn = await _try_connect(asyncpg_url)
    if conn is None:
        pytest.skip("postgres unavailable on configured KEENYSPACE_DB__URL")
    try:
        await _reset_public_schema(conn)
    finally:
        await conn.close()

    fs_root = tmp_path / "fs_root"
    fs_root.mkdir()

    monkeypatch.setenv("KEENYSPACE_DB__URL", sa_url)
    monkeypatch.setenv("KEENYSPACE_FS__ROOT", str(fs_root))
    _set_auth_env(monkeypatch)
    monkeypatch.setenv("KEENYSPACE_AUTO_MIGRATE", "true")

    import keenyspace_server.config as cfg_module

    cfg_module.get_settings.cache_clear()

    from keenyspace_server.main import build_app

    app = build_app()

    async with app.router.lifespan_context(app):
        verify_conn = await asyncpg.connect(asyncpg_url)
        try:
            actual = await _list_public_tables(verify_conn)
        finally:
            await verify_conn.close()

    cfg_module.get_settings.cache_clear()

    missing = EXPECTED_TABLES - actual
    assert not missing, f"Missing tables after auto_migrate=true: {missing}"
    assert "workspace_users" not in actual, "workspace_users must NOT exist in v1 baseline (DB-06)"


@pytest.mark.timeout(60)
async def test_auto_migrate_false_does_not_run_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sa_url = os.environ.get(
        "KEENYSPACE_DB__URL",
        "postgresql+asyncpg://postgres:x@localhost:55432/postgres",
    )
    asyncpg_url = _asyncpg_url(sa_url)

    conn = await _try_connect(asyncpg_url)
    if conn is None:
        pytest.skip("postgres unavailable on configured KEENYSPACE_DB__URL")
    try:
        await _reset_public_schema(conn)
    finally:
        await conn.close()

    fs_root = tmp_path / "fs_root"
    fs_root.mkdir()

    monkeypatch.setenv("KEENYSPACE_DB__URL", sa_url)
    monkeypatch.setenv("KEENYSPACE_FS__ROOT", str(fs_root))
    _set_auth_env(monkeypatch)
    monkeypatch.setenv("KEENYSPACE_AUTO_MIGRATE", "false")

    import keenyspace_server.config as cfg_module

    cfg_module.get_settings.cache_clear()

    from keenyspace_server.main import build_app

    app = build_app()

    async with app.router.lifespan_context(app):
        verify_conn = await asyncpg.connect(asyncpg_url)
        try:
            actual = await _list_public_tables(verify_conn)
        finally:
            await verify_conn.close()

    cfg_module.get_settings.cache_clear()

    leaked = EXPECTED_TABLES & actual
    assert not leaked, f"auto_migrate=false should leave schema empty, but found: {leaked}"
