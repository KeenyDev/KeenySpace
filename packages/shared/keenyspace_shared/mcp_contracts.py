from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AppendLogRequest(BaseModel):
    workspace: str
    content: str = Field(min_length=1)
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


class Budgets(BaseModel):
    max_steps: int
    max_tokens: int
    max_seconds: int


class Instructions(BaseModel):
    prompt: str
    tool_whitelist: list[str]
    steps: list[str]
    model: str | None = None
    budgets: Budgets


class PostCompactInjection(BaseModel):
    base_layer: str
    selected_pages: list[str]
    assembled_text: str


class BackupManifest(BaseModel):
    version: int
    keenyspace_version: str
    schema_version: int
    alembic_head: str
    created_at: datetime
    created_by: str
    fs_root_size_bytes: int
    workspaces: dict[str, Any]
    blueprints: dict[str, Any]
    pg_tables_dumped: list[str]


class RestoreError(BaseModel):
    error: str
    detail: dict[str, Any] = Field(default_factory=dict)


class WorkspaceImportResponse(BaseModel):
    uuid: str
    slug: str
