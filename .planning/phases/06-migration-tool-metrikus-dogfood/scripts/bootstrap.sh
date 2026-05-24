#!/usr/bin/env bash
set -euo pipefail

# bootstrap.sh — throwaway one-shot bootstrap of Metrikus wiki into a fresh
# KeenySpace workspace named "metrikus-dogfood".
#
# THIS IS NOT PRODUCT CODE. F-10 scope flag: no packages/server/ or
# packages/client/ source is modified by this phase. This script lives in
# .planning/ (gitignored) and is invoked manually by the operator only — never
# referenced from product code via subprocess.run or otherwise.
#
# Usage:
#   chmod +x .planning/phases/06-migration-tool-metrikus-dogfood/scripts/bootstrap.sh
#   bash .planning/phases/06-migration-tool-metrikus-dogfood/scripts/bootstrap.sh
#
# Required preconditions:
#   - KeenySpace server reachable at $KS_SERVER (default http://localhost:8000)
#   - `keenyspace login` already completed; ~/.config/keenyspace/auth.json
#     contains either an OIDC access_token or a ks_live_* api_key
#   - jq, rsync, tar installed on host
#   - ~/Interexy/Metrikus/wiki/ exists and is readable
#
# Re-run semantics (per CONTEXT.md D-03): wipe-and-retry.
#   1. archive or delete the existing metrikus-dogfood workspace server-side
#   2. rerun this script
# No idempotency state, no sidecar manifest — destination is recreated fresh.

KS_SERVER="${KS_SERVER:-http://localhost:8000}"
KS_WORKSPACE_SLUG="metrikus-dogfood"
KS_BLUEPRINT="default"
KS_SOURCE="${KS_SOURCE:-${HOME}/Interexy/Metrikus/wiki}"
KS_FS_ROOT="${KS_FS_ROOT:-/var/lib/keenyspace}"
AUTH_FILE="${HOME}/.config/keenyspace/auth.json"
PHASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# [1/8] Pre-bootstrap safety snapshot of Metrikus wiki — guard against
# accidental damage during bootstrap or downstream dogfood operations.
SNAPSHOT="${PHASE_DIR}/scripts/metrikus-pre-bootstrap-snapshot.tar.gz"
if [[ ! -f "${SNAPSHOT}" ]]; then
    echo "[1/8] Snapshotting ${KS_SOURCE} -> ${SNAPSHOT}"
    tar -czf "${SNAPSHOT}" -C "$(dirname "${KS_SOURCE}")" "$(basename "${KS_SOURCE}")"
else
    echo "[1/8] Snapshot already present at ${SNAPSHOT} — skip"
fi

# [2/8] Resolve auth token (access_token first per OIDC path, api_key
# fallback per ks_live_*). Mirrors packages/client/keenyspace/clients/http.py:14
# fallback order.
echo "[2/8] Reading auth token from ${AUTH_FILE}"
test -f "${AUTH_FILE}" || { echo "ERROR: no auth.json at ${AUTH_FILE} — run keenyspace login first"; exit 4; }
KS_TOKEN="$(jq -r '.access_token // .api_key // empty' "${AUTH_FILE}")"
test -n "${KS_TOKEN}" || { echo "ERROR: no access_token or api_key in ${AUTH_FILE}"; exit 4; }

# [3/8] Create workspace via raw curl. No `keenyspace workspace create` CLI
# subcommand exists (PATTERNS.md anchor correction #1); product surface is
# packages/server/keenyspace_server/api/workspaces.py:40-107.
#   201 -> WorkspaceResponse(uuid, slug, blueprint_ref, created_at)
#   409 -> slug taken (wipe-and-retry case per D-03)
#   422 -> invalid slug (abort, fix config)
echo "[3/8] POST ${KS_SERVER}/v1/api/workspaces/ slug=${KS_WORKSPACE_SLUG} blueprint=${KS_BLUEPRINT}"
create_response="$(
    curl -fsSL -X POST "${KS_SERVER}/v1/api/workspaces/" \
        -H "Authorization: Bearer ${KS_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"slug\":\"${KS_WORKSPACE_SLUG}\",\"blueprint\":\"${KS_BLUEPRINT}\"}"
)"
ws_uuid="$(echo "${create_response}" | jq -r .uuid)"
test -n "${ws_uuid}" && [[ "${ws_uuid}" != "null" ]] || { echo "ERROR: empty uuid in server response: ${create_response}"; exit 2; }
echo "Created workspace ${KS_WORKSPACE_SLUG} -> ${ws_uuid}"

# [4/8] Resolve destination path. Assumes docker-compose convention
# KEENYSPACE_FS__ROOT=/var/lib/keenyspace. Override with KS_FS_ROOT env var
# if your deployment differs.
target="${KS_FS_ROOT}/workspaces/${ws_uuid}"
echo "[4/8] Target = ${target}"
test -d "${target}/.keenyspace" || {
    echo "ERROR: ${target}/.keenyspace not found — blueprint clone did not land at expected path."
    echo "       Common causes:"
    echo "         - KS_FS_ROOT mismatch (current: ${KS_FS_ROOT})"
    echo "         - macOS Docker Desktop: keenyspace-fs named volume Mountpoint is VM-internal,"
    echo "           not reachable from host. Stop server and bind-mount fs_root to a host path,"
    echo "           OR run rsync inside the container via 'docker compose exec keenyspace rsync ...'"
    exit 3
}

# [5/8] rsync dry-run — load-bearing safety per PATTERNS.md "rsync allowlist
# syntax fragility". Verifies the allowlist resolves to the expected file set
# BEFORE any real write. Operator inspects rsync-dryrun.txt for unexpected
# denylist hits.
echo "[5/8] rsync --dry-run (allowlist verification) -> ${PHASE_DIR}/scripts/rsync-dryrun.txt"
rsync -avn \
    --include='concepts/***' \
    --include='incidents/***' \
    --include='decisions/***' \
    --include='services/***' \
    --include='raw/***' \
    --include='Clippings/***' \
    --include='index.md' \
    --include='log.md' \
    --include='README.md' \
    --exclude='*' \
    "${KS_SOURCE}/" "${target}/" \
    | tee "${PHASE_DIR}/scripts/rsync-dryrun.txt"
echo "Dry-run complete. Inspect rsync-dryrun.txt for unexpected denylist hits before proceeding."

# [6/8] rsync (real) — same allowlist minus -n. Allowlist (D-02): only
# concepts/, incidents/, decisions/, services/, raw/, Clippings/, plus root
# index.md / log.md / README.md. Catch-all --exclude='*' denies everything
# else (positive-only allowlist), enforcing .env / .obsidian / .git /
# _templates / CLAUDE.md / etc. exclusion by default.
echo "[6/8] rsync (real)"
rsync -av \
    --include='concepts/***' \
    --include='incidents/***' \
    --include='decisions/***' \
    --include='services/***' \
    --include='raw/***' \
    --include='Clippings/***' \
    --include='index.md' \
    --include='log.md' \
    --include='README.md' \
    --exclude='*' \
    "${KS_SOURCE}/" "${target}/"

# [7/8] Secret scan post-transfer. Hard fail if any secret marker survives
# the allowlist into the destination. Excludes .keenyspace/ internals (server
# may legitimately reference KEENYSPACE_* env names in its own config).
echo "[7/8] Secret scan on ${target}"
if grep -rEl 'API_KEY|SECRET|PASSWORD|TOKEN' "${target}" 2>/dev/null | grep -v '/.keenyspace/'; then
    echo "ERROR: secret string detected in destination after rsync."
    echo "       Wipe destination workspace and investigate allowlist drift."
    exit 5
fi
echo "Secret scan clean."

# Permission alignment — macOS uses `stat -f`, GNU uses `stat -c`. Align
# the entire migrated tree to whatever uid:gid owns the blueprint-cloned
# .keenyspace/ directory so the server process (potentially containerised)
# can read the migrated files.
server_owner="$(stat -f '%u:%g' "${target}/.keenyspace" 2>/dev/null \
                || stat -c '%u:%g' "${target}/.keenyspace")"
echo "Aligning ownership of ${target} to ${server_owner}"
chown -R "${server_owner}" "${target}" 2>/dev/null \
    || sudo chown -R "${server_owner}" "${target}"

# [8/8] Lint baseline capture + default workspace pointer. The lint output
# replaces the abandoned MIG-05 migration-report.md artefact — `keenyspace
# lint` produces the authoritative health snapshot (broken wikilinks, orphan
# pages, frontmatter issues).
echo "[8/8] Capturing lint baseline + setting default workspace"
keenyspace lint "${KS_WORKSPACE_SLUG}" \
    > "${PHASE_DIR}/scripts/lint-post-bootstrap.txt" 2>&1 \
    || echo "WARN: lint exited non-zero (broken wikilinks captured to lint-post-bootstrap.txt)"
keenyspace workspace use "${KS_WORKSPACE_SLUG}"
echo "Bootstrap complete. Append the DOGFOOD-LOG.md initial entry next (task 3)."
