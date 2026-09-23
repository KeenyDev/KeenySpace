from __future__ import annotations

from pathlib import Path

import pytest
from keenyspace_server.ws.instructions import (
    InstructionTemplateError,
    load_and_render_instructions,
)


def _write_instruction(path: Path, frontmatter: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}")


@pytest.mark.asyncio
async def test_budgets_missing_raises(tmp_path: Path) -> None:
    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(instr_path, "tool_whitelist: []\nsteps: []", "body")

    with pytest.raises(InstructionTemplateError, match="budgets"):
        await load_and_render_instructions(
            tmp_path,
            command="ingest",
            workspace_meta={"slug": "ws"},
            context={},
        )


@pytest.mark.asyncio
async def test_budgets_malformed_raises(tmp_path: Path) -> None:
    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(
        instr_path,
        "tool_whitelist: []\nsteps: []\nbudgets: not_a_dict",
        "body",
    )

    with pytest.raises(InstructionTemplateError, match="budgets"):
        await load_and_render_instructions(
            tmp_path,
            command="ingest",
            workspace_meta={"slug": "ws"},
            context={},
        )


@pytest.mark.asyncio
async def test_budgets_invalid_fields_raises(tmp_path: Path) -> None:
    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(
        instr_path,
        (
            "tool_whitelist: []\n"
            "steps: []\n"
            "budgets:\n"
            "  max_steps: not-an-int\n"
            "  max_tokens: 1000\n"
            "  max_seconds: 30"
        ),
        "body",
    )

    with pytest.raises(InstructionTemplateError, match="invalid budgets"):
        await load_and_render_instructions(
            tmp_path,
            command="ingest",
            workspace_meta={"slug": "ws"},
            context={},
        )


@pytest.mark.asyncio
async def test_budgets_missing_field_raises(tmp_path: Path) -> None:
    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(
        instr_path,
        ("tool_whitelist: []\nsteps: []\nbudgets:\n  max_steps: 5\n  max_tokens: 1000"),
        "body",
    )

    with pytest.raises(InstructionTemplateError, match="invalid budgets"):
        await load_and_render_instructions(
            tmp_path,
            command="ingest",
            workspace_meta={"slug": "ws"},
            context={},
        )


@pytest.mark.asyncio
async def test_budgets_valid_populated(tmp_path: Path) -> None:
    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(
        instr_path,
        (
            "tool_whitelist: []\n"
            "steps: []\n"
            "budgets:\n"
            "  max_steps: 7\n"
            "  max_tokens: 1234\n"
            "  max_seconds: 42"
        ),
        "body",
    )

    result = await load_and_render_instructions(
        tmp_path,
        command="ingest",
        workspace_meta={"slug": "ws"},
        context={},
    )
    assert result.budgets.max_steps == 7
    assert result.budgets.max_tokens == 1234
    assert result.budgets.max_seconds == 42


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    ["ingest", "query", "lint", "post-compact"],
)
async def test_default_blueprint_instructions_render_ok(tmp_path: Path, command: str) -> None:
    """Every instruction shipped with the default blueprint renders with valid budgets."""

    repo_root = Path(__file__).resolve().parents[4]
    src = repo_root / "blueprints" / "default" / "_instructions" / f"{command}.md"
    assert src.is_file(), f"missing blueprint instruction {src}"

    instr_path = tmp_path / ".keenyspace" / "instructions" / f"{command}.md"
    instr_path.parent.mkdir(parents=True, exist_ok=True)
    instr_path.write_bytes(src.read_bytes())

    context: dict[str, object] = {
        "source_path": "/data/x.md",
        "question": "what?",
        "transcript_excerpt": "session log...",
    }
    result = await load_and_render_instructions(
        tmp_path,
        command=command,
        workspace_meta={"slug": "demo"},
        context=context,
    )
    assert result.budgets.max_steps > 0
    assert result.budgets.max_tokens > 0
    assert result.budgets.max_seconds > 0
    assert isinstance(result.tool_whitelist, list)
    if command in {"query", "lint", "post-compact"}:
        assert "append_log" not in result.tool_whitelist
