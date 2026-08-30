# Using KeenySpace via MCP

## Why this matters to me (the model)

KeenySpace gives me a durable, shared memory that survives context compaction and
spans sessions: instead of re-deriving facts every time, I append knowledge once and
later read back a compiled, wikilink-connected knowledge graph. It is my long-term
notebook that humans and other agents read and write too, so what I record becomes
shared ground truth rather than throwaway chat history.

## How it's wired underneath

Plain markdown files in an Obsidian-compatible vault are the canon; Postgres only holds
the registry/ACL/audit, never page content. I never edit pages directly. My only write
surface is an append-only WAL (`append_log`); a server-side compile agent (pydantic-ai)
reads accumulated WAL entries and materializes them into pages via atomic writes. Reads
go straight off disk. Navigation is graph traversal over `[[wikilinks]]` — there is no
full-text or vector index in v1 by design. Every tool is scoped to one `workspace` slug.

---

Access to KeenySpace is only through the `keenyspace` MCP tools. Follow the model below
exactly.

## Mental model (do not violate)

- **Markdown is canon; pages are read-only to me.** There is NO tool to write a page.
- **The only writeable surface is the WAL via `append_log`.** I append knowledge
  fragments to the log; the server-side compile assembles pages from them.
- **compile is a separate step.** Pages do NOT appear immediately after `append_log`.
  `compile` materializes them; check progress with `compile_status`.
- **Every tool is scoped to one workspace.** Pass `workspace` (a slug, e.g.
  `keenyspace`) explicitly, unless the MCP connection URL pins one as
  `?workspace=<slug>` — then the argument is optional and the pin is the default.
  An explicit argument always wins. Pick the workspace first, then work.
- **A locally pulled vault (`keenyspace pull`) is ephemeral** — local edits are
  overwritten by the next pull. Do not treat it as source of truth or edit it by hand.

## Session start

1. `list_workspaces()` → pick the right one by `slug`.
2. `get_workspace_info(workspace)` → metadata, `blueprint_pin`, `compile_state`.
3. Pass that `workspace` into every subsequent call.

## Reading (always before writing)

- `search_workspace(workspace, query, limit?, cursor?)` — search by filename + content.
- `list_pages(workspace, prefix?, limit?, cursor?)` — survey pages (optionally by prefix).
- `read_page(workspace, path)` — full page text.
- `get_recent_changes(workspace, since?, cursor?, limit?)` — what changed (ISO timestamp
  or cursor). Sort: mtime DESC.
- All list tools are cursor-paginated: when `next_cursor != null`, keep reading.
- Navigate via `[[wikilinks]]` from index/MOC pages, NOT full-text grep (no index in v1).
  Back every claim with a `[[page-name]]` link.

## Writing knowledge

- `append_log(workspace, content, parent_id?)` — the only way to contribute.
- `content` is a self-contained, meaningful knowledge fragment (a fact / decision /
  relationship), not raw dialogue. Write it so the compile agent can turn it into a page.
- Do NOT try to write `raw/`, `logs/`, or pages directly — no such tool exists.

## Materializing into pages

1. `compile(workspace)` — fire-and-forget trigger for a compile pass.
2. `compile_status(workspace)` — poll state until done.
3. Only then are new/updated pages visible via `read_page` / `search_workspace`.

## Server-driven instructions (optional)

`get_instructions(workspace, command, context)` returns a server-side prompt for a
command. Supply the required context keys or you get `instructions_template_error`:

| command        | required context keys                |
|----------------|--------------------------------------|
| `query`        | `question` (opt. `max_pages`)        |
| `ingest`       | `source_path`                        |
| `post-compact` | `transcript_excerpt`                 |
| `lint`         | —                                    |

## What NOT to do

- Do not edit/create pages outside `append_log` + `compile`.
- Do not treat the local pulled vault as canon.
- Do not expect pages right after `append_log` without `compile`.
- Do not omit `workspace` — it is required on every call.
- Do not rely on full-text/vector search — there is none; traverse the wikilink graph.
