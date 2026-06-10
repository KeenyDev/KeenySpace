---
status: as-built design (v0.1.0-alpha), migrated from concepts/ and verified against the implementation.
---

# Workspace model

A workspace is the primary abstraction in KeenySpace. One workspace equals one Obsidian vault
equals one directory on the filesystem.

## Analogy with Postgres

| Postgres | KeenySpace |
|----------|------------|
| `database` | workspace |
| `template1` | blueprint |
| `CREATE DATABASE foo TEMPLATE template1` | `keenyspace workspace create --blueprint default --name foo` |

## Filesystem layout (server-side)

```
<server-data-root>/
  workspaces/
    <ws-uuid>/                    <- live workspace = Obsidian vault
      .keenyspace/                <- workspace metadata (not synced to Obsidian)
        config.yaml               <- name, slug, blueprint ref, schema version
        instructions/             <- server-driven prompts per command
      .obsidian/                  <- Obsidian config (excluded from server canon)
      CLAUDE.md                   <- workspace schema contract for agents
      _templates/                 <- markdown templates for new pages
      logs/                       <- WAL daily log files (YYYY-MM-DD.md)
      <category>/                 <- e.g. concepts/, services/, decisions/

  blueprints/
    default/                      <- same layout, admin-only write access
      .keenyspace/
        blueprint.yaml            <- version, description
      CLAUDE.md
      _templates/
```

The workspace registry (UUID to slug mapping, blueprint ref, status) lives in Postgres, not
in workspace files.

Key properties:
- **UUID** — stable on-disk identity. Renaming the human-readable slug does not change the UUID.
- **`.keenyspace/`** — private service directory; not shown in Obsidian by default.
- **`.obsidian/`** — excluded server-side from the canon. Per-user Obsidian config is local only.
- **`blueprints/`** — parallel directory to workspaces. Readable by all users via `list_blueprints`; writeable by admins only.

## Blueprint

A blueprint is a workspace template used for cloning. Admins control blueprint content; users
clone blueprints to create new workspaces.

Each blueprint contains:
- `blueprint.yaml` — version and description.
- `CLAUDE.md` — workspace schema contract (categories, conventions for agents).
- `_templates/` — starter pages for new content.
- Optional initial content (e.g. an `index.md` skeleton).

### Cloning

```sh
keenyspace workspace create --blueprint default --name "platform-research"
```

Server-side process:
1. Verify caller has CREATE workspace permission.
2. Copy `blueprints/default/` to `workspaces/<new-uuid>/`.
3. Write `<new-uuid>/.keenyspace/config.yaml` with `blueprint: default@v1.x`.
4. Register workspace in Postgres.

Blueprint upgrade flow (merging new blueprint version into an existing workspace) is out of
scope for v1. v1 ships clone-on-create only.

## Obsidian compatibility

- **`.obsidian/` is excluded** from server canon and sync. Each user's Obsidian config is local.
- **Wikilinks** (`[[page-name]]`) are plain text from the server's perspective. Obsidian resolves them locally. `keenyspace lint` checks that wikilink targets exist.
- **Attachments** live under `raw/assets/` by default (configurable via `.keenyspace/config.yaml`).

## Workspace identity

| Layer | Identifier |
|-------|-----------|
| FS storage | UUID (`019234ab-...`) |
| Server API path | UUID or slug (slug resolves to UUID via Postgres lookup) |
| Client UX | slug (`platform-research`) |

Slug: lowercase kebab-case, unique within deployment, mutable. UUID: immutable.

## Workspace metadata (`.keenyspace/config.yaml`)

```yaml
uuid: 019234ab-...
slug: platform-research
display_name: Platform Research
blueprint: default@v1.3
created_at: 2026-05-05T10:00:00Z
schema_version: 1
```

## Lifecycle

1. **Create** — clone from blueprint (admin or authorized user).
2. **Populate** — client ingests content; agents append to WAL via MCP.
3. **Read/edit** — Obsidian locally after `keenyspace pull`, or via client commands, or via MCP.
4. **Archive** — flag in registry; becomes read-only, excluded from default listings.
5. **Delete** — admin-only; soft delete (move to `archived/`) then hard delete after configurable retention period.

Workspaces are flat (no nested workspaces) in v1.
