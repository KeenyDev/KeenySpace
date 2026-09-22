from __future__ import annotations

from functools import partial
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
from keenyspace_server.mcp.auth_bridge import current_user_from_mcp, resolve_workspace
from keenyspace_server.observability.metrics import MCP_TOOL_CALL_DURATION
from keenyspace_server.ws.cursor import decode_cursor, encode_cursor
from keenyspace_server.ws.search import (
    VAULT_SCAN_SLOTS,
    list_md_paths,
    search_workspace_files,
)
from keenyspace_server.ws.thread_slots import run_in_thread_slot

_PAGE_SIZE_DEFAULT = 50
_PAGE_SIZE_MAX = 200
_PREFIX_MAX_LEN = 512
_QUERY_MAX_LEN = 512


def _validated_limit(limit: int | None) -> int:
    if limit is None:
        return _PAGE_SIZE_DEFAULT
    return min(max(1, limit), _PAGE_SIZE_MAX)


def _decode_search_cursor(cursor: str | None) -> tuple[str | None, int]:
    """Return `(after_path, skip)` for a search cursor.

    Current cursors are keyset (`after`: last path returned). Offset cursors
    (`o`) issued before the keyset switch are still honoured as a skip count
    so pagination in flight across an upgrade does not break.
    """
    if cursor is None:
        return None, 0
    data = decode_cursor(cursor)
    after = data.get("after")
    if isinstance(after, str):
        return after, 0
    offset = data.get("o")
    if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0:
        return None, offset
    raise ValueError("malformed search cursor (missing after)")


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
    workspace: str | None = None,
    prefix: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> ListPagesResponse:
    """List .md pages in a workspace (MCP-04). Cursor-paginated."""
    with MCP_TOOL_CALL_DURATION.labels(tool="list_pages_tool").time():
        _ = current_user_from_mcp()
        workspace = resolve_workspace(workspace)

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
        all_paths = await run_in_thread_slot(
            VAULT_SCAN_SLOTS, list_md_paths, ws_root, prefix_norm
        )

        page_size = _validated_limit(limit)
        try:
            page, next_cursor = paginate_sequence(
                all_paths, cursor=cursor, page_size=page_size
            )
        except (ValueError, TypeError) as exc:
            raise ToolError(f"malformed cursor: {exc}") from exc

        return ListPagesResponse(pages=page, next_cursor=next_cursor)


async def search_workspace_tool(
    query: str,
    cursor: str | None = None,
    limit: int | None = None,
    workspace: str | None = None,
) -> SearchResponse:
    """Search workspace pages by filename + content (MCP-05). Cursor-paginated."""
    with MCP_TOOL_CALL_DURATION.labels(tool="search_workspace_tool").time():
        _ = current_user_from_mcp()
        workspace = resolve_workspace(workspace)

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

        # MCP-05 contract makes no mention of regex semantics; treat query as
        # a case-insensitive literal substring. This eliminates the ReDoS
        # surface that arbitrary user-supplied regex would create on a
        # single-worker uvicorn (a pathological pattern can stall the entire
        # process). See WR-08.
        settings = app.state.settings
        ws_root = Path(settings.fs.root) / "workspaces" / str(ws.uuid)
        page_size = _validated_limit(limit)
        try:
            after, skip = _decode_search_cursor(cursor)
        except ValueError as exc:
            raise ToolError(f"malformed cursor: {exc}") from exc

        matches = await run_in_thread_slot(
            VAULT_SCAN_SLOTS,
            partial(
                search_workspace_files,
                ws_root,
                query,
                after=after,
                skip=skip,
                limit=page_size + 1,
            ),
        )

        page = matches[:page_size]
        next_cursor = (
            encode_cursor({"after": page[-1]}) if len(matches) > page_size else None
        )
        results = [SearchResult(path=p) for p in page]
        return SearchResponse(results=results, next_cursor=next_cursor)
