from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastmcp.exceptions import ToolError
from keenyspace_server.auth.user import User
from keenyspace_server.mcp import tools
from keenyspace_server.wal import writer as wal_writer
from keenyspace_server.wal.locks import WorkspaceLockRegistry
from keenyspace_server.wal.parser import parse_wal
from ulid import ULID

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mcp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = SimpleNamespace(uuid=uuid4(), slug="ws")
    settings = SimpleNamespace(
        fs=SimpleNamespace(root=tmp_path),
        wal=SimpleNamespace(max_entry_bytes=1024),
        auth=SimpleNamespace(multi_worker=False),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(settings=settings, wal_locks=WorkspaceLockRegistry())
    )
    request = SimpleNamespace(app=app, headers={})

    @contextlib.asynccontextmanager
    async def _session() -> AsyncIterator[Any]:
        result = SimpleNamespace(scalar_one_or_none=lambda: ws)

        async def _execute(_stmt: object) -> object:
            return result

        yield SimpleNamespace(execute=_execute)

    monkeypatch.setattr(
        tools, "current_user_from_mcp", lambda: User(sub="u", _display_name="u", source="api_key")
    )
    monkeypatch.setattr(tools, "resolve_workspace", lambda w: w or "ws")
    monkeypatch.setattr(tools, "get_http_request", lambda: request)
    monkeypatch.setattr(tools, "get_db_session", _session)
    return tmp_path / "workspaces" / str(ws.uuid)


async def test_append_log_returns_entry_ts(mcp_env: Path) -> None:
    resp = await tools.append_log(content="a fact", workspace="ws")

    (log_file,) = (mcp_env / "logs").glob("*.md")
    (entry,) = parse_wal(log_file.read_text())
    assert entry.id == ULID.from_str(resp.entry_id)
    assert entry.ts == resp.ts


async def test_malformed_parent_id_is_a_tool_error(mcp_env: Path) -> None:
    with pytest.raises(ToolError, match="invalid parent_id"):
        await tools.append_log(content="a fact", parent_id="not-a-ulid", workspace="ws")

    assert not (mcp_env / "logs").exists()


@pytest.mark.parametrize("content", ["", "  "])
async def test_empty_content_is_a_tool_error(mcp_env: Path, content: str) -> None:
    with pytest.raises(ToolError, match="must not be empty"):
        await tools.append_log(content=content, workspace="ws")


async def test_oversized_content_is_a_tool_error(mcp_env: Path) -> None:
    with pytest.raises(ToolError, match="maximum size"):
        await tools.append_log(content="x" * 2048, workspace="ws")


async def test_archived_workspace_is_a_tool_error(
    mcp_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _archived(**_kwargs: object) -> wal_writer.AppendResult:
        raise wal_writer.WorkspaceArchivedError("workspace is archived")

    monkeypatch.setattr(wal_writer, "append_log", _archived)

    with pytest.raises(ToolError, match="archived"):
        await tools.append_log(content="a fact", workspace="ws")


async def test_read_page_parses_frontmatter_off_the_event_loop(mcp_env: Path) -> None:
    mcp_env.mkdir(parents=True)
    (mcp_env / "note.md").write_text("---\ntitle: Note\n---\nbody text\n")

    resp = await tools.read_page("note", workspace="ws")

    assert resp.path == "note.md"
    assert resp.frontmatter == {"title": "Note"}
    assert resp.content == "body text\n"


async def test_read_page_missing_is_a_tool_error(mcp_env: Path) -> None:
    mcp_env.mkdir(parents=True)

    with pytest.raises(ToolError, match="not found"):
        await tools.read_page("missing", workspace="ws")
