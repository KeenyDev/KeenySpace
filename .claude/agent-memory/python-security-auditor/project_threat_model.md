---
name: project-threat-model
description: KeenySpace verified trust boundaries, auth mechanisms, and recurring risky patterns (audits of 2026-09-22 on branch fix/project-review)
metadata:
  type: project
---

Boundaries verified by reading code (2026-09-22, branch fix/project-review):
- Auth: single Starlette AuthenticationMiddleware at root (auth/composite.py) covers REST + /v1/mcp mount; PUBLIC_PREFIXES startswith list. JWT: joserfc, alg allowlist RS256/ES256, aud=oidc_client_id (keenyspace-cli public client), exp/iat essential, iss manual compare, and (since 4a06ab0) a string `scope` claim is required to reject ID tokens. API keys: sha256(body+pepper) lookup + argon2 verify, 60 s success-only cache with invalidation epoch.
- Group snapshot (auth/group_snapshot.py): users.groups written from any OIDC token carrying a groups claim, BEFORE the required_group check; groups_seen_at = now (not token iat) and upsert is unconditional -> stale-but-unexpired tokens can roll the snapshot back. API keys are authorized (required_group + admin_group) with that snapshot.
- Admin API: mounted only with KEENYSPACE_ADMIN_API_ENABLED=1; AdminRoute (auth/admin_gate.py) checks admin_group before body parsing.
- No per-workspace ACL in v1 (AUTH-09 locked design) — not IDOR, but amplifies every authed-user bug.
- API keys can mint API keys (routers/api_keys.py has no source check) -> child keys ignore parent expiry.
- Page path safety: fs/path_safety.py; compile writes gated by is_compile_writable, re-checked on intent replay (apply_plan). Client pull resolves every manifest path via resolve_vault_path before any write.
- Metrics: prometheus start_http_server, default bind 0.0.0.0:9100 (not in compose ports).

Recurring risky pattern: user-supplied names joined onto fs paths WITHOUT path_safety. Check any new Path(...) / user_str join. Also: new security controls (expiry, snapshots) that a sibling path (key-mints-key, stale token) routes around.

**Why:** baseline for future diffs; re-verify against current code before reuse.
**How to apply:** start audits by re-checking these spots; if fixed, update this file.
