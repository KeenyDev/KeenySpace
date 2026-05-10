from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from keenyspace_server.compile.agent import compile_agent, run_compile_agent
from keenyspace_server.compile.loop_detector import LoopDetector
from keenyspace_server.compile.models import CompileDeps


def test_loop_detector_default_max_repeats() -> None:
    d = LoopDetector()
    assert d.max_repeats == 3
    assert d.triggered is False


def _looping_model_factory():
    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[
            ToolCallPart(tool_name="read_page", args={"path": "notes/index.md"}),
        ])
    return _fake


@pytest.mark.asyncio
async def test_loop_detector_aborts_on_repeated_tool_call(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "index.md").write_text("# Index\n")
    deps = CompileDeps(ws_root=tmp_path, wal_text="<wal_entry id='X'>data</wal_entry>")
    detector = LoopDetector(max_repeats=3)
    with compile_agent.override(model=FunctionModel(_looping_model_factory())):
        with pytest.raises((UsageLimitExceeded, Exception)):  # noqa: PT011
            await run_compile_agent(deps, max_tool_calls=10, loop_detector=detector)
    assert detector.triggered is True


@pytest.mark.asyncio
async def test_loop_detector_per_run_no_state_bleed(tmp_path: Path) -> None:
    from keenyspace_server.compile.models import CompilePlan, PageOp

    target_plan = CompilePlan(ops=[PageOp(action="create", path="x.md", body="b")])

    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=output_tool, args=target_plan.model_dump())])

    deps = CompileDeps(ws_root=tmp_path, wal_text="<wal_entry id='X'>x</wal_entry>")

    with compile_agent.override(model=FunctionModel(_fake)):
        plan_1, detector_1 = await run_compile_agent(deps)
        plan_2, detector_2 = await run_compile_agent(deps)

    assert detector_1 is not detector_2
    assert detector_1.triggered is False
    assert detector_2.triggered is False
    assert plan_1.ops == plan_2.ops
