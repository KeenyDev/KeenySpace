"""AUTH-04 CompositeAuthBackend unit tests.

Покрытие:
- Resolution order: cookie > api_key > oidc_bearer (Wave 2: cookie+oidc_bearer stubs)
- API-key path: ks_live_* → User(source='api_key')
- Anonymous → AuthenticationError
- PUBLIC_PREFIXES bypass: /healthz, /readyz, /metrics, /v1/api/auth/login, /v1/api/auth/callback
- Invalid api-key (verify returns None) → AuthenticationError (no silent fallthrough)
- Non-ks_live Bearer → не дёргает ApiKeyService.verify, проваливается на 401
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from keenyspace_server.auth.composite import PUBLIC_PREFIXES, CompositeAuthBackend
from keenyspace_server.auth.user import User
from starlette.authentication import AuthenticationError
from starlette.requests import HTTPConnection


def _conn(path: str, headers: dict[str, str] | None = None) -> HTTPConnection:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw_headers,
        "scheme": "http",
        "server": ("test", 80),
    }
    return HTTPConnection(scope)


@pytest.mark.asyncio
async def test_public_prefixes_bypass() -> None:
    fake_keys = AsyncMock()
    backend = CompositeAuthBackend(oidc_client=None, api_key_service=fake_keys)
    for prefix in PUBLIC_PREFIXES:
        result = await backend.authenticate(_conn(prefix + "/anything"))
        assert result is None
    fake_keys.verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_key_path_returns_authenticated_user() -> None:
    fake_user = User(sub="u", _display_name="u", source="api_key")
    fake_keys = AsyncMock()
    fake_keys.verify.return_value = fake_user
    backend = CompositeAuthBackend(oidc_client=None, api_key_service=fake_keys)
    conn = _conn(
        "/v1/api/workspaces/",
        headers={"Authorization": "Bearer ks_live_xxx"},
    )
    result = await backend.authenticate(conn)
    assert result is not None
    creds, user = result
    assert "authenticated" in creds.scopes
    assert user.sub == "u"
    assert user.source == "api_key"
    fake_keys.verify.assert_awaited_once_with("ks_live_xxx")


@pytest.mark.asyncio
async def test_resolution_order_api_key_short_circuits_oidc_bearer() -> None:
    """api_key resolver hits before oidc_bearer; Wave 2 stub cookie returns None."""
    fake_user = User(sub="u", _display_name="u", source="api_key")
    fake_keys = AsyncMock()
    fake_keys.verify.return_value = fake_user
    backend = CompositeAuthBackend(oidc_client=None, api_key_service=fake_keys)
    conn = _conn(
        "/v1/api/workspaces/",
        headers={"Authorization": "Bearer ks_live_x", "Cookie": "ks_at=irrelevant-wave2"},
    )
    result = await backend.authenticate(conn)
    assert result is not None


@pytest.mark.asyncio
async def test_invalid_api_key_falls_through_to_401() -> None:
    """T-3-23: ApiKeyService.verify returning None → AuthError, не silent anonymous."""
    fake_keys = AsyncMock()
    fake_keys.verify.return_value = None
    backend = CompositeAuthBackend(oidc_client=None, api_key_service=fake_keys)
    conn = _conn(
        "/v1/api/workspaces/",
        headers={"Authorization": "Bearer ks_live_invalid"},
    )
    with pytest.raises(AuthenticationError):
        await backend.authenticate(conn)


@pytest.mark.asyncio
async def test_non_ks_live_bearer_does_not_invoke_api_key_verify() -> None:
    """Wave 2: non-ks_live bearer → _try_api_key возвращает None без DB hit;
    _try_oidc_bearer (stub) тоже None; результат — 401.
    """
    fake_keys = AsyncMock()
    backend = CompositeAuthBackend(oidc_client=None, api_key_service=fake_keys)
    conn = _conn(
        "/v1/api/workspaces/",
        headers={"Authorization": "Bearer eyJxxx.zzz"},
    )
    with pytest.raises(AuthenticationError):
        await backend.authenticate(conn)
    fake_keys.verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_authorization_anonymous_raises() -> None:
    fake_keys = AsyncMock()
    backend = CompositeAuthBackend(oidc_client=None, api_key_service=fake_keys)
    with pytest.raises(AuthenticationError):
        await backend.authenticate(_conn("/v1/api/workspaces/"))


@pytest.mark.asyncio
async def test_malformed_authorization_header_raises() -> None:
    fake_keys = AsyncMock()
    backend = CompositeAuthBackend(oidc_client=None, api_key_service=fake_keys)
    conn = _conn("/v1/api/workspaces/", headers={"Authorization": "malformed-no-bearer"})
    with pytest.raises(AuthenticationError):
        await backend.authenticate(conn)


def test_public_prefixes_constant_includes_required() -> None:
    """T-3-25: prevent whitelist drift."""
    required = {
        "/healthz",
        "/readyz",
        "/metrics",
        "/v1/api/auth/login",
        "/v1/api/auth/callback",
    }
    assert required.issubset(set(PUBLIC_PREFIXES))


def test_public_prefixes_is_tuple_for_immutability() -> None:
    """T-3-25: tuple, not list — prevents accidental .append() drift."""
    assert isinstance(PUBLIC_PREFIXES, tuple)
