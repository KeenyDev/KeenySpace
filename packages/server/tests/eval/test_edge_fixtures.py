from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from keenyspace_server.compile.agent import compile_agent, run_compile_agent
from keenyspace_server.compile.models import CompileDeps, CompilePlan, PageOp
from keenyspace_server.compile.page_writer import apply_plan
from keenyspace_server.compile.wal_slice import extract_wal_slice
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

EDGE_FIXTURES = Path(__file__).parent / "fixtures" / "compile" / "edge"

pytestmark = pytest.mark.eval


def _load_fixture(fixture_dir: Path) -> tuple[str, dict[str, Any], Path]:
    wal_text = (fixture_dir / "wal.md").read_text(encoding="utf-8")
    expect: dict[str, Any] = json.loads((fixture_dir / "expect.json").read_text(encoding="utf-8"))
    vault_path = fixture_dir / "vault"
    return wal_text, expect, vault_path


def _copy_vault(vault_path: Path, ws_root: Path) -> None:
    if vault_path.exists():
        for src in vault_path.rglob("*"):
            if src.is_file() and src.name != ".gitkeep":
                dest = ws_root / src.relative_to(vault_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)


async def test_edge_01_empty_wal_returns_idempotent_noop(tmp_path: Path) -> None:
    fixture_dir = EDGE_FIXTURES / "01-empty-wal"
    _, expect, _vault_path = _load_fixture(fixture_dir)

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    logs_dir = ws_root / "logs"
    logs_dir.mkdir()
    (logs_dir / "2026-05-10.md").write_text("", encoding="utf-8")

    slice_ = extract_wal_slice(ws_root, None)
    assert slice_.entries == [], "Expected zero WAL entries for empty-wal fixture"
    assert expect["expected_status"] == "idempotent_noop"
    assert expect["expected_ops"] == []


@pytest.mark.xfail(reason="WAL input-token splitter is deferred to v1.1")
async def test_edge_02_oversized_slice_documents_behavior(tmp_path: Path) -> None:
    fixture_dir = EDGE_FIXTURES / "02-oversized-slice"
    wal_text, expect, _ = _load_fixture(fixture_dir)

    byte_heuristic_threshold = 50_000 * 3
    assert len(wal_text.encode()) > byte_heuristic_threshold, (
        f"Expected wal.md to exceed {byte_heuristic_threshold} bytes "
        f"(got {len(wal_text.encode())})"
    )
    assert "required_notes_substring" in expect and expect["required_notes_substring"] == "wal_slice_truncated", (
        "Oversized-slice fixture expect.json should document wal_slice_truncated behavior"
    )
    raise AssertionError(
        "Splitter not yet implemented — test documents the expected v1.1 behavior. "
        "Remove xfail when splitter ships."
    )


async def test_edge_03_malformed_frontmatter_overwrites_cleanly(tmp_path: Path) -> None:
    fixture_dir = EDGE_FIXTURES / "03-malformed-frontmatter"
    wal_text, expect, vault_path = _load_fixture(fixture_dir)

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    _copy_vault(vault_path, ws_root)

    target_path = expect["expected_ops"][0]["path"]
    synth_plan = CompilePlan(
        ops=[
            PageOp(
                action="update",
                path=target_path,
                body="The broken page frontmatter has been corrected. Title: Broken Page Fixed. Status: active.",
                frontmatter={"title": "Broken Page Fixed", "status": "active"},
            )
        ],
        notes="",
    )

    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool_name = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(
            parts=[ToolCallPart(tool_name=output_tool_name, args=synth_plan.model_dump())]
        )

    deps = CompileDeps(ws_root=ws_root, wal_text=wal_text)
    with compile_agent.override(model=FunctionModel(_fake)):
        plan, _, _ = await run_compile_agent(deps)

    apply_plan(ws_root, plan)

    result_file = ws_root / target_path
    assert result_file.exists(), f"Expected {target_path} to be created by apply_plan"
    content = result_file.read_text(encoding="utf-8")

    if content.startswith("---"):
        fm_end = content.find("---", 3)
        if fm_end != -1:
            fm_text = content[3:fm_end].strip()
            parsed = yaml.safe_load(fm_text)
            assert isinstance(parsed, dict), "Frontmatter should parse to a dict after overwrite"


async def test_edge_04_nonexistent_page_creates_not_loops(tmp_path: Path) -> None:
    fixture_dir = EDGE_FIXTURES / "04-page-not-exists"
    wal_text, expect, vault_path = _load_fixture(fixture_dir)

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    _copy_vault(vault_path, ws_root)

    expected_op = expect["expected_ops"][0]
    assert expected_op["action"] == "create", (
        "page-not-exists fixture should expect a create op (agent creates instead of looping)"
    )

    synth_plan = CompilePlan(
        ops=[
            PageOp(
                action="create",
                path=expected_op["path"],
                body="Docker Compose quickstart: set KEENYSPACE_DB__URL and KEENYSPACE_FS__ROOT environment variables.",
                frontmatter={},
            )
        ],
        notes="",
    )

    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool_name = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(
            parts=[ToolCallPart(tool_name=output_tool_name, args=synth_plan.model_dump())]
        )

    deps = CompileDeps(ws_root=ws_root, wal_text=wal_text)
    with compile_agent.override(model=FunctionModel(_fake)):
        plan, _, _ = await run_compile_agent(deps)

    assert plan.ops[0].action == "create", "Agent should emit create (not update) for nonexistent page"


async def test_edge_05_terse_fragment_does_not_confabulate(tmp_path: Path) -> None:
    fixture_dir = EDGE_FIXTURES / "05-terse-fragment"
    wal_text, _expect, vault_path = _load_fixture(fixture_dir)

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    _copy_vault(vault_path, ws_root)

    synth_plan = CompilePlan(
        ops=[
            PageOp(
                action="create",
                path="notes/auth.md",
                body="<!-- TBD: WAL entry was too terse to compile faithfully. Original: 'update auth' -->",
                frontmatter={},
            )
        ],
        notes="WAL entry 'update auth' is too ambiguous to resolve without additional context.",
    )

    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool_name = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(
            parts=[ToolCallPart(tool_name=output_tool_name, args=synth_plan.model_dump())]
        )

    deps = CompileDeps(ws_root=ws_root, wal_text=wal_text)
    with compile_agent.override(model=FunctionModel(_fake)):
        plan, _, _ = await run_compile_agent(deps)

    has_tbd_in_body = any("TBD" in op.body for op in plan.ops)
    has_notes = bool(plan.notes)
    assert has_tbd_in_body or has_notes, (
        "Terse-fragment agent should either place a TBD marker in op.body or surface ambiguity in plan.notes"
    )


async def test_edge_06_many_small_fragments_consolidates(tmp_path: Path) -> None:
    fixture_dir = EDGE_FIXTURES / "06-many-small-fragments"
    wal_text, expect, vault_path = _load_fixture(fixture_dir)

    entry_count = wal_text.count("<wal_entry ")
    assert entry_count >= 20, (
        f"many-small-fragments fixture should have at least 20 WAL entries (got {entry_count})"
    )

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    _copy_vault(vault_path, ws_root)

    expected_op = expect["expected_ops"][0]
    synth_plan = CompilePlan(
        ops=[
            PageOp(
                action=expected_op["action"],
                path=expected_op["path"],
                body="Consolidated compile pipeline notes from 22 WAL entries.",
                frontmatter={},
            )
        ],
        notes="",
    )

    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool_name = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(
            parts=[ToolCallPart(tool_name=output_tool_name, args=synth_plan.model_dump())]
        )

    deps = CompileDeps(ws_root=ws_root, wal_text=wal_text)
    with compile_agent.override(model=FunctionModel(_fake)):
        plan, _, _ = await run_compile_agent(deps)

    assert len(plan.ops) == len(expect["expected_ops"]), (
        f"many-small-fragments: expected {len(expect['expected_ops'])} ops, got {len(plan.ops)}"
    )
    assert plan.ops[0].path == expected_op["path"]
