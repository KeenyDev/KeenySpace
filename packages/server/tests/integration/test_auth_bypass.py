"""
AUTH-08 regression: every non-public prefix must return 401 for anonymous requests.
Parameterized over FastAPI router introspection + explicit MCP and admin paths.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

PUBLIC_PREFIXES = ("/healthz", "/readyz", "/metrics", "/docs", "/openapi.json", "/redoc")


def _collect_routes(app) -> list[tuple[str, str]]:
    result = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            path = route.path
            skip = any(path.startswith(p) for p in PUBLIC_PREFIXES)
            if skip:
                continue
            methods = route.methods or {"GET"}
            for method in methods:
                result.append((method, path))
    result.append(("POST", "/v1/mcp/"))
    result.append(("GET", "/v1/admin/api-keys"))
    return result


@pytest.fixture(scope="module")
def test_app():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("KEENYSPACE_DB__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("KEENYSPACE_FS__ROOT", "/tmp/k")
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

    import keenyspace_server.config as cfg_module
    cfg_module.get_settings.cache_clear()

    from keenyspace_server.main import build_app
    application = build_app()
    yield application
    monkeypatch.undo()
    cfg_module.get_settings.cache_clear()


def _get_routes(test_app):
    return _collect_routes(test_app)


@pytest.mark.asyncio
async def test_anonymous_gets_401_on_all_routes(test_app):
    routes = _get_routes(test_app)
    assert len(routes) > 0, "No routes found to test"

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for method, path in routes:
            template_path = path.replace("{slug}", "test-ws").replace("{path:path}", "index")
            resp = await client.request(method, template_path)
            assert resp.status_code == 401, (
                f"{method} {path} -> expected 401 (anonymous), got {resp.status_code}"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_header,expected_status",
    [
        (None, 401),
        ("", 401),
        ("Bearer ", 401),
        ("Bearer dev-", 401),
        ("Bearer dev-wrong", 401),
        ("Bearer wrong-format", 401),
        ("bearer dev-test", 401),
        ("Bearer dev-test", 201),
    ],
)
async def test_bearer_edge_cases(test_app, auth_header: str | None, expected_status: int):
    headers = {}
    if auth_header is not None:
        headers["Authorization"] = auth_header

    transport_no_raise = ASGITransport(app=test_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport_no_raise, base_url="http://test", headers=headers) as client:
        resp = await client.post(
            "/v1/api/workspaces/",
            json={"slug": "test-auth-edge", "blueprint": "default"},
        )
        if expected_status == 201:
            assert resp.status_code not in (401, 403), (
                f"valid auth should not return 401/403, got {resp.status_code}"
            )
        else:
            assert resp.status_code == 401, (
                f"auth={auth_header!r} should return 401, got {resp.status_code}"
            )
