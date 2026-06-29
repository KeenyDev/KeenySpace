from __future__ import annotations

from pathlib import Path

import pytest
from keenyspace_server.compile.agent import compile_agent, run_compile_agent
from keenyspace_server.compile.models import CompileDeps, CompilePlan, PageOp
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel


def _model_returns(plan: CompilePlan):  # type: ignore[no-untyped-def]
    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=output_tool, args=plan.model_dump())])
    return _fake


@pytest.mark.asyncio
async def test_output_validator_rejects_denylist_paths(tmp_path: Path) -> None:
    bad_plan = CompilePlan(ops=[PageOp(action="create", path=".keenyspace/secret.md", body="x")])
    good_plan = CompilePlan(ops=[PageOp(action="create", path="notes/ok.md", body="y")])

    call_count = {"n": 0}

    async def _alternating(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        call_count["n"] += 1
        output_tool = info.output_tools[0].name if info.output_tools else "final_result"
        plan_to_return = bad_plan if call_count["n"] == 1 else good_plan
        return ModelResponse(parts=[ToolCallPart(tool_name=output_tool, args=plan_to_return.model_dump())])

    deps = CompileDeps(ws_root=tmp_path, wal_text="<wal_entry id='X'>data</wal_entry>")
    with compile_agent.override(model=FunctionModel(_alternating)):
        plan, _, _ = await run_compile_agent(deps)
    assert plan.ops[0].path == "notes/ok.md"
    assert call_count["n"] >= 2


@pytest.mark.asyncio
async def test_output_validator_passes_clean_plan(tmp_path: Path) -> None:
    clean = CompilePlan(ops=[PageOp(action="create", path="notes/ok.md", body="y")])
    deps = CompileDeps(ws_root=tmp_path, wal_text="<wal_entry id='X'>data</wal_entry>")
    with compile_agent.override(model=FunctionModel(_model_returns(clean))):
        plan, _, _ = await run_compile_agent(deps)
    assert plan.ops[0].path == "notes/ok.md"


@pytest.mark.parametrize("denied_path", [
    ".keenyspace/config.yaml",
    "logs/2026-05-10.md",
    "_templates/concept.md",
    "raw/asset.md",
    "CLAUDE.md",
])
def test_validator_constants_match_path_safety_denylist(denied_path: str) -> None:
    """Defense-in-depth invariant: agent.py's denylist constants and
    fs/path_safety.py::is_compile_writable must reject the same set of paths."""
    from keenyspace_server.compile.agent import (
        _COMPILE_DENYLIST_EXACT,
        _COMPILE_DENYLIST_PREFIXES,
    )
    from keenyspace_server.fs.path_safety import is_compile_writable
    assert is_compile_writable(Path("/tmp"), denied_path) is False
    rejected = (
        any(denied_path.startswith(p) for p in _COMPILE_DENYLIST_PREFIXES)
        or denied_path in _COMPILE_DENYLIST_EXACT
    )
    assert rejected is True, f"agent.py denylist constants miss {denied_path!r}"
