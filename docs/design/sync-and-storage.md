---
status: as-built design (v0.1.0-alpha), migrated from concepts/ and verified against the implementation.
---

# Sync and storage

The core principle: markdown files in the filesystem are the source of truth. The server
mediates access, enforces authentication, and applies WAL-based writes via the compile agent.

## Write model

Clients do not write pages directly. The only writeable surface exposed to clients is WAL
append (via `POST /v1/api/workspaces/{id}/logs` or the `append_log` MCP tool).

```
Client appends a knowledge fragment
  -> Append entry to <workspace>/logs/YYYY-MM-DD.md (per-workspace WAL)

Server-side compile (on demand or periodic)
  -> Compile agent reads WAL entries
  -> Produces a CompilePlan (list of page create/update operations)
  -> Coordinator applies the plan atomically (tmp + fsync + rename)
```

The compile agent reads WAL entries and existing pages via `read_page` and `search` tools.
It does not write pages directly; writing is the coordinator's responsibility after the agent
returns a `CompilePlan`.

## Read paths

### Local Obsidian

1. User runs `keenyspace pull` — fetches workspace content to `~/keenyspace/<slug>/`.
2. User opens the directory in Obsidian and reads locally.
3. User runs `keenyspace pull` again to refresh.

Local copy is read-only from the server's perspective. Any local Obsidian edits are
ephemeral and will be overwritten by the next pull.

### MCP / HTTP (agent or CLI)

1. Agent calls an MCP read tool, or CLI calls an HTTP read endpoint.
2. Server reads from disk and returns the current content.

Always returns the latest server-side version; no local copy is involved.

## WAL per workspace

Each workspace has its own WAL: daily-rotated files under `<workspace>/logs/YYYY-MM-DD.md`,
append-only.

Concurrency model:
- **Per-workspace isolation** — WAL files for different workspaces are independent; writes to
  one workspace do not block writes to another.
- **asyncio coordination** — append operations for a single workspace run under a
  per-workspace `asyncio.Lock`. Multiple concurrent appends are serialized without blocking
  the event loop.
- **`fcntl.flock` scaffold** — available for multi-worker deployments but disabled in v1
  (single-worker uvicorn). The scaffold exists behind the `multi_worker` config flag.

Filename derivation (timestamp) happens inside the lock to avoid rotation races.

## Atomic page write

Every server-side page write follows the pattern:

1. Write to `<page_dir>/tmp/<uuid>.md`.
2. `fsync(file)`.
3. `os.replace(tmp, final_path)`.
4. `fsync(parent_dir)`.

The `tmp/` directory is always in the same directory as the target page. Using `/tmp` or
a shared root `tmp/` would make `os.rename` a cross-filesystem operation (non-atomic on
Docker volumes).

## Conflict semantics

- **Pages**: only the compile agent writes pages. Multiple compile passes may update the same
  page; the latest result wins. The LLM naturally resolves semantic duplicates across WAL
  entries when it synthesizes a new page version.
- **WAL**: append-only under per-workspace lock. No conflicts, no lost writes.

There is no client-side conflict resolution UI in v1.

## Indexing

v1 has no full-text index, vector index, or embedding store. Navigation is via wikilink
traversal. Search (via `search_workspace` MCP tool or `keenyspace lint`) uses a filename +
content grep scan over the workspace directory tree.

Vector search and embeddings are out of scope for v1 (explicit non-goal). If wikilink
traversal proves insufficient, a search index can be added in a future version without
changing the WAL or page file formats.

## Backup and restore

Backup = FS root snapshot + Postgres dump. See [architecture.md](architecture.md) for the
full backup story. The `keenyspace backup` and `keenyspace restore` CLI commands provide the
operator interface.

## Retention

Daily WAL logs are archived after a configurable retention period. Audit log rotation is
server-side. These are background scheduler tasks, not client responsibilities.

## Multi-machine replication

Out of scope for v1. Single server, single FS root, single Postgres. v2 scope.
