from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from keenyspace_server.compile.agent import compile_agent, run_compile_agent
from keenyspace_server.compile.hashing import hash_plan
from keenyspace_server.compile.models import CompileDeps, CompilePlan, PageOp
from keenyspace_server.compile.page_writer import apply_plan
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

pytestmark = pytest.mark.eval


async def test_idempotency_dimension_1_same_wal_yields_same_plan_hash(tmp_path: Path) -> None:
    fixed_plan = CompilePlan(
        ops=[
            PageOp(
                action="create",
                path="notes/test.md",
                body="hello world",
                frontmatter={"title": "Test"},
            )
        ],
        notes="",
    )

    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool_name = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(
            parts=[ToolCallPart(tool_name=output_tool_name, args=fixed_plan.model_dump())]
        )

    wal_text = "<wal_entry id='01HX0000000000000000000001'>hello</wal_entry>"
    deps = CompileDeps(ws_root=tmp_path, wal_text=wal_text)

    with compile_agent.override(model=FunctionModel(_fake)):
        plan_1, _ = await run_compile_agent(deps)
        plan_2, _ = await run_compile_agent(deps)

    h1 = hash_plan("01HX0000000000000000000001", "01HX0000000000000000000001", plan_1)
    h2 = hash_plan("01HX0000000000000000000001", "01HX0000000000000000000001", plan_2)
    assert h1 == h2


async def test_idempotency_dimension_1_no_disk_churn_on_second_apply(tmp_path: Path) -> None:
    plan = CompilePlan(
        ops=[
            PageOp(
                action="create",
                path="notes/idempot.md",
                body="content that should not change",
                frontmatter={"title": "Idempotency"},
            )
        ],
        notes="",
    )

    apply_plan(tmp_path, plan)
    target = tmp_path / "notes" / "idempot.md"
    assert target.exists()
    mtime_1 = target.stat().st_mtime_ns

    await asyncio.sleep(0.01)
    mtime_2 = target.stat().st_mtime_ns
    assert mtime_1 == mtime_2, "File mtime changed without a second write — unexpected disk churn"

    plan_again = CompilePlan(
        ops=[
            PageOp(
                action="create",
                path="notes/idempot.md",
                body="content that should not change",
                frontmatter={"title": "Idempotency"},
            )
        ],
        notes="",
    )
    assert hash_plan("A", "B", plan) == hash_plan("A", "B", plan_again)
