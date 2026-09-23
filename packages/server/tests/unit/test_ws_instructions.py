from __future__ import annotations

from pathlib import Path

import pytest

_DEFAULT_BUDGETS_FM = (
    "budgets:\n  max_steps: 10\n  max_tokens: 10000\n  max_seconds: 60"
)


def _write_instruction(path: Path, frontmatter: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = frontmatter
    if "budgets:" not in fm:
        fm = f"{fm}\n{_DEFAULT_BUDGETS_FM}"
    path.write_text(f"---\n{fm}\n---\n{body}")


@pytest.mark.asyncio
async def test_missing_file_raises_instruction_not_found(tmp_path: Path) -> None:
    from keenyspace_server.ws.instructions import (
        InstructionNotFoundError,
        load_and_render_instructions,
    )

    with pytest.raises(InstructionNotFoundError, match="ingest"):
        await load_and_render_instructions(
            tmp_path,
            command="ingest",
            workspace_meta={"slug": "my-ws"},
            context={},
        )


@pytest.mark.asyncio
async def test_renders_body_with_workspace_meta(tmp_path: Path) -> None:
    from keenyspace_server.ws.instructions import load_and_render_instructions

    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(instr_path, "tool_whitelist: []\nsteps: []", "Hello {{ workspace.slug }}")

    result = await load_and_render_instructions(
        tmp_path,
        command="ingest",
        workspace_meta={"slug": "my-ws"},
        context={},
    )
    assert result.prompt == "Hello my-ws"


@pytest.mark.asyncio
async def test_renders_body_with_context(tmp_path: Path) -> None:
    from keenyspace_server.ws.instructions import load_and_render_instructions

    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(instr_path, "tool_whitelist: []\nsteps: []", "Source: {{ context.source_path }}")

    result = await load_and_render_instructions(
        tmp_path,
        command="ingest",
        workspace_meta={"slug": "ws"},
        context={"source_path": "/data/src.md"},
    )
    assert "Source: /data/src.md" in result.prompt


@pytest.mark.asyncio
async def test_strict_undefined_raises_template_error(tmp_path: Path) -> None:
    from keenyspace_server.ws.instructions import (
        InstructionTemplateError,
        load_and_render_instructions,
    )

    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(instr_path, "tool_whitelist: []\nsteps: []", "{{ context.missing_key }}")

    with pytest.raises(InstructionTemplateError, match="missing context key"):
        await load_and_render_instructions(
            tmp_path,
            command="ingest",
            workspace_meta={"slug": "ws"},
            context={},
        )


@pytest.mark.asyncio
async def test_dunder_access_blocked_raises_template_error(tmp_path: Path) -> None:
    from keenyspace_server.ws.instructions import (
        InstructionTemplateError,
        load_and_render_instructions,
    )

    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(instr_path, "tool_whitelist: []\nsteps: []", "{{ workspace.__class__ }}")

    with pytest.raises(InstructionTemplateError, match="template injection blocked"):
        await load_and_render_instructions(
            tmp_path,
            command="ingest",
            workspace_meta={"slug": "ws", "__class__": "whatever"},
            context={},
        )


@pytest.mark.asyncio
async def test_oversized_file_raises_template_error(tmp_path: Path) -> None:
    from keenyspace_server.ws.instructions import (
        InstructionTemplateError,
        load_and_render_instructions,
    )

    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    instr_path.parent.mkdir(parents=True, exist_ok=True)
    instr_path.write_bytes(b"x" * (65 * 1024))

    with pytest.raises(InstructionTemplateError, match="too large"):
        await load_and_render_instructions(
            tmp_path,
            command="ingest",
            workspace_meta={"slug": "ws"},
            context={},
        )


@pytest.mark.asyncio
async def test_tool_whitelist_must_be_list_of_strings(tmp_path: Path) -> None:
    from keenyspace_server.ws.instructions import (
        InstructionTemplateError,
        load_and_render_instructions,
    )

    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(instr_path, "tool_whitelist: not_a_list\nsteps: []", "body")

    with pytest.raises(InstructionTemplateError):
        await load_and_render_instructions(
            tmp_path,
            command="ingest",
            workspace_meta={"slug": "ws"},
            context={},
        )


@pytest.mark.asyncio
async def test_steps_must_be_list_of_strings(tmp_path: Path) -> None:
    from keenyspace_server.ws.instructions import (
        InstructionTemplateError,
        load_and_render_instructions,
    )

    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(instr_path, "tool_whitelist: []\nsteps:\n  k: v", "body")

    with pytest.raises(InstructionTemplateError):
        await load_and_render_instructions(
            tmp_path,
            command="ingest",
            workspace_meta={"slug": "ws"},
            context={},
        )


@pytest.mark.asyncio
async def test_model_optional(tmp_path: Path) -> None:
    from keenyspace_server.ws.instructions import load_and_render_instructions

    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(instr_path, "tool_whitelist: []\nsteps: []", "body")

    result = await load_and_render_instructions(
        tmp_path,
        command="ingest",
        workspace_meta={"slug": "ws"},
        context={},
    )
    assert result.model is None


@pytest.mark.asyncio
async def test_model_string_passes_through(tmp_path: Path) -> None:
    from keenyspace_server.ws.instructions import load_and_render_instructions

    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(instr_path, "tool_whitelist: []\nsteps: []\nmodel: claude-sonnet-4-5", "body")

    result = await load_and_render_instructions(
        tmp_path,
        command="ingest",
        workspace_meta={"slug": "ws"},
        context={},
    )
    assert result.model == "claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_rejects_template_with_too_many_ast_nodes(tmp_path: Path) -> None:
    """WR-02/WR-13: AST node-count bound rejects unbounded templates.

    150 ``{{ xN }}`` interpolations expand to substantially more than 200
    nodes in Jinja's AST (each expression is wrapped in Output/Name nodes),
    triggering the ``_TEMPLATE_MAX_AST_NODES`` ceiling defined in
    ``ws/instructions.py``. Locking this in keeps a future refactor from
    silently raising or removing the bound.
    """
    from keenyspace_server.ws.instructions import (
        InstructionTemplateError,
        load_and_render_instructions,
    )

    body = "".join(f"{{{{ context.x{i} }}}}" for i in range(150))
    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(instr_path, "tool_whitelist: []\nsteps: []", body)
    ctx = {f"x{i}": str(i) for i in range(150)}

    with pytest.raises(InstructionTemplateError, match="too complex"):
        await load_and_render_instructions(
            tmp_path,
            command="ingest",
            workspace_meta={"slug": "ws"},
            context=ctx,
        )


@pytest.mark.asyncio
async def test_rejects_template_with_loop_nesting_too_deep(tmp_path: Path) -> None:
    """WR-02/WR-13: nested-for depth bound rejects pathological recursion.

    Four nested ``{% for %}`` loops exceed the ``_TEMPLATE_MAX_LOOP_DEPTH``
    bound of 3. The complexity check runs BEFORE the asyncio.to_thread render
    handoff so a malicious template never reaches the uncancellable worker.
    """
    from keenyspace_server.ws.instructions import (
        InstructionTemplateError,
        load_and_render_instructions,
    )

    body = (
        "{% for a in [1] %}"
        "{% for b in [1] %}"
        "{% for c in [1] %}"
        "{% for d in [1] %}x{% endfor %}"
        "{% endfor %}"
        "{% endfor %}"
        "{% endfor %}"
    )
    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(instr_path, "tool_whitelist: []\nsteps: []", body)

    with pytest.raises(InstructionTemplateError, match="loop nesting"):
        await load_and_render_instructions(
            tmp_path,
            command="ingest",
            workspace_meta={"slug": "ws"},
            context={},
        )


@pytest.mark.asyncio
async def test_allows_template_within_complexity_bounds(tmp_path: Path) -> None:
    """WR-02/WR-13: regression guard that idiomatic templates still render.

    Three nested loops are at (not over) the depth bound; ensures the bound
    is correctly applied as exclusive-on-overflow, not off-by-one strict.
    """
    from keenyspace_server.ws.instructions import load_and_render_instructions

    body = (
        "{% for a in context.xs %}"
        "{% for b in context.xs %}"
        "{% for c in context.xs %}{{ a }}{{ b }}{{ c }}{% endfor %}"
        "{% endfor %}"
        "{% endfor %}"
    )
    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(instr_path, "tool_whitelist: []\nsteps: []", body)

    result = await load_and_render_instructions(
        tmp_path,
        command="ingest",
        workspace_meta={"slug": "ws"},
        context={"xs": [1]},
    )
    assert result.prompt == "111"


@pytest.mark.asyncio
async def test_steps_rendered_with_context(tmp_path: Path) -> None:
    from keenyspace_server.ws.instructions import load_and_render_instructions

    instr_path = tmp_path / ".keenyspace" / "instructions" / "ingest.md"
    _write_instruction(
        instr_path,
        "tool_whitelist: []\nsteps:\n  - 'Load {{ context.source_path }}'\n  - Done",
        "body",
    )

    result = await load_and_render_instructions(
        tmp_path,
        command="ingest",
        workspace_meta={"slug": "ws"},
        context={"source_path": "/data/x.md"},
    )
    assert "/data/x.md" in result.steps[0]
    assert result.steps[1] == "Done"
