"""OIDC end-to-end flows — login, bearer, refresh and logout — against a mock Authentik.

The IdP is a pytest-httpserver stub signing real JWTs, so the browser and bearer paths
run through the production OidcClient without network egress.
"""

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
    """The login redirect carries a PKCE S256 code_challenge."""
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
    """Full browser flow: login → callback → ks_at + ks_rt cookies and a users row."""
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
                        "scope": "openid profile email groups",
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
    """Authorization: Bearer <JWT> validates against the IdP JWKS."""
    app, provider = app_with_mocked_authentik
    issuer = provider["issuer"]
    token = provider["sign_jwt"](
        {
            "iss": issuer,
            "aud": "keenyspace-test",
            "scope": "openid profile email groups",
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
    """alg=none is not in the accepted-algorithm list, so the token is rejected."""
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
    """ks_oidc_session is scoped to path=/v1/api/auth, so it is never sent to /v1/mcp."""
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
    """The explicit /refresh endpoint rotates cookies, given an api-key auth + ks_rt cookie."""
    app, provider = app_with_mocked_authentik
    issuer = provider["issuer"]
    new_at = provider["sign_jwt"](
        {
            "iss": issuer,
            "aud": "keenyspace-test",
            "scope": "openid profile email groups",
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
    """Negative: the IdP token endpoint answering 401 makes /refresh fail with 401."""
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
    """With a ks_idt cookie, logout redirects (302) to the IdP end_session endpoint."""
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
            "scope": "openid profile email groups",
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
    """Degraded path: with no ks_idt, logout clears locally and 302s to post_logout_redirect."""
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
