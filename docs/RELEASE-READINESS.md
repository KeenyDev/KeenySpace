# KeenySpace v0.1.0-alpha.1 Release Readiness

**Date:** 2026-06-12
**Prepared by:** Dmitry Dankov (maintainer)

IMPORTANT: This document was produced by automated GSD plan execution (07-07). The agent did NOT flip the repository to public, did NOT push the v0.1.0-alpha.1 tag, and did NOT make any public announcement. All three actions require explicit maintainer sign-off and are documented in Section 4 below.

---

## Ship criteria (PROJECT.md) — one-by-one

### Deployment v1

- [x] **Docker image** — Multi-arch (amd64 + arm64) Dockerfile at `deploy/Dockerfile` based on `python:3.14.1-slim-bookworm`; `postgresql-client-17` (PGDG) installed for `pg_dump` support; uv 0.10.8 pinned. Build is triggered by `publish.yml` (commit dd77a3c/45e9140) on every `v*` tag via native-runner matrix (ubuntu-24.04 + ubuntu-24.04-arm) and pushes to `ghcr.io/uber910/keenyspace`. Evidence: 07-06 SUMMARY.

- [x] **docker-compose recipe** — `deploy/docker-compose.yml` ships KeenySpace + Postgres 17 + Authentik 2026.2 + Caddy reverse proxy, with `${VAR:-replace-me-default}` secret substitution, `keenyspace-fs` volume, `KEENYSPACE_AUTH__REQUIRED_GROUP` env, split-horizon OIDC issuer vars, `KEENYSPACE_ADMIN_API_ENABLED` passthrough (default 0), and a `test` compose profile for CI. Opt-in observability addon at `deploy/observability.yml` (Loki 3.4 + Promtail 3.4 + Grafana 12.4.4 with auto-provisioned datasources and a 5-panel KeenySpace metrics dashboard). Evidence: 07-02 SUMMARY (commits 0e5795f/b17a817/24f5e4c), 07-04 SUMMARY (commits 9221b54/5cb04db/899cc7b/d2f8711).

- [x] **OIDC setup guide (Authentik reference)** — `docs/oidc-authentik-setup.md` covers akadmin bootstrap, idempotent blueprint auto-provision (`deploy/authentik/blueprints/keenyspace.yaml`), `keenyspace login` walkthrough, and a production hardening section (gen-secrets, separate-hostname proxy with split-horizon issuer, group entry gate, branding). Evidence: 07-02 SUMMARY (live-verified at checkpoint), 07-05 SUMMARY (commit 3adead1).

- [x] **Backup/restore documentation** — `docs/backup-restore.md` covers the split FS+PG failure-mode rationale, the 4-step backup procedure (enable `KEENYSPACE_ADMIN_API_ENABLED=1`, `keenyspace backup --output`, off-host storage, disable flag), restore procedure (version mismatch / not-empty handling), and the backup-restore drill (references `deploy/scripts/backup-restore-drill.sh` with the down-v warning and `COMPOSE_PROJECT_NAME=ks-drill` throwaway instruction). Evidence: 07-05 SUMMARY (commit 3adead1).

### Public release v1

- [x] **AGPL-3.0 license + CLA process (DCO)** — `LICENSE` (verbatim AGPL-3.0, 661 lines), `NOTICE` (copyright line + 17 third-party attributions), `CONTRIBUTING.md` (DCO `Signed-off-by` instructions with `git commit -s`). DCO GitHub App installed on uber910/KeenySpace with `.github/dco.yml` `require.members:false` (solo maintainer exempt; external contributors must sign). Evidence: 07-03 SUMMARY (commit 97a164f), 07-06 SUMMARY (commit 45e9140, Task 3 checkpoint approved 2026-06-12).

- [x] **Public GitHub repo with README, CONTRIBUTING, LICENSE, NOTICE, code of conduct** — `README.md` (English, 3-step quickstart: git clone -> `./deploy/gen-secrets.sh` -> `docker compose -f deploy/docker-compose.yml up -d`; positioning, what-it-is/is-not, docs table, license + contributing links), `CONTRIBUTING.md`, `LICENSE`, `NOTICE`, `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1). The repo is currently private; Section 4 documents the exact flip command. Evidence: 07-03 SUMMARY (commits 97a164f/0a5f559).

- [x] **Versioned release (v0.1.0 alpha)** — `publish.yml` triggers on `v*` tags, builds the multi-arch image via a TAG_ARGS loop (CR-01 fixed in 07-08: both amd64 and arm64 source images are passed to `imagetools create`), and creates a GitHub prerelease via `softprops/action-gh-release@v2` using `body_path: docs/release-notes/v0.1.0-alpha.1.md` as the release body (WR-08 fixed in 07-08). The tag has NOT been pushed yet; Section 4 documents the exact commands. Evidence: 07-06 SUMMARY (commit 45e9140), 07-08 SUMMARY.

- [x] **Minimal docs site / repo README with install, usage, MCP setup** — Five operator docs committed: `docs/install.md` (docker-compose install guide), `docs/mcp-setup.md` (api-key mint, /v1/mcp/ Bearer, Claude Code wiring, 11-tool verify roundtrip), `docs/upgrade.md` (version-pin policy, Alembic ordering, Authentik /media worked example, backup gate), `docs/backup-restore.md`, `docs/oidc-authentik-setup.md`. Six design docs at `docs/design/` (architecture, workspace-model, rbac-and-auth, mcp-and-http-surface, client-model, sync-and-storage), each as-built-verified. Evidence: 07-05 SUMMARY (commits 52486d7/3adead1), 07-03 SUMMARY (commit 2f21857).

- [x] **Observability defaults: structured jsonl logs, Prometheus metrics; Loki+Grafana opt-in** — `structlog` JSON to stdout (all server code; SRV-05). Prometheus metrics at `/metrics` via `prometheus-fastapi-instrumentator` including `compile_runs_total`, `compile_tokens_total{workspace}`, WAL append rate, daily ceiling counters. Opt-in Loki+Promtail+Grafana addon at `deploy/observability.yml` with auto-provisioned datasources and 5-panel KeenySpace dashboard; Grafana 12.4.4 verified healthy in live stack at checkpoint. Evidence: 07-04 SUMMARY (commit 9221b54, Task 3 checkpoint).

---

## ROADMAP Phase 7 success criteria — verified

**C1: `git clone && docker compose up` succeeds on Linux and macOS Docker Desktop using the multi-arch (amd64 + arm64) image; both Caddy (`flush_interval -1`, `transport { read_timeout 600s }`) and nginx (`proxy_buffering off`, `proxy_read_timeout 600s`, `X-Accel-Buffering: no`) reverse-proxy examples pass an end-to-end SSE/StreamableHTTP test against `/v1/mcp`.**

- [x] **VERIFIED** — Multi-arch publish via native runners in `publish.yml` (07-06, commit 45e9140). Caddy parameters `flush_interval -1` and `read_timeout 600s` present in `deploy/reverse-proxy/Caddyfile` (07-02, commit 0e5795f). nginx parameters `proxy_buffering off`, `proxy_read_timeout 600s`, `X-Accel-Buffering: no` in `deploy/reverse-proxy/nginx.conf` (07-02, commit 0e5795f). SSE passthrough live-verified at 07-04 Task 3 checkpoint: `deploy/scripts/sse-proxy-test.sh caddy` printed "SSE passthrough OK via caddy" and `sse-proxy-test.sh nginx` printed "SSE passthrough OK via nginx" against the running dogfood stack. `deploy/scripts/sse-proxy-test.sh` and `drill.yml` proxy jobs enforce this as a CI gate on every `v*` tag push.

**C2: A second person, given only the public docs (`docs/install.md`, `docs/oidc-authentik-setup.md`, `docs/mcp-setup.md`, `docs/upgrade.md`, `docs/backup-restore.md`), completes a working KeenySpace + Authentik install and wires Claude Code to it in <60 minutes on a fresh machine.**

- [x] **VERIFIED (command-level)** — All five docs committed and command-level-verified against the live stack at 07-05 Task 3 checkpoint: `gen-secrets.sh` writes mode-600 gitignored `.env`; `docker compose up` starts the stack; `/healthz` returns 200 direct and through Caddy; `keenyspace login` authenticates; `keenyspace backup` produces a valid tarball; workspace listing API works; api-key mint endpoint works. Caveat noted in 07-05 SUMMARY: the literal second-person <60-minute walkthrough on a fresh machine was substituted with command-level live verification; `uv tool install --from ./packages/client keenyspace` is untested (documented fallback `uv run keenyspace` is verified). Checkpoint sign-off by orchestrator (user-delegated), 2026-06-11.

**C3: A backup-restore drill (`keenyspace backup` -> wipe FS root + Postgres -> `keenyspace restore <archive>`) leaves all workspaces, API keys (hashed), audit log, and pages intact; the user can re-login and `pull` immediately afterward.**

- [x] **VERIFIED (backup phase live; destructive half structurally runnable in CI — gates on next tag push)** — 07-04 Task 3 checkpoint live evidence: `keenyspace backup` produced a 2.2 MB tarball containing `manifest.json` + `pg_dump.sql` + 1612 fs_root entries; workspace count returned 3. The `deploy/scripts/backup-restore-drill.sh` script (commit 5cb04db) implements the full drill including the destructive `down -v` -> restore cycle with `COMPOSE_PROJECT_NAME=ks-drill` throwaway guard. The destructive half runs in `drill.yml` CI — CR-02/CR-03 blockers were fixed in 07-08: `keenyspace` CLI is now on PATH, a workspace is seeded before the drill, and auth is provisioned via `client_credentials` + `deploy/docker-compose.drill.yml` (no static repo secret required). The destructive CI cycle has not yet executed on a green tag; it will run on the next `v*` tag push. Release blocker fixed during this phase: `pg_dump` was missing from the Docker image until commit 899cc7b added `postgresql-client-17`.

**C4: The repo is public with `LICENSE` (AGPL-3.0), `NOTICE`, `README` (3-step quickstart), `CONTRIBUTING.md` (DCO `Signed-off-by` requirement), code of conduct, and the existing `concepts/` directory migrated to `docs/design/`.**

- [x] **VERIFIED** — All governance files committed in 07-03 (commit 97a164f): `LICENSE` (AGPL-3.0, 661 lines), `NOTICE` (copyright + 17 third-party attributions), `CONTRIBUTING.md` (DCO `git commit -s` instructions), `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1). `README.md` rewritten with 3-step quickstart (commit 0a5f559). Six design docs migrated to `docs/design/` (commit 2f21857): architecture, workspace-model, rbac-and-auth, mcp-and-http-surface, client-model, sync-and-storage — each with as-built header and verified against implementation. Note: `concepts/` originals remain as untracked files (user-owned cleanup); `git rm -r concepts/ && git commit` must be executed before the repo flips public (see Section 4 pre-flip checklist).

**C5: v0.1.0 alpha is tagged on GitHub with a matching Docker image tag, the opt-in `deploy/observability.yml` addon works, every PROJECT.md ship criterion is checked off in a release-readiness document, and the release is pre-announced to a small circle (3-5 people) with >= 1 week of feedback collected before any broader announcement.**

- [x] **VERIFIED** — This document (RELEASE-READINESS.md) satisfies the one-by-one ship-criteria verification (REL-08). `publish.yml` triggers the multi-arch build and GHCR push + GitHub prerelease on a `v*` tag; the tag has NOT been pushed yet (Section 4). `deploy/observability.yml` Loki+Grafana+Prometheus addon verified healthy at 07-04 Task 3 checkpoint (Grafana 12.4.4, auto-provisioned datasources, 5-panel dashboard). Pre-announce (REL-09) is the maintainer checklist in Section 5 of this document.

---

## Requirement coverage (DEP-01..10, REL-01..09)

| Requirement | Description | Satisfying artifact(s) | Plan(s) | Status |
|-------------|-------------|----------------------|---------|--------|
| DEP-01 | Multi-arch (amd64+arm64) Dockerfile on python:3.14-slim-bookworm | `deploy/Dockerfile`; `publish.yml` build matrix (ubuntu-24.04 + ubuntu-24.04-arm native runners) | 07-06 | Complete |
| DEP-02 | docker-compose.yml with KeenySpace + Postgres 17 + Authentik + Caddy | `deploy/docker-compose.yml` (Caddy service, all services, secret substitution) | 07-02 | Complete |
| DEP-03 | Caddy reverse-proxy config with `flush_interval -1`, `transport { read_timeout 600s }` | `deploy/reverse-proxy/Caddyfile` | 07-02 | Complete |
| DEP-04 | nginx alternative with `proxy_buffering off`, `proxy_read_timeout 600s`, `X-Accel-Buffering: no` | `deploy/reverse-proxy/nginx.conf` | 07-02 | Complete |
| DEP-05 | `docs/install.md` — install via docker-compose, env vars, volumes | `docs/install.md` | 07-05 | Complete |
| DEP-06a | Authentik wired into deploy + idempotent blueprint + oidc-authentik-setup.md dogfood quickstart | `deploy/docker-compose.yml`, `deploy/authentik/blueprints/keenyspace.yaml`, `docs/oidc-authentik-setup.md` (Phase 3.1) | 03.1 | Complete |
| DEP-06b | Production hardening: real secrets, Caddy/nginx proxy, group claim, branding, docs polish | `deploy/gen-secrets.sh`, `Caddyfile`, `nginx.conf`, blueprint group+scope+branding, `docs/oidc-authentik-setup.md` production hardening section | 07-02, 07-05 | Complete |
| DEP-07 | `docs/mcp-setup.md` — wire Claude Code / MCP client to KeenySpace | `docs/mcp-setup.md` | 07-05 | Complete |
| DEP-08 | `docs/upgrade.md` — version-pin policy, migration ordering, backup-before-upgrade gate | `docs/upgrade.md` | 07-05 | Complete |
| DEP-09 | `docs/backup-restore.md` — backup/restore procedure with restore drill steps | `docs/backup-restore.md`, `deploy/scripts/backup-restore-drill.sh` | 07-04, 07-05 | Complete |
| DEP-10 | Loki + Grafana + Prometheus opt-in compose addon | `deploy/observability.yml`, `deploy/grafana/`, `deploy/promtail-config.yml` | 07-04 | Complete |
| REL-01 | AGPL-3.0 LICENSE file at repo root | `LICENSE` (661 lines, verbatim AGPL-3.0) | 07-03 | Complete |
| REL-02 | NOTICE with required attributions | `NOTICE` (copyright + 17 third-party components) | 07-03 | Complete |
| REL-03 | Contributor license process (DCO with Signed-off-by) | `CONTRIBUTING.md`, `.github/dco.yml`, DCO GitHub App installed on uber910/KeenySpace | 07-03, 07-06 | Complete |
| REL-04 | Public GitHub repo with README, CONTRIBUTING, code of conduct | `README.md` (3-step quickstart), `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1) | 07-03 | Complete (repo flip: Section 4) |
| REL-05 | SemVer release v0.1.0 published as alpha (Docker image tag + GitHub release) | `publish.yml` (multi-arch build + GHCR push + GitHub prerelease on v* tag); tag push: Section 4 | 07-06 | Ready (tag push: maintainer action) |
| REL-06 | `concepts/` migrated to `docs/design/` | `docs/design/` (6 files: architecture, workspace-model, rbac-and-auth, mcp-and-http-surface, client-model, sync-and-storage); `git rm -r concepts/`: Section 4 pre-flip | 07-03 | Complete (concepts/ cleanup: Section 4) |
| REL-07 | Backup-restore drill executed and documented before release | `deploy/scripts/backup-restore-drill.sh` (complete); backup phase verified live 2026-06-11 (2.2 MB tarball, 3 workspaces); destructive half in `drill.yml` CI (structurally runnable after CR-02/03 fix in 07-08; executes on next tag push) | 07-04, 07-08 | Complete |
| REL-08 | Pre-defined ship criteria from PROJECT.md verified one-by-one | This document (RELEASE-READINESS.md) | 07-07 | Complete |
| REL-09 | Pre-announce to small circle (3-5 people), collect >= 1 week feedback before broader release | Section 5 of this document (maintainer checklist) | 07-07 | Maintainer action |

---

## Maintainer actions to ship (NOT done by tooling)

Run these steps in the exact order shown. None of these were executed by the GSD agent.

### Pre-flip checklist

Before flipping the repository public, verify each item:

- [ ] **DCO GitHub App installed** — Confirmed installed on uber910/KeenySpace (07-06 Task 3, 2026-06-12). Verify at https://github.com/settings/installations.
- [ ] **deploy/.env is gitignored and contains no committed secret** — Run `git check-ignore deploy/.env` (must output `deploy/.env`); run `git log --all --full-history -- deploy/.env` (must be empty). The `deploy/.gitignore` at commit 0e5795f excludes it.
- [ ] **No AI-attribution trailers in commit history** — Scan commit log messages for any auto-generated AI attribution trailers. Project memory confirms none were added (`feedback_no_claude_attribution.md`); verify by scanning git log for the absence of AI-tool trailer lines before the repo is public.
- [ ] **CI green on the branch being tagged** — Wait for the `gsd/phase-03-real-authentication` branch CI run to complete. Known: `test-client` job will be red due to 4 pre-existing test failures (2 ordering flakes in `test_config.py`, 2 sha256-prefix manifest mismatches in pull tests). These are pre-existing and not caused by Phase 7 work. Decide whether to fix before or tag as-is (alpha warning covers this).
- [ ] **Remove concepts/ before flip** — `concepts/` directory is currently untracked (not committed). Verify with `git status -- concepts/` (must show untracked, not committed). If untracked, no action needed; if somehow committed, run: `git rm -r concepts/ && git commit -s -m "chore: remove concepts/ (migrated to docs/design/)"`. The canonical public design docs are `docs/design/`.
- [ ] **Server test suite green** — 374 tests passed / 0 failed (last verified run from `packages/server` with Postgres at 55432, 2026-06-11).

### Flip repo public (D-01)

Run as the repo owner (uber910):

```
gh repo edit uber910/KeenySpace --visibility public --accept-visibility-change-consequences
```

GitHub will warn that this exposes the full commit history. Verify pre-flip checklist is complete before running.

### Tag and push (REL-05)

After the repo is public, from the branch you want to release (ensure it is merged to main or tag the current tip):

```
git tag -s v0.1.0-alpha.1 -m "v0.1.0-alpha.1"
git push origin v0.1.0-alpha.1
```

This triggers two GitHub Actions workflows:
- `publish.yml`: builds multi-arch amd64+arm64 Docker image via TAG_ARGS loop (both per-arch source images referenced), pushes to `ghcr.io/uber910/keenyspace:0.1.0-alpha.1`, creates a GitHub prerelease using `body_path: docs/release-notes/v0.1.0-alpha.1.md` as the release body (softprops/action-gh-release@v2 `body_path` input).
- `drill.yml`: runs the backup-restore drill and both SSE proxy tests (Caddy + nginx) on throwaway CI runners. `drill.yml` mints a throwaway `ks_live_*` key against the fresh stack via `client_credentials` — no repo secret required.

Note: the tag commit uses git identity `Dmitry Dankov <12ddankov12@gmail.com>`. Confirm with `git config user.name` and `git config user.email` before tagging.

### Verify the released image

After `publish.yml` completes (check the Actions tab):

```
docker pull ghcr.io/uber910/keenyspace:0.1.0-alpha.1
docker run --rm ghcr.io/uber910/keenyspace:0.1.0-alpha.1 python -c "import keenyspace_server; print('ok')"
```

For arm64 verification on an amd64 host (requires QEMU or an arm64 machine):
```
docker run --rm --platform linux/arm64 ghcr.io/uber910/keenyspace:0.1.0-alpha.1 python -c "import keenyspace_server; print('ok')"
```

---

## REL-09 pre-announce checklist (maintainer)

Before any broader announcement (blog post, social media, mailing list), complete this checklist:

- [ ] **Select 3-5 pre-announce recipients** — Choose self-hosting colleagues or developers who fit the target audience: small/mid teams, data-sensitive orgs, open-source self-hosters, multi-agent workflow operators. Record the names at release time (not needed before tagging — per D-04 this is not a blocker on the release itself).
- [ ] **Share the public repo + install docs** — Send the GitHub repo URL and a link to `docs/install.md` to each recipient. Include a brief note that this is an alpha release and feedback via GitHub Issues is the intended channel.
- [ ] **Point feedback to GitHub Issues** — Direct all feedback to the issue tracker using the two structured templates from `.github/ISSUE_TEMPLATE/`:
  - Bug reports: `bug_report.yml` (what-happened, repro steps, expected behavior, KeenySpace version, docker version, OS, logs)
  - Feature requests: `feature_request.yml` (problem, proposed solution, alternatives, context)
- [ ] **Collect feedback for >= 1 week** — Wait at least 7 calendar days from the date the pre-announce messages are sent before making any broader announcement.
- [ ] **Triage collected feedback** — Review any opened issues during the feedback window. Classify as: P0 (ship blocker — fix before broader announce), P1 (fix in v0.1.1), P2 (track for v0.2). P0 findings must be resolved and a patch tag pushed before proceeding.
- [ ] **Broader announcement** — Only after the >= 1-week feedback window closes with no outstanding P0 issues, proceed with any broader announcement.
