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


async def _reset_schema(pg_url: str) -> None:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        await conn.execute(sa.text("CREATE SCHEMA public"))
    await eng.dispose()


@pytest_asyncio.fixture
async def _engine_lifespan_ctx(app, pg_url):
    """Reset schema -> engine_lifespan (с auto_migrate=true).

    Каждый test получает чистый DB state; полный app_lifespan (scheduler,
    coordinator) НЕ запускается — auth тесты этого не требуют.
    """
    from keenyspace_server.db.session import engine_lifespan

    await _reset_schema(pg_url)
    async with engine_lifespan(app):
        yield


@pytest_asyncio.fixture
async def client(app, _engine_lifespan_ctx, api_key_user):
    """Default client: authenticated через real CompositeAuthBackend + Bearer ks_live_*.

    Wave 2 cutover (D-19/D-21): integration tests proxy через настоящую auth chain,
    no middleware-bypass. Negative-auth assertions используют `anon_client`.
    """
    _, plaintext = api_key_user
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plaintext}"},
    ) as c:
        yield c


@pytest_asyncio.fixture
async def anon_client(app, _engine_lifespan_ctx):
    """Anonymous client для negative tests (auth_bypass, public endpoints)."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def api_key_client(app, _engine_lifespan_ctx, api_key_user):
    """Router-level fast-path: authenticated через test-only AuthenticationBackend stub.

    Используется Wave 1 router tests для изоляции от composite resolver chain
    (быстрая обратная связь без full DB verify roundtrip). Production path
    тестируется через default `client` fixture + integration tests против
    real composite backend.
    """
    from starlette.authentication import AuthCredentials, AuthenticationBackend
    from starlette.middleware.authentication import AuthenticationMiddleware

    from keenyspace_server.auth.user import User

    user_sub, _ = api_key_user

    class _TestAuthBackend(AuthenticationBackend):  # type: ignore[misc]
        async def authenticate(self, conn):  # type: ignore[no-untyped-def]
            path = conn.url.path
            if path.startswith(("/healthz", "/readyz", "/metrics")):
                return None
            return (
                AuthCredentials(["authenticated"]),
                User(sub=user_sub, _display_name=user_sub, source="api_key"),
            )

    for m in app.user_middleware:
        if m.cls is AuthenticationMiddleware:
            m.kwargs["backend"] = _TestAuthBackend()
            break
    app.middleware_stack = app.build_middleware_stack()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def api_key_user(app, _engine_lifespan_ctx):
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
def rsa_keypair():
    """Generate RSA keypair для подписи test JWT (D-20b)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key, pem_private


@pytest.fixture
def mock_authentik_provider(httpserver, rsa_keypair):
    """D-20b — pytest-httpserver mock Authentik (discovery + JWKS + token + end_session).

    Returns dict {issuer, jwks_uri, token_endpoint, end_session_endpoint,
    sign_jwt, httpserver}. sign_jwt(claims, kid="test-kid-1") -> JWT signed via
    RS256.
    """
    from joserfc import jwt as joserfc_jwt
    from joserfc.jwk import RSAKey

    _, pem = rsa_keypair
    rsa_key = RSAKey.import_key(pem)
    public_jwk = rsa_key.as_dict(private=False)
    public_jwk["kid"] = "test-kid-1"

    issuer = httpserver.url_for("/application/o/test").rstrip("/")
    httpserver.expect_request(
        "/application/o/test/.well-known/openid-configuration"
    ).respond_with_json(
        {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "jwks_uri": f"{issuer}/jwks",
            "end_session_endpoint": f"{issuer}/end-session",
            "userinfo_endpoint": f"{issuer}/userinfo",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }
    )
    httpserver.expect_request("/application/o/test/jwks").respond_with_json({"keys": [public_jwk]})

    def sign_jwt(claims: dict, kid: str = "test-kid-1") -> str:
        header = {"alg": "RS256", "kid": kid}
        return joserfc_jwt.encode(header, claims, rsa_key)

    return {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/jwks",
        "token_endpoint": f"{issuer}/token",
        "end_session_endpoint": f"{issuer}/end-session",
        "sign_jwt": sign_jwt,
        "httpserver": httpserver,
    }


@pytest_asyncio.fixture
async def app_with_mocked_authentik(mock_authentik_provider, fs_root, pg_url, monkeypatch):
    """Test app wired to mock Authentik provider."""
    monkeypatch.setenv("KEENYSPACE_DB__URL", pg_url)
    monkeypatch.setenv("KEENYSPACE_FS__ROOT", str(fs_root))
    monkeypatch.setenv("KEENYSPACE_AUTH__OIDC_ISSUER_URL", mock_authentik_provider["issuer"])
    monkeypatch.setenv("KEENYSPACE_AUTH__OIDC_CLIENT_ID", "keenyspace-test")
    monkeypatch.setenv("KEENYSPACE_AUTH__OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv(
        "KEENYSPACE_AUTH__OIDC_REDIRECT_URI",
        "http://test/v1/api/auth/callback",
    )
    monkeypatch.setenv("KEENYSPACE_AUTH__OIDC_POST_LOGOUT_REDIRECT_URI", "http://test/")
    monkeypatch.setenv("KEENYSPACE_AUTH__API_KEY_PEPPER", "test-pepper-32chars-padded-here!")
    monkeypatch.setenv("KEENYSPACE_AUTH__SESSION_SECRET_KEY", "test-session-secret-32chars-pad!")
    monkeypatch.setenv("KEENYSPACE_AUTH__COOKIE_SECURE", "false")
    monkeypatch.setenv("KEENYSPACE_AUTO_MIGRATE", "true")
    from keenyspace_server.config import get_settings

    get_settings.cache_clear()
    from keenyspace_server.db.session import engine_lifespan
    from keenyspace_server.main import build_app

    await _reset_schema(pg_url)
    application = build_app()
    async with engine_lifespan(application):
        yield application, mock_authentik_provider


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
