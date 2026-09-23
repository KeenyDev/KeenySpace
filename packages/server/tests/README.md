# Server test layout

Where a new test goes, in order — the first rule that matches wins:

1. **`auth/`** — the test is about authentication or authorization: OIDC discovery and
   token claims, JWKS handling, API keys, group gates, the composite auth backend. This
   package owns the whole auth vertical regardless of whether the test needs a database.
2. **`integration/`** — the test needs something outside the process under test: a
   Postgres connection (`pg_url`, `client`, `admin_client`, `anon_client`,
   `_engine_lifespan_ctx`, anything that seeds rows), a built FastAPI app or its lifespan
   (`app`, `build_app()`, an `ASGITransport` client, an in-process MCP client), a
   subprocess (`uv run alembic`, `uvicorn`), or a real TCP port.
3. **`unit/`** — everything else: pure functions, Pydantic models, routers inspected as
   objects, filesystem behaviour under `tmp_path`, hand-written fakes. No database, no
   app, no sockets, no subprocesses.
4. **`eval/`** — compile-agent evaluation fixtures and the judges that score them. Marked
   `eval` / `requires_anthropic`; fixture data lives in `eval/fixtures/`.

Nothing lives at `tests/` root except `conftest.py` (suite-wide infrastructure: env
defaults, `fs_root`, `pg_url`, `app`, the authenticated clients, API-key seeding, the mock
Authentik provider) and this file.

Fixtures stay as close to their users as possible: file-local first, then the package
`conftest.py` (`integration/conftest.py` holds the alembic-subprocess helpers,
`eval/conftest.py` the fixture roots), and only genuinely suite-wide setup in the root
`conftest.py`.

Running the suite needs a throwaway Postgres reachable via `KEENYSPACE_DB__URL`; the
alembic and schema-reset helpers `DROP SCHEMA public CASCADE`, so never point it at a
database you care about. Tests marked `real_idp` need a live Authentik via testcontainers
and are deselected by default (`addopts = -m 'not real_idp'`).
