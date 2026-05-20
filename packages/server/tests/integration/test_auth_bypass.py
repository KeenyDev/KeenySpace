"""AUTH-08 regression — graduated against real CompositeAuthBackend (Wave 2).

Покрытие:
- Anonymous request → 401 на ВСЕ non-public routes (T-3-20 middleware bypass)
- Bearer edge cases (malformed / wrong format / non-ks_live) → 401 (T-3-23)
- Valid Bearer ks_live_* → не-401/403 (positive case Pattern 8 step 4-5)
- Revoked API key → 401 (T-3-26)
- WHITELIST в test суперсет CompositeAuthBackend PUBLIC_PREFIXES (T-3-25 drift)
- /v1/admin/api-keys удалён (Phase 2 stub removed)

Fixtures: `app` (function-scoped с lifespan + DB ready) + `anon_client` (anonymous)
+ `client` (authenticated ks_live_* Bearer) — все из conftest.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

WHITELIST = {
    "/healthz",
    "/readyz",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/redoc",
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
    """T-3-20: middleware bypass regression — каждый non-public path 401 для anon."""
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
        "Bearer not_ks_live_some.jwt.like",  # validates через oidc_bearer Wave 3; Wave 2 -> 401
    ],
)
async def test_bearer_edge_cases(app, _engine_lifespan_ctx, auth_header: str | None):
    """T-3-23: composite resolver chain — все edge cases 401, no silent anonymous."""
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
    """P-10 step 5: valid ks_live_* Bearer не 401/403 через настоящий backend.

    `client` fixture authenticated via real CompositeAuthBackend (Bearer ks_live_*).
    """
    resp = await client.get("/v1/api/auth/api-keys")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_revoked_api_key_returns_401(client, api_key_user):
    """T-3-26: revoked api_key (revoked_at IS NOT NULL) → ApiKeyService.verify None → 401."""
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
    """Phase 2 F-02 + Wave 1: /v1/admin/api-keys больше не в app.routes."""
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/v1/admin/api-keys" not in paths


def test_whitelist_is_superset_of_backend_public_prefixes():
    """T-3-25: backend PUBLIC_PREFIXES должен быть subset WHITELIST.

    WHITELIST включает FastAPI internals (/docs, /openapi.json, /redoc), которые
    не проходят через AuthenticationMiddleware (BaseRoute, не APIRoute). PUBLIC_PREFIXES
    же — auth-side bypass. Любой drift backend constant без обновления test
    автоматически отлавливается этим тестом.
    """
    from keenyspace_server.auth.composite import PUBLIC_PREFIXES

    assert set(PUBLIC_PREFIXES).issubset(WHITELIST)
