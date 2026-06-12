# Wiring an MCP Client to KeenySpace

This guide connects Claude Code (or any MCP client speaking StreamableHTTP) to a running
KeenySpace server. Prerequisite: a working install and a logged-in CLI session — see
[docs/install.md](install.md).

## 1. What you are wiring

KeenySpace exposes its MCP server at `/v1/mcp` over StreamableHTTP. Agents authenticate
with a long-lived API key (`ks_live_*`) in the `Authorization` header. OIDC bearer tokens
also work, but they expire — API keys are the intended path for long-running MCP sessions.

Endpoint URL:

- Behind Caddy (recommended): `http://localhost/v1/mcp/`, or `https://<your-domain>/v1/mcp/` in production
- Direct to the server: `http://localhost:8000/v1/mcp/`

Keep the trailing slash: `/v1/mcp` (without it) answers with a 307 redirect, which
not every MCP client follows.

## 2. Mint an API key

API keys are minted by an authenticated user via `POST /v1/api/auth/api-keys`. After
`keenyspace login`, your session token is stored in `~/.config/keenyspace/auth.json`.
Mint a key with it:

```bash
TOKEN=$(python3 -c "import json,pathlib;print(json.loads(pathlib.Path.home().joinpath('.config/keenyspace/auth.json').read_text())['access_token'])")
curl -sS -X POST http://localhost:8000/v1/api/auth/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "claude-code"}'
```

The response contains the plaintext key exactly once:

```json
{
  "id": "...",
  "name": "claude-code",
  "key": "ks_live_...",
  "key_prefix": "ks_live_",
  "last4": "...",
  "created_at": "..."
}
```

**Store the `key` value securely now.** It is never shown again — list responses only
include the prefix and last four characters. To revoke a key:
`DELETE /v1/api/auth/api-keys/{id}`.

Note: if the OIDC group entry gate is enabled (`KEENYSPACE_AUTH__REQUIRED_GROUP`), it
applies when you log in and mint the key. The minted key itself bypasses the gate —
possession proves the key was created by an already-authorized user.

## 3. Configure the MCP client

### Claude Code

One-liner:

```bash
claude mcp add --transport http keenyspace http://localhost/v1/mcp/ \
  --header "Authorization: Bearer ks_live_..."
```

Or declare it in your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "keenyspace": {
      "type": "http",
      "url": "http://localhost/v1/mcp/",
      "headers": {
        "Authorization": "Bearer ks_live_..."
      }
    }
  }
}
```

Replace the URL with `https://<your-domain>/v1/mcp/` for a production server. Do not
commit a `.mcp.json` containing a real key — prefer the `claude mcp add` form (stores
config per-user) or an environment-variable expansion if your client supports it.

### Other MCP clients

Any client that supports StreamableHTTP transport works the same way: point it at
`/v1/mcp` and send `Authorization: Bearer ks_live_...` on every request.

## 4. Onboarding helpers

Two CLI commands smooth out the Claude Code integration:

```bash
keenyspace hooks install
```

Installs KeenySpace lifecycle hooks into Claude Code's `settings.json`
(`~/.claude/settings.json`, or pass `--project <dir>` for a per-project install). The
hooks re-inject workspace context on session start and after compaction, and observe
tool use for WAL logging. `keenyspace hooks status` shows what is installed;
`keenyspace hooks uninstall` removes only the KeenySpace entries and leaves your other
hooks untouched.

```bash
keenyspace workspace register <slug> [path]
```

Binds a local directory to a server workspace slug in
`~/.config/keenyspace/workspace-map.yaml` (defaults to the current git repo root). With
`--marker` it instead writes a `.keenyspace/slug-marker.json` into the directory. This
lets hooks and the CLI infer which workspace the current project belongs to.

## 5. Verify

Confirm the wiring with a tool listing. In Claude Code, run `/mcp` and check the
`keenyspace` server reports 11 tools:

`list_workspaces`, `get_workspace_info`, `read_page`, `list_pages`, `search_workspace`,
`append_log`, `get_instructions`, `list_blueprints`, `get_recent_changes`, `compile`,
`compile_status`

Then do a write-read roundtrip. Ask the agent (or call the tools directly) to:

1. `append_log` — append a note to a workspace WAL, e.g. workspace `demo`, content
   `MCP wiring verified`.
2. `compile` — trigger a compile for the workspace (requires the compile provider API
   key configured at install time), then poll `compile_status` until it completes.
3. `read_page` — read the compiled page and confirm the note landed.

If `append_log` succeeds, auth and the proxy path are correct. Remember the write model:
agents only ever append to the WAL — pages are produced exclusively by the server-side
compile. There is no direct page write surface.

## Troubleshooting

- **401 on every call:** the `Authorization` header is missing or the key was revoked.
  Verify with `curl -H "Authorization: Bearer ks_live_..." http://localhost/v1/mcp/` —
  a 401 from this means the key is bad; check `GET /v1/api/auth/api-keys` for
  `revoked_at`.
- **First MCP call works, second hangs or fails:** you are not going through the
  shipped configs. The provided Caddy/nginx configs disable response buffering
  (`flush_interval -1` / `proxy_buffering off`) — a custom proxy in between must do
  the same.
- **401 after login:** the group entry gate is enabled and your user is not in the
  required Authentik group. See the production hardening section of
  [docs/oidc-authentik-setup.md](oidc-authentik-setup.md).
