"""fastmcp Client wrapper helpers for client-side MCP tool invocations.

Per Phase 5 D-02/D-05/CLI-13: every server-driven command calls
`get_instructions(workspace, command, context)` FIRST. This module also
exposes thin wrappers for `compile` + `compile_status` MCP tools used by
the `keenyspace compile` CLI surface.

`_coerce` defensively handles fastmcp 3.2 CallToolResult: prefer
`structured_content` (dict from output_schema), fall back to the first
TextContent payload parsed as JSON, finally pass `.data` through.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from keenyspace_shared.mcp_contracts import Instructions


def build_mcp_client(server_url: str, api_key: str) -> Client:
    base = server_url.rstrip("/")
    return Client(f"{base}/v1/mcp/", auth=BearerAuth(api_key))


async def get_instructions(
    server_url: str,
    api_key: str,
    *,
    workspace: str,
    command: str,
    context: dict[str, Any],
) -> Instructions:
    async with build_mcp_client(server_url, api_key) as client:
        result = await client.call_tool(
            "get_instructions",
            {"workspace": workspace, "command": command, "context": context},
        )
    payload = _coerce(result)
    return Instructions.model_validate(payload)


async def call_compile(
    server_url: str, api_key: str, *, workspace: str
) -> dict[str, Any]:
    async with build_mcp_client(server_url, api_key) as client:
        result = await client.call_tool("compile", {"workspace": workspace})
    return _coerce(result)


async def call_compile_status(
    server_url: str, api_key: str, *, workspace: str
) -> dict[str, Any]:
    async with build_mcp_client(server_url, api_key) as client:
        result = await client.call_tool("compile_status", {"workspace": workspace})
    return _coerce(result)


def _coerce(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        first = content[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
            if isinstance(parsed, dict):
                return parsed
            return {"raw": parsed}
    if isinstance(result, dict):
        return result
    return {"raw": result}
