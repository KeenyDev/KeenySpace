# OIDC Authentik Setup

Status: Dogfood quickstart section added (Phase 3.1 DEP-06a). Production hardening section added (Phase 7 DEP-06b).

## Dogfood quickstart

This section covers bringing up KeenySpace + Authentik in a local dogfood environment.
No prior Authentik knowledge required. The blueprint auto-provisions the OIDC application
on every startup so you can run `keenyspace login` immediately after `docker compose up`.

**Step 1: Start the full stack**

```
./deploy/gen-secrets.sh
docker compose -f deploy/docker-compose.yml up
```

This starts: KeenySpace, Postgres, Authentik (server + worker), Authentik Postgres, Caddy.
Wait until all services pass their healthchecks (typically 60-90 seconds for Authentik).

**Step 2: Secrets are required**

`deploy/docker-compose.yml` has no fallback values for secrets. Run
`./deploy/gen-secrets.sh` before the first `docker compose up`; it creates
`deploy/.env` with, among others:

- `AUTHENTIK_BOOTSTRAP_PASSWORD` (akadmin initial password)
- `AUTHENTIK_BOOTSTRAP_TOKEN` (admin API token)
- `AUTHENTIK_SECRET_KEY` (session signing key)

Without them compose refuses to start and names the missing variable.

**Step 3: Blueprint auto-provision evidence**

The `authentik-worker` service applies `deploy/authentik/blueprints/keenyspace.yaml`
on every startup. This idempotently provisions:

- OAuth2 provider `keenyspace-cli` (public client, device-code enabled, per_provider issuer)
- Application `keenyspace` (slug: keenyspace)
- Brand device-code flow enabled
- Groups `keenyspace-users` (entry gate) and `keenyspace-admins` (admin API), plus the
  `groups` scope mapping that puts group names into tokens
- `akadmin` as a member of both groups, but only if `akadmin` already exists and is
  still in `authentik Admins`. The blueprint never creates `akadmin` and never re-grants
  superuser to an `akadmin` you demoted.

The `akadmin` entry writes the user's full group set on every apply: `authentik Admins`,
`keenyspace-users` and `keenyspace-admins`. Authentik blueprints have no "add one
membership" operation (a user's `groups` attribute is always replaced as a whole), so
this has two consequences:

- Any other group you add to `akadmin` by hand is dropped the next time the worker
  applies the blueprint (every startup and every change to the file). Manage extra
  memberships on other accounts, not on `akadmin`.
- Removing `akadmin` from `keenyspace-admins` in the admin UI does not last: the next
  apply adds it back. To take KeenySpace admin rights away from `akadmin`, either delete
  the `authentik_core.user` entry for `akadmin` from
  `deploy/authentik/blueprints/keenyspace.yaml` (then remove the membership in the UI),
  or remove `akadmin` from `authentik Admins`, which makes the entry's condition false so
  it stops touching `akadmin` altogether.

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

The compose file interpolates every secret as `${VAR:?run deploy/gen-secrets.sh}`, so a
missing value fails `docker compose` immediately instead of silently booting with a
well-known default. The script only appends missing keys and never overwrites existing
values. Existing installs that previously relied on the old `replace-me` defaults must
carry the database passwords and `AUTHENTIK_SECRET_KEY` over into `deploy/.env` first;
see "Existing installs" in [docs/install.md](install.md). Treat any `replace-me` value
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

1. The compose file enables the gate by default with
   `KEENYSPACE_AUTH__REQUIRED_GROUP=keenyspace-users`. Override the group name in
   `deploy/.env` if you use a different one.
2. The `keenyspace-users` group and the `groups` scope mapping are already provisioned
   by the blueprint (`deploy/authentik/blueprints/keenyspace.yaml`) — you only need to
   add users to the group: Authentik admin UI > Directory > Groups > keenyspace-users >
   Users > Add existing user.

Behavior:

- OIDC users NOT in the group are rejected at authentication with a plain 401. The
  error deliberately does not name the required group.
- Tokens must carry the `groups` claim, so clients must request the `groups` scope. The
  `keenyspace` CLI (`keenyspace login`) requests `openid profile email groups`. A token
  without the claim counts as "no groups" and is rejected by the gate.
- API keys (`ks_live_*`) are gated too. A key carries no IdP claims, so the server
  checks it against its owner's group snapshot: the groups from the owner's most recent
  OIDC token that carried a `groups` claim, stored in `users.groups` and stamped with
  that token's issue time (`iat`). Only a token issued later than the stored snapshot
  replaces it, and a changed group set takes effect for the owner's keys immediately
  (verified keys are cached for at most 60 seconds, and the cache is dropped when the
  snapshot changes). A still-valid token issued before the newest snapshot is authorized
  with the snapshot's groups, not its own, so an old token cannot restore a group the
  user has since lost.
- A key whose owner has never authenticated via OIDC since the snapshot was introduced
  has no snapshot and is rejected with 401 (`auth.group_gate.denied`,
  `reason=no_group_snapshot` in the server logs). The fix is one OIDC request by the
  owner, e.g. `keenyspace login` followed by any command, or an MCP client signing in
  via OAuth.
- The snapshot only refreshes when the owner uses OIDC. A user removed from the group in
  Authentik who only ever uses API keys keeps the old snapshot. To bound that window,
  set `KEENYSPACE_AUTH__API_KEY_GROUP_SNAPSHOT_MAX_AGE_DAYS` in `deploy/.env` (unset by
  default): keys whose owner has not presented a `groups` claim via OIDC within that
  many days are refused (`reason=group_snapshot_stale`). Owners then have to log in
  periodically.

### Admin group

`/v1/admin/*` (backup, restore, `api-keys/revoke-all`) is mounted only when
`KEENYSPACE_ADMIN_API_ENABLED=1`, and every route additionally requires membership in
`KEENYSPACE_AUTH__ADMIN_GROUP` (default `keenyspace-admins`). Non-members get 403. As
with the entry gate, OIDC callers are checked against their token's `groups` claim and
API keys against the owner's snapshot. Set a different group name in `deploy/.env`; an
empty value falls back to `keenyspace-admins`. To disable the admin API, set
`KEENYSPACE_ADMIN_API_ENABLED=0`. Procedures: [docs/backup-restore.md](backup-restore.md).

### API key expiry

Keys do not expire by default. To mint an expiring key, pass `expires_in_days` (1 to
3650) to `POST /v1/api/auth/api-keys`:

```bash
curl -sS -X POST http://localhost:8000/v1/api/auth/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "ci-agent", "expires_in_days": 90}'
```

Mint and list responses include `expires_at` (`null` for keys without expiry). An expired
key is rejected with 401. Minting and revoking a key are recorded in the audit log.

Minting requires an OIDC access token (`keenyspace login`, the browser session, or an MCP
OAuth sign-in). An API key cannot mint keys (403), so a leaked or expiring key cannot
create a longer-lived successor; `keenyspace token create` therefore needs a device-flow
login, not a `--pat` login. Minting also returns 403 when the token was issued before the
owner's newest group snapshot (for example another client signed in later, or an admin
ran revoke-all); log in again to get a fresh token.

### Offboarding a user

1. Remove the user from `keenyspace-users` (and `keenyspace-admins`, if applicable) in
   the Authentik admin UI, or deactivate the account. Newly issued tokens no longer
   carry the group; an access token issued before the change keeps passing (with its
   old groups, unless a newer snapshot exists) until it expires.
2. Revoke all of the user's API keys, because an API-key-only user keeps their old
   group snapshot. Revoke-all also replaces the user's snapshot with an empty one
   stamped with the current time: access tokens issued before it are then authorized
   with no groups and cannot mint new keys. With the admin API enabled and as a member
   of the admin group:

   ```bash
   curl -sS -X POST http://localhost:8000/v1/admin/api-keys/revoke-all \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"sub": "<user sub>"}'
   ```

   `sub` is the user's OIDC subject, the same value stored as `user_sub` on their keys.
   The response is `{"sub": "...", "revoked": <count>}`, and the call is recorded in
   the audit log as `admin.api_keys.revoked_all`.

A single key can still be revoked by its owner with `DELETE /v1/api/auth/api-keys/{id}`.

### Bearer tokens must be access tokens

`Authorization: Bearer <jwt>` accepts only OIDC access tokens. The server rejects JWTs
without a `scope` claim, which excludes the ID token Authentik returns alongside the
access token (same signer, issuer and audience). A client that sends its ID token gets
401, with `auth.token.not_access_token` in the server logs.

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
