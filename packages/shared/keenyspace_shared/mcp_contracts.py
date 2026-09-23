"""Wire contract shared by the KeenySpace server, the CLI and MCP clients.

Every model here crosses a package boundary: the server produces them from its
REST endpoints and MCP tools, the `keenyspace` CLI and third-party MCP clients
consume them. Treat the field names and types as published API — renaming a
field or tightening a type breaks clients that were built against an older
server. Field descriptions on request models are surfaced to MCP clients as
the tool input schema, so they are part of what an agent reads.

Timestamps are timezone-aware UTC. Paths are always workspace-root-relative
POSIX paths ending in `.md`. List responses carry `next_cursor`: keep calling
with it until it comes back null.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AppendLogRequest(BaseModel):
    """Body of `POST /v1/api/workspaces/{slug}/logs`.

    The endpoint selects the workspace from the URL path.
    """

    workspace: str = Field(description="Slug of the workspace the entry belongs to.")
    content: str = Field(
        min_length=1,
        description=(
            "Self-contained knowledge fragment (a fact, a decision, a relationship). "
            "Compile turns accumulated entries into pages; it is never written to a "
            "page verbatim."
        ),
    )
    parent_id: str | None = Field(
        default=None,
        description="Entry id of an earlier append that this entry refines.",
    )


class AppendLogResponse(BaseModel):
    """Receipt for one appended write-ahead-log entry.

    The entry is durable but not yet a page: it becomes visible to `read_page`
    and `search_workspace` only after a compile pass.
    """

    entry_id: str
    ts: datetime


class ReadPageResponse(BaseModel):
    """One markdown page.

    `content` is the body below the frontmatter fence; `frontmatter` is the
    parsed fence, empty when the page has none.
    """

    path: str
    content: str
    frontmatter: dict[str, Any]


class WorkspaceInfo(BaseModel):
    """Registry metadata for one workspace.

    `status` is "active" or "archived"; `compile_state` is "idle", "running"
    or "paused". `blueprint_pin` is the blueprint the workspace was created
    from. `page_count` is counted from disk, `last_compile_at` is the end of
    the most recent successful compile run and is null until one succeeds.
    """

    uuid: str
    slug: str
    status: str
    blueprint_pin: str
    archived_at: datetime | None = None
    compile_state: str
    page_count: int
    last_compile_at: datetime | None = None


class ListWorkspacesResponse(BaseModel):
    """Workspaces visible to the caller, ordered by slug."""

    workspaces: list[WorkspaceInfo]
    next_cursor: str | None = None


class ListPagesResponse(BaseModel):
    """Workspace-relative page paths, ordered by path."""

    pages: list[str]
    next_cursor: str | None = None


class SearchResult(BaseModel):
    """One page matching a search query. Search returns paths, not snippets."""

    path: str


class SearchResponse(BaseModel):
    """Search matches, ordered by path."""

    results: list[SearchResult]
    next_cursor: str | None = None


class RecentChange(BaseModel):
    """One page with its filesystem modification time in nanoseconds."""

    path: str
    mtime_ns: int


class RecentChangesResponse(BaseModel):
    """Recently modified pages, ordered by mtime descending then path ascending."""

    changes: list[RecentChange]
    next_cursor: str | None = None


class BlueprintInfo(BaseModel):
    """A vault template offered by the server, read from its `blueprint.yaml`.

    `version` and `description` fall back to "unknown" and "" when the
    blueprint file omits them.
    """

    name: str
    version: str
    description: str


class ListBlueprintsResponse(BaseModel):
    """Blueprints available on this server, ordered by name."""

    blueprints: list[BlueprintInfo]


class Budgets(BaseModel):
    """Hard limits one agent run must stay within."""

    max_steps: int
    max_tokens: int
    max_seconds: int


class Instructions(BaseModel):
    """A server-defined command prompt, rendered for one workspace.

    `tool_whitelist` names the only tools the run may call, `steps` is the
    rendered step list, `model` is an optional model hint (null means the
    client picks), and `budgets` bounds the run.
    """

    prompt: str
    tool_whitelist: list[str]
    steps: list[str]
    model: str | None = None
    budgets: Budgets


class PostCompactInjection(BaseModel):
    """Context the client re-injects into an agent session after a compaction.

    `base_layer` is the always-present grounding text, `selected_pages` are the
    page paths chosen for this session, and `assembled_text` is what is handed
    back to the agent.
    """

    base_layer: str
    selected_pages: list[str]
    assembled_text: str


class BackupManifest(BaseModel):
    """`manifest.json`, the first entry of a backup archive.

    Restore reads it before touching anything: it compares `keenyspace_version`
    and `alembic_head` against the running server and refuses a mismatch unless
    forced. `workspaces` and `blueprints` are inventory summaries (`count` plus
    the uuids / names), and `pg_tables_dumped` lists the tables in `pg_dump.sql`.
    """

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
    """Error payload for a refused restore.

    `error` is a stable machine-readable code; `detail` carries the values
    that explain it (versions, alembic heads, counts).
    """

    error: str
    detail: dict[str, Any] = Field(default_factory=dict)


class WorkspaceImportResponse(BaseModel):
    """Identity of the workspace created by an import."""

    uuid: str
    slug: str
