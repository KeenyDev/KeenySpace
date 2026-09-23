---
name: psql-meta-command-quirks
description: Verified psql 17 behaviours that shape the admin restore dump-safety scanner (api/admin.py) and a test-fixture gotcha for workspace creation
metadata:
  type: project
---

Verified empirically against psql 17.4 (2026-09-22) while hardening POST /v1/admin/restore:

- psql runs a backslash meta-command ANYWHERE on a line outside quotes: `SELECT 1; \! id` executes the shell. A "line starts with backslash" filter is not enough.
- Harmless-looking meta-commands expand backtick arguments via the shell: `\t \`cmd\``, `\f \`cmd\``, `\bind \`cmd\`` all run cmd. So allowlisting "safe" escapes like `\t`/`\N` outside COPY data is unsafe; the scanner must know exactly when psql is in COPY-in mode.
- `\\` (empty command name) and unknown commands are errors; with `-v ON_ERROR_STOP=1` psql exits 3 before reading further lines. A failing `COPY ... FROM stdin;` also stops before data lines are read.
- Local Homebrew psql is 17.4 (no `\restrict` support); deploy image and CI install latest PGDG postgresql-client-17 (17.6+, dumps carry `\restrict KEY` / `\unrestrict KEY`).

**Why:** the restore endpoint pipes an uploaded dump into psql; these facts decided the conservative scanner design (reject `$`, psql variables, `begin`, NUL; enter COPY mode only from a complete `COPY ... FROM stdin;` line at statement start).

**How to apply:** any change to the scanner or to psql invocation must keep ON_ERROR_STOP, --single-transaction and --no-psqlrc, and must fail closed.

Test gotcha: the `client` fixture in tests/conftest.py runs only engine_lifespan, so fs_root/blueprints is empty; tests that create workspaces must seed via `ensure_fs_root_layout(fs_root, <repo>/blueprints)` (repo-root `blueprints/`, not packages/server/blueprints).
