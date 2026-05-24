"""Tool-whitelist enforcement for client-side pydantic-ai agents.

Wave 1 SUMMARY documented that pydantic-ai-slim 1.93.0's
`MCPServerStreamableHTTP.__init__` does NOT accept a `tool_filter` kwarg
but DOES expose a `process_tool_call` hook. We build a hook closure that
rejects any tool whose name is not in the whitelist.

`supports_tool_filter()` remains True for the `process_tool_call` path so
callers can branch (legacy plan text referenced `tool_filter`; we provide
the hook builder either way).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any


def supports_tool_filter() -> bool:
    """True when MCPServerStreamableHTTP exposes any tool-filter mechanism."""
    try:
        from pydantic_ai.mcp import MCPServerStreamableHTTP
    except Exception:
        return False
    sig = inspect.signature(MCPServerStreamableHTTP.__init__)
    return (
        "tool_filter" in sig.parameters
        or "process_tool_call" in sig.parameters
        or hasattr(MCPServerStreamableHTTP, "filtered")
    )


class ToolWhitelistViolation(Exception):  # noqa: N818 — "Violation" preserves the security semantics; not a generic Error
    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"Tool {tool_name!r} not in whitelist")


def make_process_tool_call(
    tool_whitelist: list[str],
) -> Callable[..., Awaitable[Any]]:
    """Build a process_tool_call hook that aborts on out-of-whitelist tools.

    Signature per pydantic_ai.mcp.MCPServerStreamableHTTP.process_tool_call:
    `async def hook(ctx, call_tool, name, args) -> Any`.
    """

    allowed = set(tool_whitelist)

    async def hook(
        ctx: Any,
        call_tool: Callable[..., Awaitable[Any]],
        name: str,
        args: dict[str, Any],
    ) -> Any:
        if name not in allowed:
            raise ToolWhitelistViolation(name)
        return await call_tool(name, args)

    return hook
