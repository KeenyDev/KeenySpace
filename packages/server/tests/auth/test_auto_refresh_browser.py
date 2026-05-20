"""AUTH-06 + D-03 inline auto-refresh — browser path с cookie ks_at near expiry.

T-3-45 mitigation: stale ks_at в browser session НЕ приводит к user-visible 401
если ks_rt valid; FastAPI Depends(refresh_if_needed) rotate'ает cookies inline.
"""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_browser_path_inline_refresh_when_at_near_expiry(
    app_with_mocked_authentik,
    pg_url,
) -> None:
    """ks_at exp - 30s (внутри 60s threshold) + valid ks_rt → 200 + Set-Cookie."""
    app, provider = app_with_mocked_authentik
    issuer = provider["issuer"]
    near_expiry_at = provider["sign_jwt"](
        {
            "iss": issuer,
            "aud": "keenyspace-test",
            "sub": "u-refresh",
            "preferred_username": "alice",
            "email": "a@x",
            "iat": int(time.time()) - 3570,
            "exp": int(time.time()) + 30,
        }
    )
    new_at = provider["sign_jwt"](
        {
            "iss": issuer,
            "aud": "keenyspace-test",
            "sub": "u-refresh",
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

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES ('u-refresh', 'alice', 'a@x', 'oidc', now()) "
                "ON CONFLICT (sub) DO NOTHING"
            )
        )
        await conn.commit()
    await engine.dispose()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"ks_at": near_expiry_at, "ks_rt": "rt-original"},
        follow_redirects=False,
    ) as c:
        resp = await c.get("/v1/api/auth/api-keys")
    assert resp.status_code != 401, f"expected inline refresh, got 401: {resp.text[:200]}"
    assert resp.status_code == 200
    set_cookies = resp.headers.get_list("set-cookie")
    assert any("ks_at=" in c and new_at in c for c in set_cookies), (
        f"new ks_at not in Set-Cookie; got {set_cookies}"
    )


@pytest.mark.asyncio
async def test_browser_path_no_refresh_when_at_fresh(
    app_with_mocked_authentik,
    pg_url,
) -> None:
    """Sanity: ks_at exp далеко в будущем → НЕ trigger refresh + no Set-Cookie."""
    app, provider = app_with_mocked_authentik
    issuer = provider["issuer"]
    fresh = provider["sign_jwt"](
        {
            "iss": issuer,
            "aud": "keenyspace-test",
            "sub": "u-fresh",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3500,
        }
    )

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES ('u-fresh', 'fresh', NULL, 'oidc', now()) "
                "ON CONFLICT (sub) DO NOTHING"
            )
        )
        await conn.commit()
    await engine.dispose()

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"ks_at": fresh, "ks_rt": "rt-x"},
    ) as c:
        resp = await c.get("/v1/api/auth/api-keys")
    assert resp.status_code in {200, 404}
    set_cookies = resp.headers.get_list("set-cookie")
    assert not any("ks_at=" in c for c in set_cookies), (
        f"ks_at unexpectedly rotated when fresh; {set_cookies}"
    )
