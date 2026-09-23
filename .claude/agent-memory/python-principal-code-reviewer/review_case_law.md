---
name: review-case-law
description: Recurring defect patterns found in KeenySpace full-repo reviews (server/client/shared) - check these first on any new review
metadata:
  type: project
---

Recurring patterns seen in the 2026-09-22 full-repo review (verify against current code before citing):

- Sync file I/O inside `async def` handlers is offloaded inconsistently: some paths use `asyncio.to_thread`, siblings (read_page, pages-raw, admin backup/restore, workspace create copytree) do not. Single-worker uvicorn makes any of these a whole-server stall.
- WAL framing/parser contract is regex-based (`<wal_entry ...>(.+?)</wal_entry>`); empty content breaks it. Any change to framing/parser/append validation needs a round-trip check with edge inputs.
- Compile coordinator: DB `compile_state` is set to "running" before the pass and only reset on handled branches; unexpected exceptions/cancellation leave it stale. Triggers arriving mid-pass are collapsed into "running" and not re-queued.
- Swallowed input errors (`contextlib.suppress(Exception)` on parent_id parsing) and early `return` treated as success by callers (daemon ingest no_token path).
- Admin restore wipes before validating/replaying. (Fixed on fix/project-review: validate-first, _FsSwap aside/rollback, wipe inside psql txn, _PsqlScriptScanner — scanner judged sound; re-check only if framing changes.)
- Retry-without-backoff: "keep the buffer and retry" fixes tend to add unbounded retries that re-spend LLM tokens (daemon session_reader idle retry). Always ask: attempt cap? backoff? partial-side-effect duplication?
- Build-to-disk-then-stream responses: Starlette StreamingResponse never acloses body_iterator; a generator that never starts never runs its finally. Temp files must be unlinked-while-open or swept; bootstrap sweeps only fs_root/.tmp, NOT fs_root/tmp.
- Coordinator spawn paths: `_closed` checked at trigger() entry but not after its awaits; any new spawn site needs the re-check.

**Why:** these are structural habits of the codebase, likely to recur in new code.
**How to apply:** on any diff touching these areas, check the same failure mode explicitly.
