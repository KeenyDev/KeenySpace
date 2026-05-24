"""Tests for clients/mcp.py — defensive coercion of fastmcp CallToolResult."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from keenyspace.clients.mcp import _coerce, get_instructions


class _FakeContent:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.last_call: tuple[str, dict[str, object]] | None = None

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call_tool(self, name: str, args: dict[str, object]) -> object:
        self.last_call = (name, args)
        return SimpleNamespace(
            structured_content=self._payload,
            data=None,
            content=[],
        )


def test_coerce_prefers_structured_content() -> None:
    result = SimpleNamespace(
        structured_content={"a": 1},
        data={"b": 2},
        content=[_FakeContent('{"c": 3}')],
    )
    assert _coerce(result) == {"a": 1}


def test_coerce_falls_back_to_data() -> None:
    result = SimpleNamespace(
        structured_content=None,
        data={"b": 2},
        content=[_FakeContent('{"c": 3}')],
    )
    assert _coerce(result) == {"b": 2}


def test_coerce_falls_back_to_content_text_json() -> None:
    result = SimpleNamespace(
        structured_content=None,
        data=None,
        content=[_FakeContent('{"c": 3}')],
    )
    assert _coerce(result) == {"c": 3}


def test_coerce_handles_plain_text_content() -> None:
    result = SimpleNamespace(
        structured_content=None,
        data=None,
        content=[_FakeContent("hello")],
    )
    out = _coerce(result)
    assert out == {"raw": "hello"}


@pytest.mark.asyncio
async def test_get_instructions_parses_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(
        {
            "prompt": "be helpful",
            "tool_whitelist": ["read_page", "search_workspace"],
            "steps": ["step 1"],
            "model": None,
            "budgets": {"max_steps": 5, "max_tokens": 1000, "max_seconds": 30},
        }
    )

    def _builder(server_url: str, api_key: str) -> _FakeClient:
        return fake

    monkeypatch.setattr("keenyspace.clients.mcp.build_mcp_client", _builder)
    instr = await get_instructions(
        "http://localhost:8000",
        "ks_live_test",
        workspace="demo",
        command="query",
        context={"question": "what is foo"},
    )
    assert instr.prompt == "be helpful"
    assert instr.budgets.max_steps == 5
    assert "search_workspace" in instr.tool_whitelist
    assert fake.last_call == (
        "get_instructions",
        {
            "workspace": "demo",
            "command": "query",
            "context": {"question": "what is foo"},
        },
    )


@pytest.mark.asyncio
async def test_call_compile_returns_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keenyspace.clients.mcp import call_compile

    fake = _FakeClient({"job_id": "j1", "status": "queued"})

    def _builder(server_url: str, api_key: str) -> _FakeClient:
        return fake

    monkeypatch.setattr("keenyspace.clients.mcp.build_mcp_client", _builder)
    out = await call_compile(
        "http://localhost:8000", "ks_live_test", workspace="demo"
    )
    assert out == {"job_id": "j1", "status": "queued"}
    assert fake.last_call == ("compile", {"workspace": "demo"})


@pytest.mark.asyncio
async def test_call_compile_status_returns_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keenyspace.clients.mcp import call_compile_status

    fake = _FakeClient({"state": "running", "last_compile_at": "2026-05-24T00:00:00Z"})

    def _builder(server_url: str, api_key: str) -> _FakeClient:
        return fake

    monkeypatch.setattr("keenyspace.clients.mcp.build_mcp_client", _builder)
    out = await call_compile_status(
        "http://localhost:8000", "ks_live_test", workspace="demo"
    )
    assert out["state"] == "running"
    assert fake.last_call == ("compile_status", {"workspace": "demo"})


def test_coerce_handles_json_round_trip() -> None:
    payload = {"prompt": "x", "tool_whitelist": []}
    result = SimpleNamespace(
        structured_content=None,
        data=None,
        content=[_FakeContent(json.dumps(payload))],
    )
    assert _coerce(result) == payload
