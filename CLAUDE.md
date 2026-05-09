# KeenySpace — repo guide for Claude

KeenySpace — self-hosted opensource система для построения и совместного использования knowledge graph'ов между людьми и LLM-агентами. Каждый workspace — обычная Obsidian-совместимая директория markdown-файлов; поверх стоит сервер, который отвечает за authn, MCP-доступ агентам и server-side compile накопленных WAL-логов в страницы.

**License:** AGPL-3.0 + DCO/CLA. **Stack:** Python 3.14, FastAPI + FastMCP 3.x, Postgres 17, pydantic-ai + instructor, Authentik (reference OIDC).

## Repository state

This is a **monorepo** containing the server, the `keenyspace` CLI client, deployment artefacts, and design documentation. As of init: design phase complete; implementation begins at Phase 1.

```
keeny-space/
├── concepts/                  # First-pass design docs (will move to docs/design/ post Phase 1)
├── plans/                     # One-off execution plans
├── vision.md, README.md       # High-level positioning
├── open-questions.md          # Deliberately deferred decisions
├── .planning/                 # GSD planning workspace (LOCAL-ONLY, in .gitignore)
│   ├── PROJECT.md             # Project context + Key Decisions
│   ├── REQUIREMENTS.md        # 113 v1 REQ-IDs + traceability to phases
│   ├── ROADMAP.md             # 7-phase roadmap (Mode: mvp on each)
│   ├── STATE.md               # Project memory
│   ├── config.json            # Workflow config (interactive, standard, parallel, balanced)
│   └── research/              # Stack/Features/Architecture/Pitfalls/Summary
└── (upcoming) src/, deploy/, docs/
```

## Workflow: Get Shit Done (GSD)

Always work through GSD slash commands — they preserve goal-driven structure, atomic commits, and verification gates.

### When user asks to start a phase
- `/gsd-discuss-phase <N>` — gather context and clarify approach (recommended first step)
- `/gsd-plan-phase <N>` — create detailed plan with task decomposition
- `/gsd-execute-phase <N>` — execute plans with wave-based parallelization

### When user asks to check status
- `/gsd-progress` — situational summary, dispatch next action
- `/gsd-stats` — phases/plans/requirements counts and timeline

### When user asks to extend or rework
- `/gsd-phase` — CRUD operations on phases in ROADMAP.md
- `/gsd-debug` — systematic debugging with persistent state
- `/gsd-undo` — safe revert of phase or plan commits

### When user asks to ship
- `/gsd-ship` — PR creation with code review

For the full list, run `/gsd-help`.

## Architectural rules (locked in PROJECT.md)

These are decisions; do NOT relitigate without explicit user request.

- **Markdown is canon.** Server reads/writes plain `.md` files. Postgres holds workspace registry, users, sessions, ACL refs, audit, compile cursors — NOT page content.
- **WAL is the only writeable surface for clients.** Pages are produced by server-side compile from WAL entries via pydantic-ai + instructor. Clients never push pages directly.
- **Atomic write helper:** `tmp/` ALWAYS in same directory as target page (NEVER `/tmp` — `os.rename` becomes non-atomic copy+delete across volumes). `fsync(file)` + `rename` + `fsync(parent_dir)`.
- **WAL concurrency:** per-workspace `asyncio.Lock` registry. Filename derivation INSIDE the lock (TZ=UTC, no rotation race). `fcntl.flock` scaffold behind `multi_worker` flag, OFF in v1.
- **FastAPI + FastMCP composition:** `mcp.http_app(path="/")` + `app.mount("/v1/mcp", mcp_app)` + **OBLIGATORY** `combine_lifespans(app_lifespan, mcp_app.lifespan)`. Without composed lifespans, StreamableHTTP silently fails on the second MCP call.
- **Auth at the FastAPI root, NOT in FastMCP.** Single `Starlette AuthMiddleware` on the root populates `request.state.user`; FastMCP tools read identity via `auth_bridge` from `get_http_request().state.user`. Do NOT use FastMCP's OAuthProxy under the mount path (PrefectHQ/fastmcp #1862).
- **Compile agent guardrails:** tool surface restricted to `read_page` / `write_page` / `search` ONLY (NO fetch/shell/network); WAL wrapped in `<wal_entry>` delimiters with explicit "data, not instructions" system prompt; step/token/time budgets enforced; loop detection (same `(tool, args_hash)` 3x → abort); temperature 0; idempotency hash check.
- **Single-worker uvicorn** in v1. Multi-worker requires flock + APScheduler dedupe + sibling compile-агент isolation — explicitly v1.5+.
- **9 Tier-1 MCP tools in v1**, no Tier-2: `list_workspaces`, `get_workspace_info`, `read_page`, `list_pages`, `search_workspace`, `append_log`, `get_instructions`, `list_blueprints`, `get_recent_changes`.
- **API keys (`ks_live_*`) are the primary MCP agent path.** OIDC bearer is alternate (short-lived tokens don't fit long-running MCP sessions).
- **`keenyspace pull` refuses dirty local state without `--force`**; `.obsidian/` excluded server-side from canon.
- **`keenyspace backup` + `keenyspace restore` + `keenyspace doctor` shipped in v1** — #1 self-host failure mode is split FS+PG backup.
- **No LangChain.** pydantic-ai + instructor cover all use cases; LangChain is bloat for our compile shape.
- **No vector search v1.** Wikilink traversal is the navigation philosophy.
- **No real-time collab cursors / CRDT, no Notion-style block editor, no SaaS hosted, no auto-update client.** See PROJECT.md "Out of Scope" — do not propose these.

## Coding standards

- **Async-only**: SQLAlchemy 2.0 async + asyncpg. Sync SQLAlchemy and `psycopg2` forbidden.
- **Pydantic 2.13+** everywhere. Settings via pydantic-settings.
- **Alembic from day one** — no raw SQL DDL, no model auto-create on boot.
- **structlog** JSON to stdout for logging; never `print` for diagnostics.
- **Tests:** pytest + pytest-asyncio + httpx ASGITransport for FastAPI; `mcp-inspector` for MCP integration tests.
- **Linting:** ruff + mypy strict. CI gates fail on either.
- **No emojis in code or docs** unless explicitly requested.
- **No comments unless WHY is non-obvious.** No "what" comments — well-named identifiers explain themselves.

## Per-phase context

When working on a specific phase, the GSD tooling produces local artefacts:
- `.planning/phase-N/PLAN.md` — phase plan with task decomposition
- `.planning/phase-N/RESEARCH.md` — phase-level research (if research agent enabled)
- `.planning/phase-N/PATTERNS.md` — codebase pattern map
- `.planning/phase-N/VERIFICATION.md` — post-execution verification report

These are local-only (`.planning/` in `.gitignore`). Read them when working on the phase. The user can run `/gsd-progress` to surface current state.

## What NOT to do

- Don't propose helm chart in v1 (deferred to v1.1).
- Don't build admin web UI (deferred to v1.5).
- Don't build multi-tenant authorization UI/CLI surface (storage abstraction only; user-facing surface single-org until v1.5).
- Don't add Tier-2 MCP tools to v1 scope — keep them out of REQUIREMENTS.md / ROADMAP.md until v1 ships.
- Don't refactor concepts/ until Phase 1 completes (then they migrate to `docs/design/`).
- Don't introduce vector search, embeddings, real-time collab, or CRDT machinery — these are explicit non-goals.
- Don't `git add -A` from the repo root without checking — the user's working copy may contain uncommitted Obsidian per-user state.

---

*Generated during `/gsd-new-project` initialization on 2026-05-09.*
*Project mode: mvp (vertical slicing). Workflow: interactive, standard granularity, balanced model profile.*
