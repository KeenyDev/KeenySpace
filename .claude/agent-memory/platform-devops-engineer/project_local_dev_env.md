---
name: project-local-dev-env
description: How to run KeenySpace tests and the server from source locally — dev Postgres compose, required KEENYSPACE_* vars, contended port 55432
metadata:
  type: project
---

Local development prerequisites, verified 2026-09-23 on this machine.

- `deploy/docker-compose.dev.yml` (compose project `keenyspace-dev`) brings up the Postgres
  the server suite needs: `postgres:17.2-alpine`, `127.0.0.1:55432:5432`, user/password/db
  `postgres`/`x`/`postgres`. Those exact values come from the fallback URL in
  `packages/server/tests/conftest.py` — change one and the other must change with it.
  The suite runs `DROP SCHEMA public CASCADE` between tests, so the data is disposable.
- **Port 55432 is contended on the dev laptop**: a container named `ks-style-pg` (another
  engineer's) binds it. Verification runs there need a temporary port override
  (`ports: !override` in a scratch compose file), not an edit to the committed file.
- Booting the server from source needs, with no defaults: `KEENYSPACE_DB__URL`, and under
  `KEENYSPACE_AUTH__`: `OIDC_ISSUER_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`,
  `OIDC_REDIRECT_URI`, `OIDC_POST_LOGOUT_REDIRECT_URI`, `SESSION_SECRET_KEY`,
  `API_KEY_PEPPER`. `KEENYSPACE_FS__ROOT` defaults to `/var/lib/keenyspace` (unwritable on a
  laptop) and `KEENYSPACE_METRICS_PORT` defaults to 9100, so both need overriding.
  `Settings` uses `extra="forbid"` — an unknown `KEENYSPACE_*` var is a hard startup failure.
- `keenyspace_server.main:app` builds the full app only when `KEENYSPACE_DB__URL` is set;
  otherwise the module exposes a health-only skeleton app. A "mysteriously featureless"
  server almost always means the DB URL was missing.
- The server pytest and alembic runs must start in `packages/server/` (alembic.ini is
  resolved relative to cwd).
- The root `.env.example` cannot boot the server (as of 2026-09-23): it omits every OIDC /
  session / pepper field and sets `KEENYSPACE_AUTH__DEV_TOKEN`, which `extra="forbid"`
  rejects. Do not point contributors at it.

**Why:** the repo was "welcoming to read, hostile to run" — none of this was documented
before the 2026-09-23 onboarding pass.
**How to apply:** CONTRIBUTING.md is the canonical public version of this; keep both in sync
with `.github/workflows/ci.yml`. See [[project-deploy-gotchas]] for the deployment stack.
