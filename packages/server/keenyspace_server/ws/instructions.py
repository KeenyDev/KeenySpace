from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import jinja2
import structlog
from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment
from keenyspace_shared.mcp_contracts import Instructions

from keenyspace_server.mcp.tools import _split_frontmatter

log = structlog.get_logger(__name__)

_INSTRUCTIONS_MAX_BYTES = 64 * 1024
_OUTPUT_MAX_CHARS = 32 * 1024
_RENDER_TIMEOUT_SECONDS = 5.0

_JINJA_ENV = SandboxedEnvironment(
    undefined=jinja2.StrictUndefined,
    autoescape=False,
)


class InstructionNotFoundError(ValueError):
    pass


class InstructionTemplateError(ValueError):
    pass


def _render_one(template_str: str, ctx: dict[str, Any]) -> str:
    tmpl = _JINJA_ENV.from_string(template_str)
    return tmpl.render(**ctx)


async def _render_async(template_str: str, ctx: dict[str, Any]) -> str:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_render_one, template_str, ctx),
            timeout=_RENDER_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise InstructionTemplateError("template render timeout") from exc
    except SecurityError as exc:
        raise InstructionTemplateError(f"template injection blocked: {exc}") from exc
    except jinja2.UndefinedError as exc:
        raise InstructionTemplateError(f"missing context key in template: {exc}") from exc
    except jinja2.TemplateSyntaxError as exc:
        raise InstructionTemplateError(f"template syntax error: {exc}") from exc


def _truncate(rendered: str, label: str) -> str:
    if len(rendered) <= _OUTPUT_MAX_CHARS:
        return rendered
    log.warning(
        "instructions.output_truncated",
        label=label,
        original_len=len(rendered),
        max_len=_OUTPUT_MAX_CHARS,
    )
    return rendered[:_OUTPUT_MAX_CHARS]


async def load_and_render_instructions(
    ws_dir: Path,
    *,
    command: str,
    workspace_meta: dict[str, Any],
    context: dict[str, Any],
) -> Instructions:
    instructions_path = ws_dir / ".keenyspace" / "instructions" / f"{command}.md"
    if not instructions_path.is_file():
        raise InstructionNotFoundError(f"command {command!r} not found for workspace")

    raw_bytes = instructions_path.read_bytes()
    if len(raw_bytes) > _INSTRUCTIONS_MAX_BYTES:
        raise InstructionTemplateError(
            f"instructions file too large: {len(raw_bytes)} bytes "
            f"(max {_INSTRUCTIONS_MAX_BYTES})"
        )
    content = raw_bytes.decode("utf-8", errors="replace")

    frontmatter, body = _split_frontmatter(content)

    tool_whitelist = frontmatter.get("tool_whitelist", [])
    if not isinstance(tool_whitelist, list) or not all(
        isinstance(t, str) for t in tool_whitelist
    ):
        raise InstructionTemplateError(
            "frontmatter.tool_whitelist must be a list of strings"
        )

    steps_raw = frontmatter.get("steps", [])
    if not isinstance(steps_raw, list) or not all(
        isinstance(s, str) for s in steps_raw
    ):
        raise InstructionTemplateError("frontmatter.steps must be a list of strings")

    model_raw = frontmatter.get("model")
    if model_raw is not None and not isinstance(model_raw, str):
        raise InstructionTemplateError("frontmatter.model must be a string or null")

    ctx: dict[str, Any] = {"workspace": workspace_meta, "context": context}
    rendered_body = _truncate(await _render_async(body, ctx), label="prompt")
    rendered_steps = [
        _truncate(await _render_async(step, ctx), label=f"steps[{i}]")
        for i, step in enumerate(steps_raw)
    ]

    return Instructions(
        prompt=rendered_body,
        tool_whitelist=list(tool_whitelist),
        steps=rendered_steps,
        model=model_raw,
    )
