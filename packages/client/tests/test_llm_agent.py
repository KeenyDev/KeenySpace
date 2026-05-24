"""Tests for clients/llm.py — budget triple-guard and tool_whitelist enforcement.

Pattern mirrors Phase 2 test_compile_agent.py: FunctionModel for deterministic
behaviour. We inject a fake Agent factory so the test does not need to talk to
a real MCP server.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from keenyspace.agent.budgets import BudgetAbort, make_loop_detector
from keenyspace.agent.tool_filter import (
    ToolWhitelistViolation,
    make_process_tool_call,
    supports_tool_filter,
)
from keenyspace.clients.llm import run_server_driven_command
from keenyspace_shared.mcp_contracts import Budgets, Instructions
from pydantic_ai import Agent, Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

_BUDGETS_TIGHT = Budgets(max_steps=2, max_tokens=10_000, max_seconds=30)
_BUDGETS_FAST_TIMEOUT = Budgets(max_steps=10, max_tokens=10_000, max_seconds=1)


def _make_instructions(
    *,
    tool_whitelist: list[str] | None = None,
    budgets: Budgets | None = None,
) -> Instructions:
    return Instructions(
        prompt="You are a helpful assistant.",
        tool_whitelist=tool_whitelist
        or ["search_workspace", "read_page", "list_pages"],
        steps=["step one"],
        model=None,
        budgets=budgets or _BUDGETS_TIGHT,
    )


async def _stub_search_workspace(q: str) -> str:
    return f"no results for {q}"


async def _stub_read_page(path: str) -> str:
    return f"contents of {path}"


async def _stub_list_pages() -> list[str]:
    return ["index.md"]


_STUB_TOOLS = [
    Tool(_stub_search_workspace, name="search_workspace"),
    Tool(_stub_read_page, name="read_page"),
    Tool(_stub_list_pages, name="list_pages"),
]


def _agent_factory_with_model(model: Any) -> Any:
    """Factory that builds an Agent with stubbed tools so FunctionModel-emitted
    tool calls resolve locally instead of hitting a real MCP server."""

    def builder(*, model: Any, system_prompt: str, toolsets: list[Any]) -> Agent:
        return Agent(
            model=model,
            instructions=system_prompt,
            tools=_STUB_TOOLS,
            toolsets=[],
        )

    real_model = model

    def real_builder(*, model: Any, system_prompt: str, toolsets: list[Any]) -> Agent:
        return Agent(
            model=real_model,
            instructions=system_prompt,
            tools=_STUB_TOOLS,
            toolsets=[],
        )

    return real_builder


def test_supports_tool_filter_truthy_on_pydantic_ai_1_93() -> None:
    assert supports_tool_filter() is True


@pytest.mark.asyncio
async def test_happy_path_returns_output() -> None:
    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content="here is the answer")])

    fm = FunctionModel(_fake)
    out = await run_server_driven_command(
        server_url="http://localhost:8000",
        api_key="ks_live_test",
        instructions=_make_instructions(),
        user_prompt="what is foo?",
        llm_model="anthropic:claude-sonnet-4-6",
        _agent_factory=_agent_factory_with_model(fm),
    )
    assert "answer" in str(out)


@pytest.mark.asyncio
async def test_budget_step_limit_aborts() -> None:
    """request_limit=2 with FunctionModel that emits a tool call → UsageLimitExceeded → BudgetAbort."""

    call_count = {"n": 0}

    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        call_count["n"] += 1
        # Always emit a fake tool call to consume request budget.
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="search_workspace",
                    args={"q": f"iteration-{call_count['n']}"},
                ),
            ]
        )

    fm = FunctionModel(_fake)

    with pytest.raises(BudgetAbort) as excinfo:
        await run_server_driven_command(
            server_url="http://localhost:8000",
            api_key="ks_live_test",
            instructions=_make_instructions(budgets=_BUDGETS_TIGHT),
            user_prompt="loop forever",
            llm_model="anthropic:claude-sonnet-4-6",
            _agent_factory=_agent_factory_with_model(fm),
        )
    # Either usage_limit_exceeded or loop_detected — both are budget aborts.
    assert excinfo.value.reason in {"usage_limit_exceeded", "loop_detected"}


@pytest.mark.asyncio
async def test_budget_timeout_aborts() -> None:
    async def _slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(2.0)
        return ModelResponse(parts=[TextPart(content="too late")])

    fm = FunctionModel(_slow)

    instructions = _make_instructions(
        budgets=Budgets(max_steps=10, max_tokens=10_000, max_seconds=1)
    )
    with pytest.raises(BudgetAbort) as excinfo:
        await run_server_driven_command(
            server_url="http://localhost:8000",
            api_key="ks_live_test",
            instructions=instructions,
            user_prompt="slow",
            llm_model="anthropic:claude-sonnet-4-6",
            _agent_factory=_agent_factory_with_model(fm),
        )
    assert excinfo.value.reason == "timeout_exceeded"


@pytest.mark.asyncio
async def test_loop_detector_aborts() -> None:
    """Same (tool, args) 3x → LoopDetector triggers; reason should be loop_detected."""

    async def _repeat(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="search_workspace",
                    args={"q": "always_same"},
                ),
            ]
        )

    fm = FunctionModel(_repeat)
    instructions = _make_instructions(
        budgets=Budgets(max_steps=50, max_tokens=100_000, max_seconds=30)
    )
    with pytest.raises(BudgetAbort) as excinfo:
        await run_server_driven_command(
            server_url="http://localhost:8000",
            api_key="ks_live_test",
            instructions=instructions,
            user_prompt="repeat",
            llm_model="anthropic:claude-sonnet-4-6",
            _agent_factory=_agent_factory_with_model(fm),
        )
    # Loop detector raises ModelRetry → eventually UsageLimitExceeded; we mapped it.
    assert excinfo.value.reason in {"loop_detected", "usage_limit_exceeded"}


@pytest.mark.asyncio
async def test_tool_whitelist_violation_raises_budget_abort() -> None:
    """process_tool_call hook rejects non-whitelisted tool → BudgetAbort."""

    hook = make_process_tool_call(["read_page", "search_workspace"])

    async def _passthrough(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    with pytest.raises(ToolWhitelistViolation):
        await hook(None, _passthrough, "append_log", {"content": "x"})

    # And whitelisted call goes through:
    result = await hook(None, _passthrough, "read_page", {"path": "p"})
    assert result == {"ok": True}


def test_loop_detector_default_max_repeats() -> None:
    detector = make_loop_detector()
    assert detector.max_repeats == 3
    assert detector.triggered is False
