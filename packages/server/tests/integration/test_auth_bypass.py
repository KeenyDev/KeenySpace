"""Anonymous callers must never reach a protected route — the real backend, no bypass.

Swept over `app.router.routes`; together these tests guarantee:
  - every non-public route answers 401 to an anonymous caller
  - the api-keys, refresh and logout endpoints are in that sweep, not silently missing
  - the superseded /v1/admin/api-keys stub is absent from the route table
  - an expired or malformed ks_at cookie → 401
  - a revoked API key → 401
  - this module's WHITELIST stays a superset of CompositeAuthBackend.PUBLIC_PREFIXES

Fixtures: `app` (function-scoped, lifespan + DB ready), `anon_client` (anonymous),
`client` (authenticated ks_live_* Bearer) and `app_with_mocked_authentik` (mock IdP, for
the expired-JWT cases) — all from conftest.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

WHITELIST = {
    "/healthz",
    "/readyz",
    "/.well-known/oauth-protected-resource",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/v1/api/auth/discovery",
    "/v1/api/auth/login",
    "/v1/api/auth/callback",
}


def _collect_routes(application) -> list[tuple[str, str]]:
    result = []
    for route in application.routes:
        if isinstance(route, APIRoute):
            path = route.path
            skip = any(path.startswith(p) for p in WHITELIST)
            if skip:
                continue
            methods = route.methods or {"GET"}
            for method in methods:
                result.append((method, path))
    result.append(("POST", "/v1/mcp/"))
    return result


@pytest.mark.asyncio
async def test_anonymous_gets_401_on_all_routes(app, _engine_lifespan_ctx, anon_client):
    """Middleware-bypass regression: every non-public path 401s for an anonymous caller."""
    routes = _collect_routes(app)
    assert len(routes) > 0, "No routes found to test"

    for method, path in routes:
        template_path = path.replace("{slug}", "test-ws").replace("{path:path}", "index")
        resp = await anon_client.request(method, template_path)
        assert resp.status_code == 401, (
            f"{method} {path} -> expected 401 (anonymous), got {resp.status_code}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_header",
    [
        None,
        "",
        "Bearer ",
        "malformed",
        "Bearer wrong-format",
        "bearer ks_live_xxx",  # lowercase scheme
        "Bearer ks_live_",  # empty body — composite still tries verify, DB miss -> 401
        "Bearer ks_live_invalid-but-44-chars-no-match-AAAAAAAA",  # well-formed, unknown
        "Bearer not_ks_live_some.jwt.like",  # reaches oidc_bearer; not a real JWT -> 401
    ],
)
async def test_bearer_edge_cases(app, _engine_lifespan_ctx, auth_header: str | None):
    """Composite resolver chain: every credential edge case 401s, never silent anonymous."""
    headers = {}
    if auth_header is not None:
        headers["Authorization"] = auth_header

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        resp = await c.post(
            "/v1/api/workspaces/",
            json={"slug": "test-auth-edge", "blueprint": "default"},
        )
        assert resp.status_code == 401, (
            f"auth={auth_header!r} expected 401, got {resp.status_code}: {resp.text[:200]}"
        )


@pytest.mark.asyncio
async def test_authenticated_with_api_key_reaches_routes(client, api_key_user):
    """A valid ks_live_* Bearer gets through: the sweep above is not 401-ing everything.

    The `client` fixture authenticates via the real CompositeAuthBackend, no bypass.
    """
    resp = await client.get("/v1/api/auth/api-keys")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_revoked_api_key_returns_401(client, api_key_user):
    """A revoked api_key (revoked_at IS NOT NULL) makes verify return None → 401."""
    list_resp = await client.get("/v1/api/auth/api-keys")
    assert list_resp.status_code == 200
    items = list_resp.json()
    assert items, "expected at least one api key for the user (api_key_user fixture seed)"
    key_id = items[0]["id"]

    del_resp = await client.delete(f"/v1/api/auth/api-keys/{key_id}")
    assert del_resp.status_code == 204

    post_resp = await client.get("/v1/api/auth/api-keys")
    assert post_resp.status_code == 401, (
        f"revoked key should yield 401, got {post_resp.status_code}: {post_resp.text}"
    )


def test_admin_stub_removed(app):
    """The superseded /v1/admin/api-keys stub is no longer in app.routes."""
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/v1/admin/api-keys" not in paths


def test_whitelist_is_superset_of_backend_public_prefixes():
    """The backend's PUBLIC_PREFIXES must stay a subset of this module's WHITELIST.

    WHITELIST also covers FastAPI internals (/docs, /openapi.json, /redoc), which never
    reach AuthenticationMiddleware because they are BaseRoute, not APIRoute;
    PUBLIC_PREFIXES is the auth-side bypass. Widening the backend constant without
    widening the test is caught here.
    """
    from keenyspace_server.auth.composite import PUBLIC_PREFIXES

    assert set(PUBLIC_PREFIXES).issubset(WHITELIST)


def test_collected_routes_include_the_auth_endpoints(app):
    """The auth endpoints really are in the anonymous-401 sweep, under the expected paths.

    The sweep reads routes off `app.router.routes`, so a protected endpoint that was
    never mounted would pass it vacuously. This pins that the API-key CRUD, refresh and
    logout routes are collected, and that the public /login and /callback are not.
    """
    collected = {(m, p) for m, p in _collect_routes(app)}
    # API-key CRUD under /v1/api/auth/api-keys
    assert ("POST", "/v1/api/auth/api-keys") in collected
    assert ("GET", "/v1/api/auth/api-keys") in collected
    assert ("DELETE", "/v1/api/auth/api-keys/{key_id}") in collected
    # refresh + logout (authed; cookie- or ks_live-driven)
    assert ("POST", "/v1/api/auth/refresh") in collected
    assert ("POST", "/v1/api/auth/logout") in collected
    # the superseded /v1/admin/api-keys stub
    paths_only = {p for _, p in collected}
    assert "/v1/admin/api-keys" not in paths_only
    # /login + /callback are public (in WHITELIST, hence deliberately not swept)
    assert ("GET", "/v1/api/auth/login") not in collected
    assert ("GET", "/v1/api/auth/callback") not in collected


@pytest.mark.asyncio
async def test_expired_jwt_in_cookie_returns_401(app_with_mocked_authentik) -> None:
    """A ks_at cookie past exp plus the 30s leeway → 401.

    joserfc JWTClaimsRegistry(leeway=30) in OidcClient.validate_access_token rejects a
    token whose `exp` passed more than 30 seconds ago. A cookie carrying such a JWT
    reaches the `_try_cookie` resolver → InvalidTokenError → the composite chain tries
    api_key (no Bearer) then oidc_bearer (no Bearer) → AuthenticationError → 401.
    """
    import time

    application, provider = app_with_mocked_authentik
    issuer = provider["issuer"]
    expired_token = provider["sign_jwt"](
        {
            "iss": issuer,
            "aud": "keenyspace-test",
            "scope": "openid profile email groups",
            "sub": "u-exp",
            "iat": int(time.time()) - 7200,
            "exp": int(time.time()) - 100,  # 100s past exp (>leeway 30s)
        }
    )
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"ks_at": expired_token},
    ) as c:
        resp = await c.get("/v1/api/auth/api-keys")
    assert resp.status_code == 401, (
        f"expired ks_at should yield 401, got {resp.status_code}: {resp.text[:200]}"
    )


@pytest.mark.asyncio
async def test_malformed_cookie_returns_401(app_with_mocked_authentik) -> None:
    """A malformed ks_at cookie (not a JWT at all) → 401.

    The `_try_cookie` resolver catches DecodeError / InvalidTokenError / any parse
    failure and returns None, so the composite chain falls through to 401.
    """
    application, _ = app_with_mocked_authentik
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"ks_at": "not.a.jwt"},
    ) as c:
        resp = await c.get("/v1/api/auth/api-keys")
    assert resp.status_code == 401, (
        f"malformed ks_at should yield 401, got {resp.status_code}: {resp.text[:200]}"
    )
