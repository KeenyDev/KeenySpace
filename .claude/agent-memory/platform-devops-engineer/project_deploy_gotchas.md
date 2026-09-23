---
name: project-deploy-gotchas
description: Non-obvious KeenySpace deploy constraints (image editable install, no Redis, metrics port, fail-closed secrets, test-lane env file)
metadata:
  type: project
---

- Server image must keep workspace members editable (/app/packages layout): db/session.py resolves alembic.ini and main.py the default blueprints dir relative to the source tree. `uv sync --no-editable` breaks both until alembic is packaged inside keenyspace_server (checked 2026-09-22).
- Authentik 2025.10+ does not use Redis; the service was removed from compose 2026-09-22 (upgraded installs keep an orphan container/volume).
- deploy/.env is interpolation-only; app overrides go in deploy/keenyspace.env (env_file). CI reuses the prebuilt image via KEENYSPACE_IMAGE.
- Prometheus metrics live on an internal listener (KEENYSPACE_METRICS_PORT, default 9100, 0 disables), never on the API port; observability.yml must run in the same compose project to reach keenyspace:9100.
- Compose secrets are `${VAR:?...}` (fail closed); the real_idp testcontainers lane interpolates from committed deploy/authentik-test.env.

**Why:** these were decided during the 2026-09-22 deploy hardening pass and are not obvious from any single file.
**How to apply:** check these before changing the Dockerfile install mode, removing services, or touching metrics/observability wiring.
