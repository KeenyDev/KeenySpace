from __future__ import annotations

import base64
import hashlib
import os
import secrets
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


def pytest_configure(config):  # type: ignore[no-untyped-def]
    config.addinivalue_line("markers", "eval: marker for compile evaluation suite (Plans 06-08)")
    config.addinivalue_line(
        "markers", "requires_anthropic: marker for fixtures that hit the real Anthropic API"
    )
    config.addinivalue_line(
        "markers", "slow: subprocess-uvicorn integration test; runs only in full suite"
    )


@pytest.fixture
def fs_root(tmp_path):
    root = tmp_path / "fs_root"
    root.mkdir()
    return root


@pytest.fixture
def pg_url():
    url = os.environ.get(
        "KEENYSPACE_DB__URL",
        "postgresql+asyncpg://postgres:x@localhost:55432/postgres",
    )
    return url


@pytest.fixture
def app_env(fs_root, pg_url, monkeypatch):
    monkeypatch.setenv("KEENYSPACE_DB__URL", pg_url)
    monkeypatch.setenv("KEENYSPACE_FS__ROOT", str(fs_root))
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
    monkeypatch.setenv("KEENYSPACE_AUTO_MIGRATE", "true")
    return {"fs_root": fs_root, "pg_url": pg_url}


@pytest.fixture
def app(app_env):
    import keenyspace_server.config as cfg_module

    cfg_module.get_settings.cache_clear()

    from keenyspace_server.main import build_app

    application = build_app()
    return application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def api_key_user(app):
    """D-20a fast-path API-key fixture — direct DB seed bypassing OIDC.

    Returns: (user_sub: str, plaintext_key: str).
    Wave 0 stub: создаёт users row + api_keys row напрямую через get_db_session.
    Real argon2 hash + lookup_hash вычисляются здесь же (НЕ ждём Wave 1).
    """
    from argon2 import PasswordHasher
    from keenyspace_server.config import get_settings
    from keenyspace_server.db.session import get_db_session
    from sqlalchemy import text

    settings = get_settings()
    pepper = settings.auth.api_key_pepper
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    lookup_hash = hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"test-user-{uuid4().hex[:8]}"
    key_id = uuid4()
    now = datetime.now(UTC)

    async with get_db_session() as session:
        await session.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "test", "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, lookup_hash, "
                "created_at) VALUES (:id, :sub, 'test', 'ks_live_', :h, :lh, :now)"
            ),
            {
                "id": key_id,
                "sub": user_sub,
                "h": argon_hash,
                "lh": lookup_hash,
                "now": now,
            },
        )
        await session.commit()

    return (user_sub, f"ks_live_{body}")


async def _seed_api_keys(pg_url):
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(pg_url)
    async with eng.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, created_at) "
                "VALUES (gen_random_uuid(), 'seed', 'seed', 'ks_live_', 'dummy', NOW())"
            )
        )
    await eng.dispose()


@pytest.fixture
def alembic_at_0002(pg_url, app_env):
    """Применяет миграции до 0002 (НЕ до head) и вставляет seed api_keys row.

    Используется в tests/auth/test_alembic_0003.py для проверки pre-assertion
    (миграция 0003 raises RuntimeError при непустой api_keys table).
    """
    import asyncio

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "0002")
    asyncio.run(_seed_api_keys(pg_url))
    return None
