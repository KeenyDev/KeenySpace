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

### Signing in with OAuth instead of an API key

A client that speaks the MCP OAuth bootstrap (Claude Code's `/mcp` -> Authorize) can log
the user in interactively, with no `ks_live_*` key to mint, paste, or rotate. Add the
server without an `Authorization` header:

```bash
claude mcp add --transport http keenyspace http://localhost/v1/mcp/
```

Then run `/mcp`, pick `keenyspace`, and choose Authorize. The browser opens Authentik, and
the tokens live in the client rather than in a config file.

How the discovery chain works, in case it needs debugging:

1. The unauthenticated request to `/v1/mcp` gets a 401 carrying
   `WWW-Authenticate: Bearer resource_metadata="<public-url>/.well-known/oauth-protected-resource"`.
   Only the MCP mount emits this header — `/v1/api` keeps a plain 401.
2. That URL serves RFC 9728 protected-resource metadata naming the Authentik issuer as the
   authorization server. Both the bare path and the RFC 9728 path-suffix form
   (`/.well-known/oauth-protected-resource/v1/mcp`) return the same document, since clients
   differ on which they request.
3. The client authorizes against Authentik as the existing `keenyspace-cli` provider.
   There is no dynamic client registration; the provider allows RFC 8252 loopback redirect
   URIs (any `127.0.0.1`/`localhost` port with a `/callback`, `/oauth/callback` or
   `/auth/callback` path), which is what a native client's ephemeral listener needs.

**`KEENYSPACE_SERVER__PUBLIC_URL` must be the URL clients actually reach** (default
`http://localhost:8000`; behind a reverse proxy, your real origin). The challenge header
and the metadata document are both built from it, so a wrong value sends clients to an
address they cannot resolve and the Authorize button never completes.

Access tokens expire, so an API key remains the better fit for unattended agents and long
running sessions; OAuth is the better fit for a person at a keyboard.

### Pinning a workspace to the connection

Every workspace-scoped tool takes a `workspace` argument. Append `?workspace=<slug>` to
the endpoint URL to pin one for the whole connection, and the argument becomes optional —
calls that omit it hit the pinned workspace:

```bash
claude mcp add --transport http keenyspace-notes "http://localhost/v1/mcp/?workspace=notes" \
  --header "Authorization: Bearer ks_live_..."
```

An explicit `workspace` argument always overrides the pin. Register one server entry per
workspace when an agent works in several. With no argument and no pin, tools fail with
`no workspace specified`.

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
