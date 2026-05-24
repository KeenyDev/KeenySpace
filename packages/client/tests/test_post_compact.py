"""Integration tests for daemon/post_compact.assemble_context.

We stub get_instructions and run_server_driven_command at the
keenyspace.daemon.post_compact module to keep the test deterministic and
free of network/LLM dependencies. The CLI-13 + HK-12 invariant
(get_instructions called BEFORE the agent run) is asserted via a shared
call-order list.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import keenyspace.daemon.post_compact as pc_mod
import pytest
from keenyspace.agent.budgets import BudgetAbort
from keenyspace_shared.mcp_contracts import (
    Budgets,
    Instructions,
    PostCompactInjection,
)


def _make_instructions(
    *,
    tool_whitelist: list[str] | None = None,
) -> Instructions:
    return Instructions(
        prompt="You select workspace context after compaction.",
        tool_whitelist=tool_whitelist
        or ["search_workspace", "read_page", "list_pages"],
        steps=["read", "search", "assemble"],
        model=None,
        budgets=Budgets(max_steps=10, max_tokens=20_000, max_seconds=45),
    )


@pytest.fixture
def auth_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pc_mod, "read_auth", lambda: {"api_key": "ks_live_test"}
    )


@pytest.fixture
def settings_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Llm:
        provider = "anthropic"
        model = "claude-sonnet-4-6"

    class _Settings:
        server_url = "http://localhost:8000"
        llm = _Llm()

    monkeypatch.setattr(pc_mod, "get_client_settings", lambda: _Settings())


@pytest.fixture
def fake_transcript(tmp_path: Path) -> Path:
    f = tmp_path / "transcript.jsonl"
    body = '{"role":"user","content":"discuss auth refactor"}\n' * 5
    f.write_text(body, encoding="utf-8")
    return f


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    instructions: Instructions,
    agent_output: Any,
    call_order: list[str] | None = None,
) -> None:
    async def fake_get_instructions(
        server_url: str,
        api_key: str,
        *,
        workspace: str,
        command: str,
        context: dict[str, Any],
    ) -> Instructions:
        if call_order is not None:
            call_order.append("get_instructions")
        assert command == "post-compact"
        assert "transcript_excerpt" in context
        return instructions

    async def fake_run(
        *,
        server_url: str,
        api_key: str,
        instructions: Instructions,
        user_prompt: str,
        llm_model: str,
        output_type: type | None = None,
        _agent_factory: Any | None = None,
    ) -> Any:
        if call_order is not None:
            call_order.append("run_server_driven_command")
        if isinstance(agent_output, Exception):
            raise agent_output
        return agent_output

    monkeypatch.setattr(pc_mod, "get_instructions", fake_get_instructions)
    monkeypatch.setattr(pc_mod, "run_server_driven_command", fake_run)


@pytest.mark.asyncio
async def test_assemble_context_happy_path(
    auth_loaded: None,
    settings_loaded: None,
    fake_transcript: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stubs(
        monkeypatch,
        instructions=_make_instructions(),
        agent_output=PostCompactInjection(
            base_layer="CLAUDE.md+index.md",
            selected_pages=["concepts/foo.md"],
            assembled_text="Hello workspace",
        ),
    )
    response = await pc_mod.assemble_context(
        {
            "workspace_slug": "demo",
            "transcript_path": str(fake_transcript),
        }
    )
    assert response == {
        "ok": True,
        "content": "Hello workspace",
        "error": None,
    }


@pytest.mark.asyncio
async def test_assemble_context_no_workspace_slug() -> None:
    response = await pc_mod.assemble_context(
        {"transcript_path": "/tmp/anything.jsonl"}
    )
    assert response["ok"] is False
    assert response["error"] == "no_workspace_slug"
    assert response["content"] is None


@pytest.mark.asyncio
async def test_assemble_context_no_transcript_path() -> None:
    response = await pc_mod.assemble_context({"workspace_slug": "demo"})
    assert response["ok"] is False
    assert response["error"] == "no_transcript_path"


@pytest.mark.asyncio
async def test_assemble_context_missing_transcript_file(
    auth_loaded: None,
    settings_loaded: None,
    tmp_path: Path,
) -> None:
    response = await pc_mod.assemble_context(
        {
            "workspace_slug": "demo",
            "transcript_path": str(tmp_path / "nope.jsonl"),
        }
    )
    assert response["ok"] is False
    assert response["error"] == "transcript_unavailable"


@pytest.mark.asyncio
async def test_assemble_context_no_auth(
    fake_transcript: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pc_mod, "read_auth", lambda: {})
    response = await pc_mod.assemble_context(
        {
            "workspace_slug": "demo",
            "transcript_path": str(fake_transcript),
        }
    )
    assert response["ok"] is False
    assert response["error"] == "no_auth"


@pytest.mark.asyncio
async def test_assemble_context_append_log_in_whitelist_refused(
    auth_loaded: None,
    settings_loaded: None,
    fake_transcript: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stubs(
        monkeypatch,
        instructions=_make_instructions(
            tool_whitelist=["search_workspace", "read_page", "append_log"]
        ),
        agent_output=PostCompactInjection(
            base_layer="", selected_pages=[], assembled_text="(unused)"
        ),
    )
    response = await pc_mod.assemble_context(
        {
            "workspace_slug": "demo",
            "transcript_path": str(fake_transcript),
        }
    )
    assert response["ok"] is False
    assert response["error"] == "append_log_in_whitelist"


@pytest.mark.asyncio
async def test_assemble_context_budget_abort(
    auth_loaded: None,
    settings_loaded: None,
    fake_transcript: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stubs(
        monkeypatch,
        instructions=_make_instructions(),
        agent_output=BudgetAbort("usage_limit_exceeded"),
    )
    response = await pc_mod.assemble_context(
        {
            "workspace_slug": "demo",
            "transcript_path": str(fake_transcript),
        }
    )
    assert response["ok"] is False
    assert response["error"].startswith("budget_abort:")
    assert "usage_limit_exceeded" in response["error"]


@pytest.mark.asyncio
async def test_assemble_context_unexpected_error_swallowed(
    auth_loaded: None,
    settings_loaded: None,
    fake_transcript: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_stubs(
        monkeypatch,
        instructions=_make_instructions(),
        agent_output=RuntimeError("mcp server exploded"),
    )
    response = await pc_mod.assemble_context(
        {
            "workspace_slug": "demo",
            "transcript_path": str(fake_transcript),
        }
    )
    assert response["ok"] is False
    assert response["error"] == "unexpected_error"


@pytest.mark.asyncio
async def test_server_driven_post_compact_calls_get_instructions_first(
    auth_loaded: None,
    settings_loaded: None,
    fake_transcript: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI-13 + HK-12 invariant: get_instructions BEFORE agent run."""
    order: list[str] = []
    _install_stubs(
        monkeypatch,
        instructions=_make_instructions(),
        agent_output=PostCompactInjection(
            base_layer="b", selected_pages=[], assembled_text="ok"
        ),
        call_order=order,
    )
    await pc_mod.assemble_context(
        {
            "workspace_slug": "demo",
            "transcript_path": str(fake_transcript),
        }
    )
    assert order == ["get_instructions", "run_server_driven_command"]


@pytest.mark.asyncio
async def test_test_hatch_bypasses_agent(
    auth_loaded: None,
    settings_loaded: None,
    fake_transcript: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KEENYSPACE_TEST_AGENT_RESPONSE returns content verbatim, skipping the agent."""
    monkeypatch.setenv("KEENYSPACE_TEST_AGENT_RESPONSE", "hatch-injected text")

    async def must_not_be_called(*a: Any, **kw: Any) -> Any:
        raise AssertionError("get_instructions should not be called in test hatch path")

    monkeypatch.setattr(pc_mod, "get_instructions", must_not_be_called)
    response = await pc_mod.assemble_context(
        {
            "workspace_slug": "demo",
            "transcript_path": str(fake_transcript),
        }
    )
    assert response == {
        "ok": True,
        "content": "hatch-injected text",
        "error": None,
    }


@pytest.mark.asyncio
async def test_envelope_transcript_path_can_come_from_payload(
    auth_loaded: None,
    settings_loaded: None,
    fake_transcript: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hook handlers nest the Claude Code envelope under 'payload'; daemon
    accepts transcript_path at either level."""
    _install_stubs(
        monkeypatch,
        instructions=_make_instructions(),
        agent_output=PostCompactInjection(
            base_layer="", selected_pages=[], assembled_text="from payload"
        ),
    )
    response = await pc_mod.assemble_context(
        {
            "workspace_slug": "demo",
            "payload": {"transcript_path": str(fake_transcript)},
        }
    )
    assert response["ok"] is True
    assert response["content"] == "from payload"


@pytest.mark.asyncio
async def test_response_shape_serialisable(
    auth_loaded: None,
    settings_loaded: None,
    fake_transcript: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-09 JSONL response shape must survive json.dumps round-trip."""
    _install_stubs(
        monkeypatch,
        instructions=_make_instructions(),
        agent_output=PostCompactInjection(
            base_layer="b", selected_pages=["p.md"], assembled_text="hi"
        ),
    )
    response = await pc_mod.assemble_context(
        {
            "workspace_slug": "demo",
            "transcript_path": str(fake_transcript),
        }
    )
    encoded = json.dumps(response)
    decoded = json.loads(encoded)
    assert set(decoded.keys()) == {"ok", "content", "error"}
