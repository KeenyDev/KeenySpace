from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from keenyspace_server.compile.agent import compile_agent, run_compile_agent
from keenyspace_server.compile.models import CompileDeps, CompilePlan, PageOp
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

FIXTURES = Path(__file__).parent / "fixtures" / "compile" / "golden"

pytestmark = pytest.mark.eval


def _load_fixture(fixture_dir: Path) -> tuple[str, dict[str, Any], Path]:
    wal_text = (fixture_dir / "wal.md").read_text(encoding="utf-8")
    expect: dict[str, Any] = json.loads((fixture_dir / "expect.json").read_text(encoding="utf-8"))
    vault_path = fixture_dir / "vault"
    return wal_text, expect, vault_path


def _synth_plan_from_expect(expect: dict[str, Any]) -> CompilePlan:
    body_fragments: list[str] = expect.get("required_body_fragments", [])
    fm_keys: list[str] = expect.get("required_frontmatter_keys", [])
    body = "\n\n".join(body_fragments) if body_fragments else "Compiled content."
    frontmatter = dict.fromkeys(fm_keys, "value")
    ops = [
        PageOp(
            action=op["action"],
            path=op["path"],
            body=body,
            frontmatter=frontmatter,
        )
        for op in expect["expected_ops"]
    ]
    return CompilePlan(ops=ops, notes="")


_fixture_names = sorted(
    p.name
    for p in FIXTURES.iterdir()
    if p.is_dir() and (p / "expect.json").exists()
)


@pytest.mark.parametrize("fixture_name", _fixture_names)
async def test_golden_fixture_roundtrip(fixture_name: str, tmp_path: Path) -> None:
    fixture_dir = FIXTURES / fixture_name
    wal_text, expect, vault_path = _load_fixture(fixture_dir)

    synth_plan = _synth_plan_from_expect(expect)

    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool_name = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(
            parts=[ToolCallPart(tool_name=output_tool_name, args=synth_plan.model_dump())]
        )

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    if vault_path.exists() and any(vault_path.iterdir()):
        for src in vault_path.rglob("*"):
            if src.is_file() and src.name != ".gitkeep":
                dest = ws_root / src.relative_to(vault_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)

    deps = CompileDeps(ws_root=ws_root, wal_text=wal_text)
    with compile_agent.override(model=FunctionModel(_fake)):
        plan, _, _ = await run_compile_agent(deps)

    assert [(op.action, op.path) for op in plan.ops] == [
        (o["action"], o["path"]) for o in expect["expected_ops"]
    ], f"Op list mismatch for fixture {fixture_name}"

    for op in plan.ops:
        for fragment in expect.get("required_body_fragments", []):
            assert fragment in op.body, (
                f"Fragment {fragment!r} missing from op body in fixture {fixture_name}"
            )
        for key in expect.get("required_frontmatter_keys", []):
            assert key in op.frontmatter, (
                f"Frontmatter key {key!r} missing in fixture {fixture_name}"
            )
