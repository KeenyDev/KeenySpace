# OIDC Authentik Setup

Status: Dogfood quickstart section added (Phase 3.1 DEP-06a). Production hardening section added (Phase 7 DEP-06b).

## Dogfood quickstart

This section covers bringing up KeenySpace + Authentik in a local dogfood environment.
No prior Authentik knowledge required. The blueprint auto-provisions the OIDC application
on every startup so you can run `keenyspace login` immediately after `docker compose up`.

**Step 1: Start the full stack**

```
docker compose up
```

This starts: KeenySpace, Postgres, Authentik (server + worker), Authentik Postgres, Redis.
Wait until all services pass their healthchecks (typically 60-90 seconds for Authentik).

**Step 2: Default credentials warning**

The `deploy/docker-compose.yml` ships with `*-replace-me` placeholder values for:

- `AUTHENTIK_BOOTSTRAP_PASSWORD` (akadmin initial password)
- `AUTHENTIK_BOOTSTRAP_TOKEN` (admin API token)
- `AUTHENTIK_SECRET_KEY` (session signing key)

These are fine for local dogfood. Replace them before any production or internet-facing
deployment (Phase 7 DEP-06b owns the real-secret-management section of this doc).

**Step 3: Blueprint auto-provision evidence**

The `authentik-worker` service applies `deploy/authentik/blueprints/keenyspace.yaml`
on every startup. This idempotently provisions:

- OAuth2 provider `keenyspace-cli` (public client, device-code enabled, per_provider issuer)
- Application `keenyspace` (slug: keenyspace)
- Brand device-code flow enabled

Verify the application was provisioned (after Authentik is healthy):

```
curl -H "Authorization: Bearer <AUTHENTIK_BOOTSTRAP_TOKEN>" \
  http://localhost:9000/api/v3/core/applications/?slug=keenyspace
```

A non-empty `results` array confirms the blueprint applied. The blueprint survives
`docker compose down -v` and re-applies on next `docker compose up`.

**Step 4: keenyspace login walkthrough**

The CLI probes `/v1/api/auth/discovery` to find the IdP issuer, then starts device-code:

```
keenyspace login
```

The CLI prints a verification URL. Open it in a browser, log in with the akadmin credentials,
and approve the device code. The CLI polls until the code is approved, then stores the
session token. This exercises the full RFC 8628 device-code flow against real Authentik.

**Step 5: Smoke read**

After login succeeds, verify the session works:

```
keenyspace workspace list
```

Or via mcp-inspector against `/v1/mcp` with `read_page` on any workspace page.

**Deferred to Phase 7 DEP-06b (not in this section):**

- Real secret management (replace `*-replace-me` placeholders with vault/SOPS/env-files)
- Reverse-proxy / TLS in front of Authentik (Caddy/nginx)
- Group claim to workspace authorization mapping
- Brand customization for Authentik login page
- Full production deployment guide

---

## What this doc covers (Phase 7 scope)

- Authentik OAuth2 provider configuration:
  - Application + Provider creation
  - `redirect_uri` = `https://<keenyspace-host>/v1/api/auth/callback`
  - `post_logout_redirect_uri` = `https://<keenyspace-host>/`
  - Scopes claim mapping: `openid` + `profile` + `email` + `groups`
- Device-code provider (AUTH-05; required by Phase 5 CLI `keenyspace login` headless flow):
  - Separate provider config in Authentik (distinct from interactive OAuth2 provider)
  - Same audience + scope set as the interactive provider — without this CLI tokens
    will NOT validate against KeenySpace middleware
  - URL: `/application/o/device/`
  - Reference: https://docs.goauthentik.io/add-secure-apps/providers/oauth2/device_code/
- KeenySpace env vars (`KEENYSPACE_AUTH__OIDC_*`):
  - `OIDC_ISSUER_URL` — Authentik application discovery URL prefix
  - `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`
  - `OIDC_REDIRECT_URI`, `OIDC_POST_LOGOUT_REDIRECT_URI`

## v1 Notes

- KeenySpace is OIDC-protocol-neutral; Authentik is the reference IdP in v0.1.0 alpha.
- Device-code path is delegated to Authentik entirely; KeenySpace ships zero new
  endpoints for device-code in v1 (CONTEXT D-14). Phase 5 CLI calls Authentik
  `/application/o/device/` directly per RFC 8628.
- Authentik device-code provider must emit tokens with the same `audience` and
  `scope` set as the interactive provider — otherwise CLI-minted tokens will
  not validate against KeenySpace middleware.

## Production hardening (DEP-06b)

The dogfood quickstart above is intentionally permissive. Before exposing the stack to
a network, work through every subsection here.

### Secrets

Run the secret generator once, before first boot:

```bash
./deploy/gen-secrets.sh
```

It writes `deploy/.env` (mode 600, gitignored) with `openssl rand` values for all eight
secrets the stack consumes: the KeenySpace and Authentik Postgres passwords, the
Authentik secret key and bootstrap admin password/token, the OIDC client secret, the
session signing key, and the API key pepper.

Never ship the `replace-me` compose defaults. They exist only so the dogfood stack
boots without a `.env` file — every `${VAR:-...replace-me...}` fallback in
`deploy/docker-compose.yml` is overridden by the generated `.env`. The release CI scans
for placeholder values reaching production configs; you should treat any `replace-me`
in a running deployment as an incident.

### Reverse proxy in front of Authentik

KeenySpace itself is fronted by Caddy (`deploy/reverse-proxy/Caddyfile`) or nginx
(`deploy/reverse-proxy/nginx.conf`). When you put Authentik behind a proxy too, give it
a **separate hostname** (e.g. `auth.example.com`), NOT a sub-path on the main domain:

```caddyfile
auth.{$DOMAIN} {
    reverse_proxy authentik:9000
}
```

Sub-path proxying (`example.com/auth/`) changes the OIDC issuer URL embedded in every
token (`iss` claim becomes `https://example.com/auth/application/o/keenyspace/`), and
Authentik's internal redirects do not reliably rewrite under a path prefix. The result
is silent auth breakage: login appears to succeed, then every API call gets 401 with
`auth.token.iss_mismatch` in the server logs.

Whatever URL your users reach Authentik at, the split-horizon issuer variables must
reflect it:

- `KEENYSPACE_AUTH__OIDC_ISSUER_URL` — the PUBLIC issuer URL, exactly as clients see it
  (e.g. `https://auth.example.com/application/o/keenyspace/`). Tokens carry this `iss`.
- `KEENYSPACE_AUTH__OIDC_INTERNAL_ISSUER_URL` — stays
  `http://authentik:9000/application/o/keenyspace/` so the server fetches OIDC
  discovery and JWKS over the compose network. JWKS keys are host-independent, so
  internally fetched keys validate publicly issued tokens.

This is the same split-horizon pattern the dogfood compose uses for
`localhost:9000` / `authentik:9000` — production just swaps the public half for your
real hostname.

### Group entry gate

Restrict server access to members of one Authentik group:

1. Set `KEENYSPACE_AUTH__REQUIRED_GROUP=keenyspace-users` in `deploy/.env` and restart
   the server. Empty (the default) means the gate is disabled.
2. The `keenyspace-users` group and the `groups` scope mapping are already provisioned
   by the blueprint (`deploy/authentik/blueprints/keenyspace.yaml`) — you only need to
   add users to the group: Authentik admin UI > Directory > Groups > keenyspace-users >
   Users > Add existing user.

Behavior:

- OIDC users NOT in the group are rejected at authentication with a plain 401. The
  error deliberately does not name the required group.
- API keys (`ks_live_*`) BYPASS the gate. A key can only be minted by a user who passed
  the gate at mint time, so possession proves admission — and long-running MCP sessions
  survive later IdP group changes. Revoke the key
  (`DELETE /v1/api/auth/api-keys/{id}`) to cut off a holder.

### Branding

The blueprint provisions the login page branding automatically: brand title
"KeenySpace" plus the logo and favicon mounted from `deploy/authentik/branding/` into
the Authentik containers at `/blueprints/custom/branding/`. No manual admin UI work and
no CSS theming — if you need a different logo, replace the SVG files and restart the
Authentik worker to re-apply the blueprint.

## TODO (Phase 7)

- [ ] Step-by-step screenshots for Authentik admin UI
- [x] Group-claim property mapping verification (`groups` scope) — provisioned by
      blueprint and verified live in Phase 7 (DEP-06b)
- [ ] Backup / restore drill for Authentik config alongside KeenySpace backup
