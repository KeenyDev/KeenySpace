from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastmcp.exceptions import ToolError


@pytest.fixture
def search_ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import keenyspace_server.mcp.page_tools as page_tools

    ws_uuid = uuid4()
    ws_root = tmp_path / "workspaces" / str(ws_uuid)
    ws_root.mkdir(parents=True)
    workspace = SimpleNamespace(uuid=ws_uuid)
    app = SimpleNamespace(
        state=SimpleNamespace(settings=SimpleNamespace(fs=SimpleNamespace(root=tmp_path)))
    )

    class _Result:
        def scalar_one_or_none(self) -> SimpleNamespace:
            return workspace

    class _Session:
        async def execute(self, _stmt: object) -> _Result:
            return _Result()

    @asynccontextmanager
    async def _session():  # type: ignore[no-untyped-def]
        yield _Session()

    monkeypatch.setattr(page_tools, "current_user_from_mcp", lambda: None)
    monkeypatch.setattr(page_tools, "resolve_workspace", lambda ws: ws or "ws")
    monkeypatch.setattr(page_tools, "get_http_request", lambda: SimpleNamespace(app=app))
    monkeypatch.setattr(page_tools, "get_db_session", _session)
    return ws_root


async def _collect(query: str, limit: int, cursor: str | None = None) -> list[str]:
    from keenyspace_server.mcp.page_tools import search_workspace_tool

    collected: list[str] = []
    while True:
        resp = await search_workspace_tool(query=query, cursor=cursor, limit=limit, workspace="ws")
        assert len(resp.results) <= limit
        collected.extend(r.path for r in resp.results)
        cursor = resp.next_cursor
        if cursor is None:
            return collected


@pytest.mark.asyncio
async def test_search_pages_through_all_matches_in_path_order(search_ws: Path) -> None:
    names = [f"p{i:02d}.md" for i in range(7)]
    for name in names:
        (search_ws / name).write_text("needle")
    (search_ws / "p03a.md").write_text("miss")

    assert await _collect("needle", limit=2) == names


@pytest.mark.asyncio
async def test_search_last_full_page_has_no_next_cursor(search_ws: Path) -> None:
    from keenyspace_server.mcp.page_tools import search_workspace_tool

    for name in ("a.md", "b.md"):
        (search_ws / name).write_text("needle")
    resp = await search_workspace_tool(query="needle", limit=2, workspace="ws")
    assert [r.path for r in resp.results] == ["a.md", "b.md"]
    assert resp.next_cursor is None


@pytest.mark.asyncio
async def test_search_honours_legacy_offset_cursor(search_ws: Path) -> None:
    for name in ("a.md", "b.md", "c.md", "d.md"):
        (search_ws / name).write_text("needle")
    legacy = base64.urlsafe_b64encode(json.dumps({"o": 2}).encode()).decode()

    assert await _collect("needle", limit=1, cursor=legacy) == ["c.md", "d.md"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cursor", ["!!!not-base64", base64.urlsafe_b64encode(b'{"x": 1}').decode()]
)
async def test_search_rejects_malformed_cursor(search_ws: Path, cursor: str) -> None:
    from keenyspace_server.mcp.page_tools import search_workspace_tool

    with pytest.raises(ToolError, match="malformed cursor"):
        await search_workspace_tool(query="needle", cursor=cursor, workspace="ws")
