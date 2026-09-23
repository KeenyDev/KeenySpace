---
name: project-perf-case-law
description: KeenySpace performance case law - recorded tradeoffs (no FTS index, single-worker), measured argon2 baseline, known hot/unbounded paths as of 2026-09-22 review
metadata:
  type: project
---

Recorded tradeoffs (respect; challenge only with data): no full-text/vector index in v1 (brute vault scan is by design); single-worker uvicorn; per-workspace asyncio.Lock registry keyed by workspace UUID (bounded by workspace count, not a leak).

**Why:** locked in PROJECT.md / CLAUDE.md architectural rules.

**How to apply:** flag per-page rescans, missing early-exit and missing concurrency caps around vault scans, not the absence of an index.

Baseline (2026-09-22, 8-core Apple Silicon dev laptop, argon2-cffi defaults t=3 m=64MiB p=4): PasswordHasher.verify median 37 ms, sd 6 ms, n=20. ApiKeyService.verify runs it on every ks_live_ request.

Hot/unbounded paths found in the 2026-09-22 review (check current code before reuse): compile/wal_slice.py re-parses the full WAL history on every pass; the slice is not chunked against max_input_tokens; export and admin backup buffer whole archives in memory; pages-raw reads raw/ files on the event loop; client pull re-downloads unchanged files; session_reader buffers grow without bound on ingest failure.
