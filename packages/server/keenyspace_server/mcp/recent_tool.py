from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from keenyspace_shared.mcp_contracts import RecentChange, RecentChangesResponse
from sqlalchemy import select

from keenyspace_server.db.models import Workspace
from keenyspace_server.db.session import get_db_session
from keenyspace_server.mcp.auth_bridge import current_user_from_mcp
from keenyspace_server.observability.metrics import MCP_TOOL_CALL_DURATION
from keenyspace_server.ws.cursor import decode_mtime_cursor, encode_mtime_cursor
from keenyspace_server.ws.recent import scan_recent_changes

_PAGE_SIZE_DEFAULT = 50
_PAGE_SIZE_MAX = 200


def _validated_limit(limit: int | None) -> int:
    if limit is None:
        return _PAGE_SIZE_DEFAULT
    return min(max(1, limit), _PAGE_SIZE_MAX)


async def get_recent_changes_tool(
    workspace: str,
    since: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> RecentChangesResponse:
    """Return pages modified since cursor or ISO timestamp (MCP-09).

    Sort order: (mtime_ns DESC, path ASC). Custom cursor `(mtime_ns, path)`
    stable across concurrent FS writes (RESEARCH §Pattern 4).
    """
    with MCP_TOOL_CALL_DURATION.labels(tool="get_recent_changes_tool").time():
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

        since_ns: int | None = None
        if since is not None:
            try:
                since_dt = datetime.fromisoformat(since)
            except ValueError as exc:
                raise ToolError(f"invalid since timestamp: {exc}") from exc
            since_ns = int(since_dt.timestamp() * 1_000_000_000)

        settings = app.state.settings
        ws_root = Path(settings.fs.root) / "workspaces" / str(ws.uuid)
        all_items = await asyncio.to_thread(scan_recent_changes, ws_root, since_ns)

        if cursor is not None:
            try:
                cursor_mtime_ns, cursor_path = decode_mtime_cursor(cursor)
            except ValueError as exc:
                raise ToolError(f"malformed cursor: {exc}") from exc
            cursor_key = (-cursor_mtime_ns, cursor_path)
            all_items = [
                (m, p) for (m, p) in all_items if (-m, p) > cursor_key
            ]

        page_size = _validated_limit(limit)
        page = all_items[:page_size]
        remaining = all_items[page_size:]

        next_cursor: str | None = None
        if page and remaining:
            last_mtime, last_path = page[-1]
            next_cursor = encode_mtime_cursor(last_mtime, last_path)

        changes = [RecentChange(path=p, mtime_ns=m) for m, p in page]
        return RecentChangesResponse(changes=changes, next_cursor=next_cursor)
