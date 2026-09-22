# Installing KeenySpace

This guide walks through a production-grade install of the full KeenySpace stack
(KeenySpace server, Postgres 17, Authentik IdP, Caddy reverse proxy) with docker compose.
Target time from clone to first login: under 60 minutes on a fresh machine.

## 1. Prerequisites

- Docker Engine with Compose v2 (`docker compose version` must work; Compose v1 `docker-compose` is not supported)
- git
- ~2 GB of free RAM for Authentik alone; 4 GB total is a comfortable minimum for the whole stack
- A Python 3.14 toolchain with [uv](https://docs.astral.sh/uv/) for the `keenyspace` CLI client
- Optional for HTTPS: a DNS record pointing at the host (Caddy provisions Let's Encrypt certificates automatically when `DOMAIN` is set)

## 2. Clone and generate secrets

```bash
git clone https://github.com/KeenyDev/KeenySpace.git
cd KeenySpace
./deploy/gen-secrets.sh
```

`gen-secrets.sh` writes `deploy/.env` with cryptographically random values
(`openssl rand`) for every secret the stack needs: `POSTGRES_PASSWORD`,
`AUTHENTIK_DB_PASSWORD`, `AUTHENTIK_SECRET_KEY`, `AUTHENTIK_BOOTSTRAP_PASSWORD`,
`AUTHENTIK_BOOTSTRAP_TOKEN`, `KEENYSPACE_OIDC_CLIENT_SECRET`,
`KEENYSPACE_SESSION_SECRET_KEY` and `KEENYSPACE_API_KEY_PEPPER`.

**Important:**

- The file is created with mode 600 and is gitignored. Never commit it.
- The script is idempotent: if `deploy/.env` already exists it only appends the keys
  that are missing and never overwrites an existing value. Re-running it is safe.
- There are no fallback defaults. If any of these variables is missing, every
  `docker compose` command against `deploy/docker-compose.yml` fails before touching
  containers, e.g.
  `required variable AUTHENTIK_DB_PASSWORD is missing a value: run deploy/gen-secrets.sh`.
- Compose reads `deploy/.env` automatically because it sits next to the compose file.
  If you keep secrets elsewhere, pass `--env-file <path>` explicitly.

### Existing installs (upgrading from a compose file with `replace-me` defaults)

Earlier versions of the compose file fell back to built-in defaults when a variable was
unset. Some of those values are baked into your existing volumes: the Postgres roles
were created with `POSTGRES_PASSWORD` / `AUTHENTIK_DB_PASSWORD`, and Authentik signs
its data with `AUTHENTIK_SECRET_KEY`. Generating fresh values for them would lock the
stack out of its own databases.

Before you pull this change and run `gen-secrets.sh`, write the values your stack is
actually running with into `deploy/.env`. If you never had a `deploy/.env`, or it lacks
some of these keys, your stack runs on the previous defaults:

```bash
cat >> deploy/.env <<'EOF'
POSTGRES_PASSWORD=devpw
AUTHENTIK_DB_PASSWORD=authentik-db-password-replace-me
AUTHENTIK_SECRET_KEY=authentik-secret-key-replace-me-50-bytes-padding-padding-padding
EOF
chmod 600 deploy/.env
./deploy/gen-secrets.sh   # fills in only the keys that are still missing
```

Only add the lines for keys that are not already in your `deploy/.env`. For the
remaining secrets, decide whether to carry over the old value or generate a new one:

- `KEENYSPACE_API_KEY_PEPPER`: a new value **invalidates every existing `ks_live_*`
  API key**. To keep existing keys working, carry over the previous value (default
  `api-key-pepper-replace-me-min-32-bytes-xxxxxx`); otherwise re-mint keys after the
  upgrade.
- `KEENYSPACE_SESSION_SECRET_KEY`: a new value only invalidates in-flight login
  sessions (users repeat the login flow).
- `AUTHENTIK_BOOTSTRAP_PASSWORD` / `AUTHENTIK_BOOTSTRAP_TOKEN`: only applied when
  Authentik bootstraps an empty database, so new values do not change akadmin's
  current password on an existing install.
- `KEENYSPACE_OIDC_CLIENT_SECRET`: the shipped `keenyspace-cli` client is public, so
  the value is not checked by Authentik.

The server logs `config.secret.placeholder` at startup for any secret that still
contains `replace-me`. Treat the previous defaults as compromised if the stack was ever
reachable from a network, and rotate them deliberately once the upgrade is stable.

## 3. Configure

`deploy/docker-compose.yml` is the source of truth for every environment variable.
Configuration lives in two gitignored files next to it:

- `deploy/.env`: secrets plus the variables the compose file references as `${VAR}`
  (the table below). Compose only uses it for substitution; it is **not** loaded into
  any container as a whole, so the KeenySpace container never sees the Postgres or
  Authentik secrets it does not need.
- `deploy/keenyspace.env` (optional): app-only `KEENYSPACE_*` overrides such as compile
  budgets, the backstop interval or the log level, loaded into the KeenySpace container
  only. Start from `deploy/keenyspace.env.example`. Variables set explicitly under
  `environment:` in the compose file cannot be overridden from here; set those in
  `deploy/.env`.

The `deploy/.env` settings you will most likely set:

| Variable | Purpose | Default |
|----------|---------|---------|
| `DOMAIN` | Public hostname for Caddy. Set a real domain (e.g. `keenyspace.example.com`) to enable automatic HTTPS via Let's Encrypt. Unset means plain HTTP on `localhost:80`. | `localhost:80` |
| `KEENYSPACE_AUTH__REQUIRED_GROUP` | OIDC entry gate: only members of this Authentik group can log in. API keys bypass it (they are minted by an already-authorized user). | `keenyspace-users` |
| `KEENYSPACE_AUTH__ADMIN_GROUP` | Authentik group whose members get administrative rights on the server. | `keenyspace-admins` |
| `KEENYSPACE_METRICS_PORT` | Internal Prometheus listener inside the container. Not published to the host. `0` disables it. | `9100` |
| `KEENYSPACE_COMPILE__PROVIDER` | LLM provider for the server-side compile agent (`anthropic` or `openai`). | `anthropic` |
| `KEENYSPACE_COMPILE__MODEL` | Model used by the compile agent. | `claude-sonnet-4-6` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | API key for the chosen compile provider. The compile pipeline does not run without it. | empty |

Append your settings to `deploy/.env`:

```bash
cat >> deploy/.env <<'EOF'
DOMAIN=keenyspace.example.com
ANTHROPIC_API_KEY=sk-ant-...
EOF
```

For app tuning, create the override file:

```bash
cp deploy/keenyspace.env.example deploy/keenyspace.env
chmod 600 deploy/keenyspace.env
# uncomment e.g. KEENYSPACE_COMPILE__MAX_OUTPUT_TOKENS_PER_SPACE=...
```

**Existing installs:** earlier versions loaded all of `deploy/.env` into the KeenySpace
container. Move every `KEENYSPACE_*` override line from `deploy/.env` to
`deploy/keenyspace.env`, except the ones listed in the table above
(`KEENYSPACE_AUTH__REQUIRED_GROUP`, `KEENYSPACE_AUTH__ADMIN_GROUP`,
`KEENYSPACE_METRICS_PORT`, `KEENYSPACE_COMPILE__PROVIDER`, `KEENYSPACE_COMPILE__MODEL`,
`KEENYSPACE_ADMIN_API_ENABLED`) and the secrets, which stay in `deploy/.env`. Overrides
left behind in `deploy/.env` are silently ignored.

The group gate is on by default: OIDC users who are not members of `keenyspace-users`
are rejected at login. Add your users to that group in Authentik after first boot (see
the production hardening section of [docs/oidc-authentik-setup.md](oidc-authentik-setup.md)).
Existing installs that ran without the gate must add their users to the group before
upgrading, or those users lose OIDC login (existing API keys keep working).

For a production deployment behind a real domain, also review the split-horizon OIDC
issuer variables (`KEENYSPACE_AUTH__OIDC_ISSUER_URL` must match the URL your users
reach Authentik at) in the same hardening section.

## 4. Start the stack

```bash
docker compose -f deploy/docker-compose.yml up -d
```

The first run builds the KeenySpace image from source and pulls the pinned images for
Postgres 17, Authentik 2026.2 (with its own Postgres 16), and Caddy. Authentik takes 60-90 seconds to become
healthy on first boot; the KeenySpace server waits for it (`depends_on` healthchecks).

### Network exposure

Only Caddy (ports 80 and 443) listens on all host interfaces; it is the public entry
point. Everything else is published on `127.0.0.1` only, or not at all:

| Port | Service | Reachable from |
|------|---------|----------------|
| 80, 443 | Caddy | the network |
| 8000 | KeenySpace API (direct, bypassing Caddy) | the host only |
| 9000, 9443 | Authentik | the host only |
| 5432 | KeenySpace Postgres | the host only |
| 9100 | Prometheus metrics | the compose network only (not published) |

The shipped issuer URL (`http://localhost:9000/...`) therefore only works for clients
on the same host. To let remote users log in, put Authentik behind the reverse proxy on
its own hostname and set `KEENYSPACE_AUTH__OIDC_ISSUER_URL` accordingly (see
"Reverse proxy in front of Authentik" in [docs/oidc-authentik-setup.md](oidc-authentik-setup.md)).

### Volumes

All persistent state lives in named Docker volumes:

| Volume | Holds | Back up? |
|--------|-------|----------|
| `keenyspace-fs` | The canon markdown workspaces (your actual knowledge graphs) | Yes — covered by `keenyspace backup` |
| `postgres-data` | KeenySpace Postgres: workspace registry, users, hashed API keys, audit log, compile cursors | Yes — covered by `keenyspace backup` |
| `authentik-postgresql` | Authentik database: users, groups, providers | Yes — IdP state, not covered by `keenyspace backup` |
| `authentik-media`, `authentik-templates`, `authentik-certs` | Authentik media, templates, certificates | Recommended |
| `caddy-data`, `caddy-config` | Let's Encrypt certificates and Caddy state | Recommended (avoids re-issuing certificates) |

Authentik 2025.10 and later no longer use Redis, so the stack has no Redis service.
Installs upgraded from an older compose file still have an orphaned `authentik-redis`
container and volume. Remove the container with
`docker compose -f deploy/docker-compose.yml up -d --remove-orphans`. The leftover
volume holds only disposable cache data; delete it with `docker volume rm` once the
upgraded stack is healthy.

`docker compose down -v` destroys all of these. See [docs/backup-restore.md](backup-restore.md)
before doing anything destructive.

## 5. Verify

Wait until every service reports healthy:

```bash
docker compose -f deploy/docker-compose.yml ps
```

Then check the server directly and through Caddy:

```bash
curl http://localhost:8000/healthz
curl http://localhost/healthz
```

Both must return HTTP 200. If the direct check passes but the Caddy check fails, Caddy
has not finished starting or `DOMAIN` points somewhere unexpected — check
`docker compose -f deploy/docker-compose.yml logs caddy`.

## 6. First login

Install the CLI client from the cloned repo:

```bash
uv tool install --from ./packages/client keenyspace
```

(Alternatively, run it without installing: `uv run keenyspace --help` from the repo root.)

Configure the server URL and log in:

```bash
keenyspace init
```

The wizard prompts for the server URL (`https://keenyspace.example.com`, or
`http://localhost` for a local install) and then starts the OIDC device-code login flow
against Authentik. Log in with the bootstrap admin: username `akadmin`, password is the
`AUTHENTIK_BOOTSTRAP_PASSWORD` value from `deploy/.env`.

For Authentik details (blueprint auto-provisioning, device-code flow, troubleshooting),
see [docs/oidc-authentik-setup.md](oidc-authentik-setup.md).

Smoke test the session:

```bash
keenyspace workspace list
```

## 7. Caveats

- **macOS Docker Desktop and log scraping:** the opt-in observability addon
  (`deploy/observability.yml`) ships Promtail, which reads container logs from
  `/var/lib/docker/containers`. On macOS Docker Desktop that path lives inside the
  Docker VM, not on the host filesystem, so log scraping into Loki only works on Linux
  hosts. Metrics (Prometheus + Grafana) work everywhere.
- **Metrics are internal:** Prometheus scrapes `keenyspace:9100` over the compose
  network; `/metrics` is not served on the API port. Start the addon in the same
  compose project as the stack:
  `docker compose -f deploy/docker-compose.yml -f deploy/observability.yml up -d`.
- **Single worker:** KeenySpace v1 runs single-worker uvicorn by design. Do not add
  replicas or `--workers` flags.

## 8. Next steps

- Wire an MCP client (Claude Code) to your server: [docs/mcp-setup.md](mcp-setup.md)
- Set up backups before you put real data in: [docs/backup-restore.md](backup-restore.md)
- Production hardening checklist: [docs/oidc-authentik-setup.md](oidc-authentik-setup.md)
