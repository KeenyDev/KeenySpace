"""D-05 group entry gate in CompositeAuthBackend.authenticate.

The gate applies to every principal: OIDC users by their token's groups claim,
API keys by their owner's group snapshot.
  1. OIDC user in required group -> admitted.
  2. OIDC user not in required group -> AuthenticationError.
  3. api_key whose owner snapshot holds the group -> admitted.
  4. api_key whose owner snapshot lacks the group -> AuthenticationError.
  5. api_key whose owner has no snapshot -> AuthenticationError.
  6. Empty required_group disables the gate.
  7. Error message does not leak the group name (ASVS V7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import structlog.testing
from keenyspace_server.auth.composite import CompositeAuthBackend
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
async def test_oidc_user_in_required_group_passes() -> None:
    user = User(sub="u", _display_name="u", source="oidc", groups=["keenyspace-users"])
    fake_keys = AsyncMock()
    backend = CompositeAuthBackend(
        oidc_client=None,
        api_key_service=fake_keys,
        required_group="keenyspace-users",
    )
    backend._try_oidc_bearer = AsyncMock(return_value=user)  # type: ignore[method-assign]
    backend._try_api_key = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await backend.authenticate(_conn("/v1/api/workspaces/"))
    assert result is not None
    creds, returned_user = result
    assert "authenticated" in creds.scopes
    assert returned_user.sub == "u"


@pytest.mark.asyncio
async def test_oidc_user_not_in_required_group_raises() -> None:
    user = User(sub="u", _display_name="u", source="oidc", groups=[])
    fake_keys = AsyncMock()
    backend = CompositeAuthBackend(
        oidc_client=None,
        api_key_service=fake_keys,
        required_group="keenyspace-users",
    )
    backend._try_oidc_bearer = AsyncMock(return_value=user)  # type: ignore[method-assign]
    backend._try_api_key = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with pytest.raises(AuthenticationError):
        await backend.authenticate(_conn("/v1/api/workspaces/"))


def _key_backend(user: User) -> CompositeAuthBackend:
    backend = CompositeAuthBackend(
        oidc_client=None,
        api_key_service=AsyncMock(),
        required_group="keenyspace-users",
    )
    backend._try_api_key = AsyncMock(return_value=user)  # type: ignore[method-assign]
    return backend


@pytest.mark.asyncio
async def test_api_key_with_group_in_owner_snapshot_passes() -> None:
    user = User(
        sub="u",
        _display_name="u",
        source="api_key",
        groups=["keenyspace-users"],
        groups_seen_at=datetime.now(UTC),
    )

    result = await _key_backend(user).authenticate(_conn("/v1/api/workspaces/"))

    assert result is not None
    assert "authenticated" in result[0].scopes


@pytest.mark.asyncio
async def test_api_key_whose_owner_left_the_group_is_denied() -> None:
    user = User(
        sub="u", _display_name="u", source="api_key", groups=[], groups_seen_at=datetime.now(UTC)
    )

    with structlog.testing.capture_logs() as logs, pytest.raises(AuthenticationError):
        await _key_backend(user).authenticate(_conn("/v1/api/workspaces/"))

    assert {"event": "auth.group_gate.denied", "reason": "not_in_group"}.items() <= logs[-1].items()


@pytest.mark.asyncio
async def test_api_key_without_owner_snapshot_is_denied() -> None:
    user = User(sub="u", _display_name="u", source="api_key")

    with structlog.testing.capture_logs() as logs, pytest.raises(AuthenticationError):
        await _key_backend(user).authenticate(_conn("/v1/api/workspaces/"))

    assert {
        "event": "auth.group_gate.denied",
        "reason": "no_group_snapshot",
    }.items() <= logs[-1].items()


@pytest.mark.asyncio
async def test_group_gate_disabled_when_empty_string() -> None:
    user = User(sub="u", _display_name="u", source="oidc", groups=[])
    fake_keys = AsyncMock()
    backend = CompositeAuthBackend(
        oidc_client=None,
        api_key_service=fake_keys,
        required_group="",
    )
    backend._try_oidc_bearer = AsyncMock(return_value=user)  # type: ignore[method-assign]
    backend._try_api_key = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await backend.authenticate(_conn("/v1/api/workspaces/"))
    assert result is not None


@pytest.mark.asyncio
async def test_group_gate_error_message_does_not_leak_group_name() -> None:
    user = User(sub="u", _display_name="u", source="oidc", groups=[])
    fake_keys = AsyncMock()
    backend = CompositeAuthBackend(
        oidc_client=None,
        api_key_service=fake_keys,
        required_group="keenyspace-users",
    )
    backend._try_oidc_bearer = AsyncMock(return_value=user)  # type: ignore[method-assign]
    backend._try_api_key = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with pytest.raises(AuthenticationError) as exc_info:
        await backend.authenticate(_conn("/v1/api/workspaces/"))

    assert str(exc_info.value) == "forbidden"
    assert "keenyspace-users" not in str(exc_info.value)
