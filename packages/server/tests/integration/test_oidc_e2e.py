"""AUTH-01/02/04/06/07 OIDC e2e — pytest-httpserver mock Authentik."""

from __future__ import annotations

import time
import urllib.parse

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_login_redirects_to_authentik(app_with_mocked_authentik) -> None:
    app, _provider = app_with_mocked_authentik
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        resp = await c.get("/v1/api/auth/login")
    assert resp.status_code == 302
    loc = resp.headers["location"]
    parsed = urllib.parse.urlparse(loc)
    q = urllib.parse.parse_qs(parsed.query)
    assert q["code_challenge_method"] == ["S256"]
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["keenyspace-test"]


@pytest.mark.asyncio
async def test_pkce_s256_present(app_with_mocked_authentik) -> None:
    """AUTH-04: PKCE S256 challenge мн в login redirect."""
    app, _provider = app_with_mocked_authentik
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        resp = await c.get("/v1/api/auth/login")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"]
    assert "code_challenge" in q


@pytest.mark.asyncio
async def test_login_callback_sets_cookies_and_upserts_user(
    app_with_mocked_authentik, pg_url
) -> None:
    """AUTH-01: full browser flow login → callback → ks_at+ks_rt cookies + users row."""
    app, provider = app_with_mocked_authentik
    issuer = provider["issuer"]
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        login_resp = await c.get("/v1/api/auth/login")
        loc = login_resp.headers["location"]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
        state = params["state"][0]
        nonce = params["nonce"][0]

        provider["httpserver"].expect_request("/application/o/test/token").respond_with_json(
            {
                "access_token": provider["sign_jwt"](
                    {
                        "iss": issuer,
                        "aud": "keenyspace-test",
                        "sub": "u-1",
                        "preferred_username": "alice",
                        "email": "a@x",
                        "iat": int(time.time()),
                        "exp": int(time.time()) + 3600,
                    }
                ),
                "refresh_token": "rt-mock-001",
                "refresh_expires_in": 86400 * 14,
                "id_token": provider["sign_jwt"](
                    {
                        "iss": issuer,
                        "aud": "keenyspace-test",
                        "sub": "u-1",
                        "preferred_username": "alice",
                        "email": "a@x",
                        "nonce": nonce,
                        "iat": int(time.time()),
                        "exp": int(time.time()) + 3600,
                    }
                ),
                "expires_in": 3600,
                "token_type": "Bearer",
                "userinfo": {
                    "sub": "u-1",
                    "preferred_username": "alice",
                    "email": "a@x",
                },
            }
        )
        cb_resp = await c.get(
            f"/v1/api/auth/callback?code=test-code&state={state}",
        )

    assert cb_resp.status_code == 302, cb_resp.text[:200]
    cookies = {ck.name: ck.value for ck in cb_resp.cookies.jar}
    assert "ks_at" in cookies
    assert "ks_rt" in cookies

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.text("SELECT sub, display_name, source FROM users WHERE sub='u-1'")
        )
        row = result.one()
    await engine.dispose()
    assert row[0] == "u-1" and row[1] == "alice" and row[2] == "oidc"


@pytest.mark.asyncio
async def test_oidc_bearer_validates_via_jwks(app_with_mocked_authentik, pg_url) -> None:
    """AUTH-04: Authorization: Bearer <JWT> path."""
    app, provider = app_with_mocked_authentik
    issuer = provider["issuer"]
    token = provider["sign_jwt"](
        {
            "iss": issuer,
            "aud": "keenyspace-test",
            "sub": "u-bearer",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
    )
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES ('u-bearer', 'b', NULL, 'oidc', now()) "
                "ON CONFLICT (sub) DO NOTHING"
            )
        )
        await conn.commit()
    await engine.dispose()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        resp = await c.get("/v1/api/auth/api-keys")
    assert resp.status_code != 401, resp.text[:200]


@pytest.mark.asyncio
async def test_invalid_jwt_alg_none_returns_401(app_with_mocked_authentik) -> None:
    """T-3-31 — alg=none не в whitelist; токен отвергнут."""
    app, _ = app_with_mocked_authentik
    bad = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJoYWNrIn0."
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {bad}"},
    ) as c:
        resp = await c.get("/v1/api/auth/api-keys")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_session_cookie_path_scoped(app_with_mocked_authentik) -> None:
    """T-3-37: ks_oidc_session cookie path=/v1/api/auth — не уходит на /v1/mcp."""
    app, _ = app_with_mocked_authentik
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        login_resp = await c.get("/v1/api/auth/login")
    set_cookies = login_resp.headers.get_list("set-cookie")
    session_cookies = [c for c in set_cookies if "ks_oidc_session" in c]
    assert session_cookies, f"no ks_oidc_session cookie in {set_cookies}"
    assert any("path=/v1/api/auth" in c.lower() for c in session_cookies), (
        f"ks_oidc_session path not scoped: {session_cookies}"
    )


@pytest.mark.asyncio
async def test_refresh_rotates_cookies(app_with_mocked_authentik, pg_url) -> None:
    """AUTH-06 explicit /refresh endpoint via authed api-key + ks_rt cookie."""
    app, provider = app_with_mocked_authentik
    issuer = provider["issuer"]
    new_at = provider["sign_jwt"](
        {
            "iss": issuer,
            "aud": "keenyspace-test",
            "sub": "u-4",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
    )
    provider["httpserver"].expect_request("/application/o/test/token").respond_with_json(
        {
            "access_token": new_at,
            "refresh_token": "rt-rotated",
            "id_token": new_at,
            "expires_in": 3600,
            "refresh_expires_in": 86400 * 14,
            "token_type": "Bearer",
        }
    )

    # Seed an api_key for authed /refresh access
    import base64
    import hashlib
    import secrets
    from datetime import UTC, datetime
    from uuid import uuid4

    import sqlalchemy as sa
    from argon2 import PasswordHasher
    from sqlalchemy.ext.asyncio import create_async_engine

    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    pepper = "test-pepper-32chars-padded-here!"
    lookup_hash = hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"u-refresh-{uuid4().hex[:8]}"
    now = datetime.now(UTC)
    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, 'r', NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "now": now},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, lookup_hash, "
                "created_at) VALUES (:id, :sub, 'r', 'ks_live_', :h, :lh, :now)"
            ),
            {
                "id": uuid4(),
                "sub": user_sub,
                "h": argon_hash,
                "lh": lookup_hash,
                "now": now,
            },
        )
        await conn.commit()
    await engine.dispose()
    plaintext = f"ks_live_{body}"

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plaintext}"},
    ) as c:
        resp = await c.post("/v1/api/auth/refresh", cookies={"ks_rt": "rt-001"})
    assert resp.status_code == 200, resp.text[:200]
    set_cookies = resp.headers.get_list("set-cookie")
    assert any("ks_at=" in c for c in set_cookies)
    assert any("ks_rt=" in c for c in set_cookies)


@pytest.mark.asyncio
async def test_refresh_failure_returns_401(app_with_mocked_authentik, pg_url) -> None:
    """AUTH-06 negative: token endpoint returns 401 → refresh fails."""
    app, provider = app_with_mocked_authentik
    provider["httpserver"].expect_request("/application/o/test/token").respond_with_data(
        "invalid", status=401
    )

    import base64
    import hashlib
    import secrets
    from datetime import UTC, datetime
    from uuid import uuid4

    import sqlalchemy as sa
    from argon2 import PasswordHasher
    from sqlalchemy.ext.asyncio import create_async_engine

    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    pepper = "test-pepper-32chars-padded-here!"
    lookup_hash = hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"u-fail-{uuid4().hex[:8]}"
    now = datetime.now(UTC)
    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, 'r', NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "now": now},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, lookup_hash, "
                "created_at) VALUES (:id, :sub, 'r', 'ks_live_', :h, :lh, :now)"
            ),
            {
                "id": uuid4(),
                "sub": user_sub,
                "h": argon_hash,
                "lh": lookup_hash,
                "now": now,
            },
        )
        await conn.commit()
    await engine.dispose()
    plaintext = f"ks_live_{body}"

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plaintext}"},
    ) as c:
        resp = await c.post("/v1/api/auth/refresh", cookies={"ks_rt": "bad-rt"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_logout_calls_end_session(app_with_mocked_authentik, pg_url) -> None:
    """AUTH-07: ks_idt cookie → logout_url → end_session 302."""
    app, provider = app_with_mocked_authentik
    issuer = provider["issuer"]
    id_token = provider["sign_jwt"](
        {
            "iss": issuer,
            "aud": "keenyspace-test",
            "sub": "u-3",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
    )
    at_token = provider["sign_jwt"](
        {
            "iss": issuer,
            "aud": "keenyspace-test",
            "sub": "u-3",
            "preferred_username": "carol",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
    )

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES ('u-3', 'carol', NULL, 'oidc', now()) "
                "ON CONFLICT (sub) DO NOTHING"
            )
        )
        await conn.commit()
    await engine.dispose()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        resp = await c.post(
            "/v1/api/auth/logout",
            cookies={"ks_idt": id_token, "ks_at": at_token},
        )
    assert resp.status_code == 302
    assert "end-session" in resp.headers["location"]


@pytest.mark.asyncio
async def test_logout_no_id_token_still_clears(app_with_mocked_authentik, pg_url) -> None:
    """T-3-36 degraded: без ks_idt — local clear + 302 на post_logout_redirect."""
    app, _ = app_with_mocked_authentik

    import base64
    import hashlib
    import secrets
    from datetime import UTC, datetime
    from uuid import uuid4

    import sqlalchemy as sa
    from argon2 import PasswordHasher
    from sqlalchemy.ext.asyncio import create_async_engine

    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    pepper = "test-pepper-32chars-padded-here!"
    lookup_hash = hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"u-logout-{uuid4().hex[:8]}"
    now = datetime.now(UTC)
    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, 'l', NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "now": now},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, lookup_hash, "
                "created_at) VALUES (:id, :sub, 'l', 'ks_live_', :h, :lh, :now)"
            ),
            {
                "id": uuid4(),
                "sub": user_sub,
                "h": argon_hash,
                "lh": lookup_hash,
                "now": now,
            },
        )
        await conn.commit()
    await engine.dispose()
    plaintext = f"ks_live_{body}"

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plaintext}"},
        follow_redirects=False,
    ) as c:
        resp = await c.post("/v1/api/auth/logout")
    assert resp.status_code == 302
    set_cookies = resp.headers.get_list("set-cookie")
    assert any("ks_at=" in c for c in set_cookies)
