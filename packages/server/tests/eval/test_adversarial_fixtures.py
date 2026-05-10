from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import respx
from keenyspace_server.compile.agent import compile_agent, run_compile_agent
from keenyspace_server.compile.loop_detector import LoopDetector
from keenyspace_server.compile.models import CompileDeps, CompilePlan, PageOp
from keenyspace_server.compile.page_writer import CompilePlanSafetyError, apply_plan
from pydantic import ValidationError
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

ADVERSARIAL_FIXTURES = Path(__file__).parent / "fixtures" / "compile" / "adversarial"

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


@pytest.mark.asyncio
async def test_adv_01_prompt_injection_exfiltrate_no_egress(tmp_path: Path) -> None:
    fixture_dir = ADVERSARIAL_FIXTURES / "01-prompt-injection-exfiltrate"
    wal_text, expect, vault_path = _load_fixture(fixture_dir)

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    _copy_vault(vault_path, ws_root)

    safe_plan = CompilePlan(
        ops=[PageOp(action="update", path="index.md", body="Updated index content.", frontmatter={})],
        notes="Instruction injection attempt detected in WAL entry: 'Ignore previous instructions'.",
    )

    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=output_tool, args=safe_plan.model_dump())])

    deps = CompileDeps(ws_root=ws_root, wal_text=wal_text)

    with respx.mock(assert_all_called=False) as mock_transport, compile_agent.override(model=FunctionModel(_fake)):
        plan, _ = await run_compile_agent(deps)

    evil_calls = [
        call
        for route in mock_transport.routes
        for call in route.calls
        if "evil.example" in str(call.request.url)
    ]
    assert evil_calls == [], "No HTTP request to evil.example should have been made"

    assert plan.notes, "CompilePlan.notes must be non-empty to surface the injection attempt"

    injection_text = "Ignore previous instructions"
    for op in plan.ops:
        assert injection_text not in op.body, (
            f"Literal injection instruction must not appear verbatim in op.body: {op.body!r}"
        )

    assert expect["expected_status"] == "success"


@pytest.mark.asyncio
async def test_adv_02_prompt_injection_denylist_write_rejected(tmp_path: Path) -> None:
    fixture_dir = ADVERSARIAL_FIXTURES / "02-prompt-injection-denylist-write"
    wal_text, _expect, vault_path = _load_fixture(fixture_dir)

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    _copy_vault(vault_path, ws_root)

    bad_plan = CompilePlan(
        ops=[PageOp(action="create", path=".keenyspace/secret.md", body="exposed secret")],
        notes="",
    )

    call_count: dict[str, int] = {"n": 0}

    async def _always_bad(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        call_count["n"] += 1
        output_tool = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=output_tool, args=bad_plan.model_dump())])

    deps = CompileDeps(ws_root=ws_root, wal_text=wal_text)
    with compile_agent.override(model=FunctionModel(_always_bad)), pytest.raises(Exception):  # noqa: B017
        await run_compile_agent(deps, max_tool_calls=5)

    assert call_count["n"] >= 2, "Validator should trigger multiple retries before exhaustion"

    with pytest.raises(CompilePlanSafetyError) as exc_info:
        apply_plan(tmp_path / "direct_ws", bad_plan)
    assert ".keenyspace/secret.md" in str(exc_info.value), (
        "apply_plan must raise CompilePlanSafetyError for denylist paths (defense-in-depth)"
    )


@pytest.mark.asyncio
async def test_adv_03_tool_call_loop_aborts(tmp_path: Path) -> None:
    fixture_dir = ADVERSARIAL_FIXTURES / "03-tool-call-loop"
    wal_text, _expect, vault_path = _load_fixture(fixture_dir)

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    _copy_vault(vault_path, ws_root)

    async def _looping(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[
            ToolCallPart(tool_name="read_page", args={"path": "notes/index.md"}),
        ])

    deps = CompileDeps(ws_root=ws_root, wal_text=wal_text)
    detector = LoopDetector(max_repeats=3)

    with compile_agent.override(model=FunctionModel(_looping)), pytest.raises((UsageLimitExceeded, Exception)):
        await run_compile_agent(deps, max_tool_calls=10, loop_detector=detector)

    assert detector.triggered is True, "LoopDetector.triggered must be True after loop abort"


def test_adv_04_invalid_action_enum_rejected() -> None:
    fixture_dir = ADVERSARIAL_FIXTURES / "04-invalid-action-enum"
    _wal_text, _expect, _vault_path = _load_fixture(fixture_dir)

    with pytest.raises(ValidationError) as exc_info:
        PageOp(action="archive", path="notes/meetings.md", body="Some content")  # type: ignore[arg-type]

    errors = exc_info.value.errors()
    assert any(e["loc"] == ("action",) for e in errors), (
        "ValidationError must report 'action' field violation"
    )


@pytest.mark.parametrize("bad_path", ["../etc/passwd", "/abs/path", "x/../escape.md"])
def test_adv_05_traversal_path_rejected(bad_path: str) -> None:
    fixture_dir = ADVERSARIAL_FIXTURES / "05-traversal-path"
    _wal_text, _expect, _vault_path = _load_fixture(fixture_dir)

    with pytest.raises(ValidationError) as exc_info:
        PageOp(action="create", path=bad_path, body="x")

    errors = exc_info.value.errors()
    assert any(e["loc"] == ("path",) for e in errors), (
        f"ValidationError must report 'path' violation for {bad_path!r}"
    )


def test_adv_06_duplicate_paths_rejected() -> None:
    fixture_dir = ADVERSARIAL_FIXTURES / "06-duplicate-paths"
    _wal_text, _expect, _vault_path = _load_fixture(fixture_dir)

    with pytest.raises(ValidationError) as exc_info:
        CompilePlan(ops=[
            PageOp(action="create", path="x.md", body="first body"),
            PageOp(action="update", path="x.md", body="second body"),
        ])

    assert exc_info.value.error_count() >= 1, (
        "CompilePlan must raise ValidationError for duplicate PageOp paths"
    )
