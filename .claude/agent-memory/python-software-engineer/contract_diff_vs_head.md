---
name: contract-diff-vs-head
description: How to prove a refactor is wire-contract-preserving in this uv workspace — PYTHONPATH-override a HEAD worktree against the repo .venv and diff the route/OpenAPI dump
metadata:
  type: reference
---

To prove a behavior-preserving refactor did not move the HTTP contract, build the app
twice and diff the route table + `app.openapi()`:

1. `git worktree add --detach <scratch>/head-baseline HEAD`
2. Run a dump script with the repo's own `.venv/bin/python`, prefixing
   `PYTHONPATH=<scratch>/head-baseline/packages/server:<scratch>/head-baseline/packages/shared`.
   PYTHONPATH wins over the workspace's editable installs, so the same interpreter and
   dependency set executes the OLD sources — no second venv needed.
3. Diff the two JSON dumps.

**Why:** the editable installs point at the live checkout, so "run the old code" otherwise
means a full second environment. Verified working on this workspace layout.

**How to apply:** `build_app()` needs the same env the test conftest sets
(`KEENYSPACE_FS__ROOT`, all `KEENYSPACE_AUTH__OIDC_*`, `API_KEY_PEPPER`,
`SESSION_SECRET_KEY`, `KEENYSPACE_METRICS_PORT=0`) or Settings validation fails; filter the
`config.secret.too_short` stdout log line out of both dumps before diffing (it carries a
timestamp). MCP wire tool names are NOT in that dump — they are pinned by the
`tests/test_mcp_*.py` in-memory-client tests instead. See [[test-lifespan-in-fixture]].
