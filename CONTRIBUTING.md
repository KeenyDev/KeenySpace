# Contributing to KeenySpace

KeenySpace is released under the GNU Affero General Public License v3.0 (AGPL-3.0).
Contributions are welcome.

## Developer Certificate of Origin (DCO)

All commits must include a `Signed-off-by` trailer. This certifies that you wrote the
contribution or have the right to submit it under the project license.

To sign off, use:

```
git commit -s -m "your commit message"
```

This appends a trailer to your commit:

```
Signed-off-by: Your Name <your@email.com>
```

The DCO bot enforces this requirement on all pull requests. External contributors
must sign off every commit in a PR. Repository members are exempt per `.github/dco.yml`
(`require: members: false`), but are encouraged to sign off anyway.

Full DCO text: https://developercertificate.org/

## Scope and non-goals

v1 is deliberately narrow. The items below are decided, not open questions — a PR that
implements one will be declined on scope, however good the code is. Open an issue first
if you think one should move.

Deferred to a later version:

- 8 Tier-2 MCP tools — v1.1. The v1 surface is exactly 11 Tier-1 tools:
  `list_workspaces`, `get_workspace_info`, `read_page`, `list_pages`, `search_workspace`,
  `append_log`, `get_instructions`, `list_blueprints`, `get_recent_changes`, `compile`,
  `compile_status`. Deferred: `get_backlinks`, `find_orphans`, `find_broken_links`,
  `frontmatter_search`, `read_section`, `update_section`, `lint_workspace`.
- Helm chart — v1.1. v1 deploys via docker-compose only.
- OpenTelemetry baseline — v1.1.
- Admin web UI — v1.5. Administration is CLI and API only in v1.
- Multi-tenant authorization surface (UI/CLI) — v1.5. v1 has the storage abstraction but
  stays single-org.
- Multi-worker uvicorn — v1.5+. v1 is single-worker by design; multi-worker needs
  `flock`, scheduler dedupe, and compile-agent isolation first.

Permanent non-goals:

- Hosted SaaS. KeenySpace is self-hosted only.
- Vector search and embedding-based retrieval. v1 navigation is `[[wikilink]]` traversal;
  there is no full-text or vector index, by design.
- Real-time collaborative cursors or CRDT machinery.
- A Notion-style block editor. Markdown files are the canon and the edit surface.
- Auto-updating clients.
- LangChain. The compile pipeline uses pydantic-ai + instructor.

Architectural rules that constrain most PRs:

- Markdown files are canon. Postgres holds the workspace registry, users, sessions, ACL
  references, audit, and compile cursors — never page content.
- The WAL is the only writeable surface for clients. Pages are produced by the server-side
  compile pass; no client writes a page directly.
- Async only: SQLAlchemy 2.0 async + asyncpg. Sync SQLAlchemy and `psycopg2` are out.
- Schema changes go through Alembic. No raw DDL, no model auto-create on boot.
- `structlog` to stdout for diagnostics, never `print`.

See [docs/design/](docs/design/) for the as-built design documentation.

## Development setup

### Prerequisites

- [uv](https://docs.astral.sh/uv/) — CI pins 0.10.8. It provisions the Python 3.14
  toolchain the workspace needs; no system Python setup is required.
- Docker with Compose v2 — the server test suite needs a real Postgres 17.
- Nothing else. The test suite needs no secrets, no LLM API key, and no OIDC provider.

Install the workspace exactly as CI does:

```sh
uv sync --all-packages --all-extras
```

CI runs the same command with `--locked`, which fails if `uv.lock` is out of date with the
`pyproject.toml` files. Run `uv sync --locked --all-packages --all-extras` before pushing if
you changed dependencies.

### Test database (required for the server suite)

`packages/server/tests/conftest.py` reads `KEENYSPACE_DB__URL` and falls back to
`postgresql+asyncpg://postgres:x@localhost:55432/postgres`. With nothing listening there,
every database-backed test errors out. `deploy/docker-compose.dev.yml` starts exactly that
database — no secrets or `.env` file needed:

```sh
docker compose -f deploy/docker-compose.dev.yml up -d
docker compose -f deploy/docker-compose.dev.yml ps    # wait for "healthy"
```

The suite runs `DROP SCHEMA public CASCADE` between tests, so this database is disposable
by design. It binds to 127.0.0.1 only and is separate from the deployment stack in
`deploy/docker-compose.yml` (different compose project, different volume, port 55432
instead of 5432), so both can run at once.

Stop it when you are done:

```sh
docker compose -f deploy/docker-compose.dev.yml down      # add -v to drop the data too
```

The client test suite needs no database.

### The checks CI runs

These are the four commands from `.github/workflows/ci.yml`. All must pass before a PR is
mergeable; run them locally to reproduce CI:

```sh
uv run ruff check .

uv run mypy --strict \
  packages/server/keenyspace_server \
  packages/client/keenyspace \
  packages/shared/keenyspace_shared

cd packages/server && uv run --package keenyspace-server pytest tests/ -x -q -m "not real_idp"

uv run --package keenyspace pytest packages/client/tests/ -x -q
```

Notes:

- The server suite runs from `packages/server/` because `alembic.ini` is resolved relative
  to the working directory.
- `-m "not real_idp"` deselects the tests that need a live Authentik. It is already the
  default via `addopts` in `packages/server/pyproject.toml`; CI states it explicitly. See
  `deploy/docker-compose.authentik-test.yml` if you need that lane.
- `mypy` is scoped to the three source packages, not `packages/` as a whole — test files
  are not type-checked under `--strict`.

### Running the server from source

`deploy/docker-compose.yml` is the deployment path. For iterating on the server itself,
run it under uvicorn against the dev database above.

Settings come from `KEENYSPACE_*` environment variables
(`packages/server/keenyspace_server/config.py`). The model forbids unknown
`KEENYSPACE_*` variables and fails at startup naming any required field that is missing.
These have no defaults and must be set:

| Variable | Notes |
|----------|-------|
| `KEENYSPACE_DB__URL` | Also decides what the app is: without it, `keenyspace_server.main:app` falls back to a health-only skeleton app. |
| `KEENYSPACE_AUTH__OIDC_ISSUER_URL` | |
| `KEENYSPACE_AUTH__OIDC_CLIENT_ID` | |
| `KEENYSPACE_AUTH__OIDC_CLIENT_SECRET` | |
| `KEENYSPACE_AUTH__OIDC_REDIRECT_URI` | |
| `KEENYSPACE_AUTH__OIDC_POST_LOGOUT_REDIRECT_URI` | |
| `KEENYSPACE_AUTH__SESSION_SECRET_KEY` | Warns below 32 bytes or when it contains `replace-me`. |
| `KEENYSPACE_AUTH__API_KEY_PEPPER` | Same check. |

`KEENYSPACE_FS__ROOT` has a default of `/var/lib/keenyspace`, which is not writable on a
development machine — set it to a scratch directory. Set `KEENYSPACE_METRICS_PORT=0`
unless port 9100 is free and you want the Prometheus listener.

```sh
export KEENYSPACE_DB__URL=postgresql+asyncpg://postgres:x@localhost:55432/postgres
export KEENYSPACE_FS__ROOT=/tmp/keenyspace-dev
export KEENYSPACE_AUTH__OIDC_ISSUER_URL=http://localhost:9000/application/o/keenyspace/
export KEENYSPACE_AUTH__OIDC_CLIENT_ID=keenyspace-cli
export KEENYSPACE_AUTH__OIDC_CLIENT_SECRET=dev-only-placeholder-not-a-secret
export KEENYSPACE_AUTH__OIDC_REDIRECT_URI=http://localhost:8001/v1/api/auth/callback
export KEENYSPACE_AUTH__OIDC_POST_LOGOUT_REDIRECT_URI=http://localhost:8001/
export KEENYSPACE_AUTH__SESSION_SECRET_KEY='dev-only-session-secret-32chars!'
export KEENYSPACE_AUTH__API_KEY_PEPPER='dev-only-api-key-pepper-32chars!'
export KEENYSPACE_AUTH__COOKIE_SECURE=false
export KEENYSPACE_METRICS_PORT=0
mkdir -p "$KEENYSPACE_FS__ROOT"

cd packages/server
uv run --package keenyspace-server alembic upgrade head
uv run --package keenyspace-server uvicorn keenyspace_server.main:app --reload --port 8001
```

Verify it came up:

```sh
curl -s http://127.0.0.1:8001/healthz     # {"status":"ok"}
```

The OIDC values above are placeholders. The process boots and serves `/healthz`,
`/readyz`, and the MCP mount at `/v1/mcp`, but any login flow needs a real identity
provider — see [docs/oidc-authentik-setup.md](docs/oidc-authentik-setup.md).

## Pull request checklist

- [ ] The change is in scope (see "Scope and non-goals" above)
- [ ] `uv run ruff check .` reports no issues
- [ ] `uv run mypy --strict` over the three source packages reports no issues
- [ ] Server tests pass against the dev Postgres, client tests pass
- [ ] Every commit is signed off with `git commit -s`
- [ ] Commit messages are concise and describe the change, not the implementation
