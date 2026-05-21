---
phase: 04-workspace-lifecycle-blueprints
plan: "08"
subsystem: workspace-import
tags: [workspace, import, zip, security, audit, metrics, roundtrip]
dependency_graph:
  requires: ["04-01", "04-07"]
  provides: ["ws/import_.py", "api/workspace_import.py (stub replaced)"]
  affects: ["POST /v1/api/workspaces/import"]
tech_stack:
  added: []
  patterns:
    - "asyncio.to_thread zip validation + extraction"
    - "same-volume tmp + os.rename atomic import (Pitfall #8 mitigation)"
    - "posixpath.normpath + stat.S_ISLNK zip safety (RESEARCH Pattern 7)"
    - "200MB uncompressed size cap before extraction (zip-bomb guard)"
    - "audit-before-commit pattern"
key_files:
  created:
    - packages/server/keenyspace_server/ws/import_.py
    - packages/server/tests/test_ws_import.py
    - packages/server/tests/integration/test_export_import_roundtrip.py
  modified:
    - packages/server/keenyspace_server/api/workspace_import.py
    - packages/shared/keenyspace_shared/mcp_contracts.py
decisions:
  - "Zip entries extracted one-by-one (not extractall) with per-entry safety checks"
  - "Upload tmp file (.upload_tmp_*.zip) placed in same workspaces/ dir as import_tmp for same-volume guarantee"
  - "validate_import_zip double-opens zip: first pass for safety + blueprint_ref; second pass (unpack) only after validation passes"
  - "outcome variable tracks metric label atomically via finally block"
metrics:
  duration: "12 minutes"
  completed_at: "2026-05-21T13:30:00Z"
  tasks_completed: 2
  files_changed: 5
---

# Phase 4 Plan 08: Workspace Import Summary

WS-06 (import half) implemented with full zip security hardening and atomic same-volume restore. POST /import replaces the Plan 01 stub and forms the export->import roundtrip integration test required by ROADMAP Phase 4 success criterion #4. WorkspaceImportResponse added to shared contracts.

## Tasks Completed

| Task | Name | Commit | Key Files |
|------|------|--------|-----------|
| 1 | ws/import_.py service + WorkspaceImportResponse contract | 8e00f41 | keenyspace_server/ws/import_.py, keenyspace_shared/mcp_contracts.py, tests/test_ws_import.py |
| 2 | POST /import endpoint + roundtrip integration test | f0ae304 | keenyspace_server/api/workspace_import.py, tests/integration/test_export_import_roundtrip.py |

TDD commits:
- c195ba9 — test(04-08): RED for ws/import_.py unit tests
- 8e00f41 — feat(04-08): GREEN ws/import_.py service + WorkspaceImportResponse contract
- 448f55e — test(04-08): RED for export->import roundtrip integration tests
- f0ae304 — feat(04-08): GREEN POST /import endpoint replacing Plan 01 stub

## What Was Built

### ws/import_.py

- `MAX_IMPORT_UNCOMPRESSED_BYTES = 200 * 1024 * 1024` — zip-bomb guard enforced BEFORE extraction
- `WorkspaceImportError(code, message)` — sentinel for all validation failures
- `WorkspaceSlugConflictError(slug)` — sentinel for 409 path
- `_validate_zip_sync(zip_path)` — synchronous zip validation (for asyncio.to_thread):
  - `bad_zip`: BadZipFile caught at open
  - `path_traversal`: posixpath.normpath detects `..` segments and absolute paths
  - `symlink`: stat.S_ISLNK(info.external_attr >> 16) detects symlink entries
  - `size_cap`: running total > MAX_IMPORT_UNCOMPRESSED_BYTES during infolist walk
  - `empty_workspace`: no .md file entries after full walk
  - Reads `.keenyspace/config.yaml` from zip to extract `preserved_blueprint_ref`
- `validate_import_zip(zip_path)` — async wrapper via asyncio.to_thread
- `_unpack_zip_sync(zip_path, dest)` — defence-in-depth: also skips symlinks on extraction
- `import_workspace(session, *, settings, slug, zip_path, actor_sub)`:
  - Slug regex validation (reuses same _SLUG_RE as create endpoint)
  - Pre-check slug uniqueness -> WorkspaceSlugConflictError
  - validate_import_zip -> WorkspaceImportError on any failure
  - New UUID always generated; source UUID from zip is discarded (D-07)
  - import_tmp = `<fs_root>/workspaces/.import_tmp_<rand>` (same-volume, Pitfall #8)
  - _write_workspace_config overwrites config.yaml with new uuid+slug+blueprint_ref
  - DB INSERT Workspace row (status='active', compile_state='idle')
  - write_audit(workspace.imported) before session.commit()
  - IntegrityError -> rollback + rmtree(import_tmp) + WorkspaceSlugConflictError
  - os.rename(import_tmp, final_dir) — atomic same-volume rename after commit
  - shutil.rmtree(import_tmp) cleanup on all failure paths via finally
  - WORKSPACE_IMPORT_TOTAL metric incremented once per request

### api/workspace_import.py

- Replaces Plan 01 stub completely
- `POST /v1/api/workspaces/import` — multipart (file=UploadFile, slug=Form)
- Streams upload in 64KB chunks to same-volume `.upload_tmp_*.zip`
- WorkspaceImportError -> HTTP 422 with `{code, message}` detail
- WorkspaceSlugConflictError -> HTTP 409 with `{code: "workspace_slug_conflict", slug}`
- Always unlinks upload_tmp in finally block
- HTTP 201 + WorkspaceImportResponse on success

### mcp_contracts.py

- `WorkspaceImportResponse(uuid: str, slug: str)` appended

## Test Coverage

- 9 unit tests in `tests/test_ws_import.py` — all pass
- 7 integration tests in `tests/integration/test_export_import_roundtrip.py` — skip gracefully when postgres unavailable (standard CI pattern); pass with real Postgres

### Unit test coverage
- path traversal (relative `..` and absolute paths)
- symlink entry rejection
- no .md files rejection
- bad zip rejection
- size cap guard
- blueprint_ref extraction from in-zip config.yaml
- None blueprint_ref when config.yaml absent
- async wrapper passes through

### Integration test coverage
- Full export->import roundtrip with page content equality (D-08 lossless)
- path_traversal -> 422
- empty_workspace -> 422
- bad_zip -> 422
- slug_conflict -> 409
- new uuid assigned ignoring source uuid; blueprint_ref preserved
- unauthenticated -> 401 (AUTH-08 regression)

## Deviations from Plan

None — plan executed exactly as written.

## Threat Model Coverage

All threats from the plan's threat_model are mitigated:

| Threat ID | Mitigation |
|-----------|------------|
| T-4-13 | posixpath.normpath + `..` check + posixpath.isabs reject; pinned by unit tests + integration |
| T-4-14 | stat.S_ISLNK check in validate; defence-in-depth in _unpack_zip_sync |
| T-4-15 | MAX_IMPORT_UNCOMPRESSED_BYTES cap enforced during infolist walk before extraction |
| T-4-16 | Pre-check slug uniqueness; IntegrityError secondary defence; cleanup in finally |
| T-4-17 | import_tmp in workspaces/ (same-volume as final dir); os.rename always same-volume |
| T-4-18 | Router under protected_deps in main.py; AuthMiddleware enforces 401; test pins |
| T-4-19 | write_audit(workspace.imported) before session.commit(); roundtrip test asserts row |
| T-4-20 | ZipFile open in try/except; BadZipFile -> WorkspaceImportError("bad_zip"); finally closes |
| T-4-21 | blueprint_ref treated as informational string only; never used to load code (D-05) |

## Known Stubs

None.

## Threat Flags

None — no new security surface beyond what is modeled in the plan's threat_model.

## Self-Check: PASSED

- `packages/server/keenyspace_server/ws/import_.py` — exists
- `packages/server/keenyspace_server/api/workspace_import.py` — exists, contains `@router.post`
- `packages/shared/keenyspace_shared/mcp_contracts.py` — contains `WorkspaceImportResponse`
- `packages/server/tests/test_ws_import.py` — exists, 9 tests pass
- `packages/server/tests/integration/test_export_import_roundtrip.py` — exists, 7 tests (skip without DB)
- Commits: c195ba9, 8e00f41, 448f55e, f0ae304 — all present in git log
