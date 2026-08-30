from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from keenyspace_server.mcp import auth_bridge


def _pin(monkeypatch: pytest.MonkeyPatch, query: dict[str, str]) -> None:
    monkeypatch.setattr(
        auth_bridge,
        "get_http_request",
        lambda: SimpleNamespace(query_params=query),
    )


def test_explicit_argument_wins_over_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin(monkeypatch, {"workspace": "pinned"})

    assert auth_bridge.resolve_workspace("explicit") == "explicit"


def test_falls_back_to_url_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin(monkeypatch, {"workspace": "pinned"})

    assert auth_bridge.resolve_workspace(None) == "pinned"


def test_empty_argument_falls_back_to_url_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin(monkeypatch, {"workspace": "pinned"})

    assert auth_bridge.resolve_workspace("") == "pinned"


def test_no_argument_and_no_pin_raises_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin(monkeypatch, {})

    with pytest.raises(ToolError, match="no workspace specified"):
        auth_bridge.resolve_workspace(None)
