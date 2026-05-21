from __future__ import annotations

import asyncio
import re
from pathlib import Path

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.utilities.pagination import paginate_sequence
from keenyspace_shared.mcp_contracts import (
    ListPagesResponse,
    SearchResponse,
    SearchResult,
)
from sqlalchemy import select

from keenyspace_server.db.models import Workspace
from keenyspace_server.db.session import get_db_session
from keenyspace_server.mcp.auth_bridge import current_user_from_mcp
from keenyspace_server.observability.metrics import MCP_TOOL_CALL_DURATION
from keenyspace_server.ws.search import list_md_paths, search_workspace_files

_PAGE_SIZE_DEFAULT = 50
_PAGE_SIZE_MAX = 200
_PREFIX_MAX_LEN = 512
_QUERY_MAX_LEN = 512


def _validated_limit(limit: int | None) -> int:
    if limit is None:
        return _PAGE_SIZE_DEFAULT
    return min(max(1, limit), _PAGE_SIZE_MAX)


def _validate_prefix(prefix: str) -> str:
    if not prefix:
        raise ToolError("prefix must not be empty")
    if len(prefix) > _PREFIX_MAX_LEN:
        raise ToolError(f"prefix exceeds maximum length of {_PREFIX_MAX_LEN}")
    if "\x00" in prefix:
        raise ToolError("prefix contains NUL byte")
    if prefix.startswith("/") or prefix.startswith("\\"):
        raise ToolError("prefix must not start with / or \\")
    parts = prefix.replace("\\", "/").split("/")
    for part in parts:
        if part in (".", ".."):
            raise ToolError(f"prefix contains dot-segment: {part!r}")
        if part.startswith("."):
            raise ToolError(f"prefix contains hidden component: {part!r}")
    return prefix


async def list_pages_tool(
    workspace: str,
    prefix: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> ListPagesResponse:
    """List .md pages in a workspace (MCP-04). Cursor-paginated."""
    with MCP_TOOL_CALL_DURATION.labels(tool="list_pages_tool").time():
        _ = current_user_from_mcp()

        req = get_http_request()
        app = req.app

        async with get_db_session() as session:
            ws = (
                await session.execute(
                    select(Workspace).where(Workspace.slug == workspace)
                )
            ).scalar_one_or_none()

        if ws is None:
            raise ToolError(f"workspace {workspace!r} not found")

        prefix_norm: str | None = None
        if prefix is not None:
            prefix_norm = _validate_prefix(prefix)

        settings = app.state.settings
        ws_root = Path(settings.fs.root) / "workspaces" / str(ws.uuid)
        all_paths = await asyncio.to_thread(list_md_paths, ws_root, prefix_norm)

        page_size = _validated_limit(limit)
        try:
            page, next_cursor = paginate_sequence(
                all_paths, cursor=cursor, page_size=page_size
            )
        except (ValueError, TypeError) as exc:
            raise ToolError(f"malformed cursor: {exc}") from exc

        return ListPagesResponse(pages=page, next_cursor=next_cursor)


async def search_workspace_tool(
    workspace: str,
    query: str,
    cursor: str | None = None,
    limit: int | None = None,
) -> SearchResponse:
    """Search workspace pages by filename + content (MCP-05). Cursor-paginated."""
    with MCP_TOOL_CALL_DURATION.labels(tool="search_workspace_tool").time():
        _ = current_user_from_mcp()

        req = get_http_request()
        app = req.app

        async with get_db_session() as session:
            ws = (
                await session.execute(
                    select(Workspace).where(Workspace.slug == workspace)
                )
            ).scalar_one_or_none()

        if ws is None:
            raise ToolError(f"workspace {workspace!r} not found")

        if not query:
            raise ToolError("query must not be empty")
        if len(query) > _QUERY_MAX_LEN:
            raise ToolError(f"query exceeds maximum length of {_QUERY_MAX_LEN}")

        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as exc:
            raise ToolError(f"invalid search query (regex): {exc}") from exc

        settings = app.state.settings
        ws_root = Path(settings.fs.root) / "workspaces" / str(ws.uuid)
        matches = await asyncio.to_thread(search_workspace_files, ws_root, pattern)

        page_size = _validated_limit(limit)
        try:
            page, next_cursor = paginate_sequence(
                matches, cursor=cursor, page_size=page_size
            )
        except (ValueError, TypeError) as exc:
            raise ToolError(f"malformed cursor: {exc}") from exc

        results = [SearchResult(path=p) for p in page]
        return SearchResponse(results=results, next_cursor=next_cursor)
