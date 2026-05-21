from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AppendLogRequest(BaseModel):
    workspace: str
    content: str
    parent_id: str | None = None


class AppendLogResponse(BaseModel):
    entry_id: str
    ts: datetime


class ReadPageResponse(BaseModel):
    path: str
    content: str
    frontmatter: dict[str, Any]


class WorkspaceInfo(BaseModel):
    uuid: str
    slug: str
    status: str
    blueprint_pin: str
    archived_at: datetime | None = None
    compile_state: str
    page_count: int
    last_compile_at: datetime | None = None


class ListWorkspacesResponse(BaseModel):
    workspaces: list[WorkspaceInfo]
    next_cursor: str | None = None


class ListPagesResponse(BaseModel):
    pages: list[str]
    next_cursor: str | None = None


class SearchResult(BaseModel):
    path: str


class SearchResponse(BaseModel):
    results: list[SearchResult]
    next_cursor: str | None = None


class RecentChange(BaseModel):
    path: str
    mtime_ns: int


class RecentChangesResponse(BaseModel):
    changes: list[RecentChange]
    next_cursor: str | None = None


class BlueprintInfo(BaseModel):
    name: str
    version: str
    description: str


class ListBlueprintsResponse(BaseModel):
    blueprints: list[BlueprintInfo]


class Instructions(BaseModel):
    prompt: str
    tool_whitelist: list[str]
    steps: list[str]
    model: str | None = None
