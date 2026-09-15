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
(`openssl rand`) for every secret the stack needs: Postgres passwords, the Authentik
secret key and bootstrap admin credentials, the OIDC client secret, the session
signing key, and the API key pepper.

**Important:**

- The file is created with mode 600 and is gitignored (`deploy/.gitignore`). Never commit it.
- The script refuses to overwrite an existing `deploy/.env` — delete the file first if you intend to regenerate.
- Without a generated `.env`, the compose file falls back to `replace-me` placeholder
  defaults. Those are acceptable only for a throwaway local sandbox, never for anything
  reachable from a network.

## 3. Configure

`deploy/docker-compose.yml` is the source of truth for every environment variable.
Operator-facing settings go into `deploy/.env` (the compose file reads it via `env_file`
and `${VAR:-default}` substitution). The ones you will most likely set:

| Variable | Purpose | Default |
|----------|---------|---------|
| `DOMAIN` | Public hostname for Caddy. Set a real domain (e.g. `keenyspace.example.com`) to enable automatic HTTPS via Let's Encrypt. Unset means plain HTTP on `localhost:80`. | `localhost:80` |
| `KEENYSPACE_AUTH__REQUIRED_GROUP` | OIDC entry gate. Set to `keenyspace-users` to require membership in that Authentik group for login. Empty disables the gate. API keys bypass it (they are minted by an already-authorized user). | empty (disabled) |
| `KEENYSPACE_COMPILE__PROVIDER` | LLM provider for the server-side compile agent (`anthropic` or `openai`). | `anthropic` |
| `KEENYSPACE_COMPILE__MODEL` | Model used by the compile agent. | `claude-sonnet-4-6` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | API key for the chosen compile provider. The compile pipeline does not run without it. | empty |

Append your settings to `deploy/.env`:

```bash
cat >> deploy/.env <<'EOF'
DOMAIN=keenyspace.example.com
KEENYSPACE_AUTH__REQUIRED_GROUP=keenyspace-users
ANTHROPIC_API_KEY=sk-ant-...
EOF
```

If you enable the group gate, you must also add your users to the `keenyspace-users`
group in Authentik after first boot — see the production hardening section of
[docs/oidc-authentik-setup.md](oidc-authentik-setup.md).

For a production deployment behind a real domain, also review the split-horizon OIDC
issuer variables (`KEENYSPACE_AUTH__OIDC_ISSUER_URL` must match the URL your users
reach Authentik at) in the same hardening section.

## 4. Start the stack

```bash
docker compose -f deploy/docker-compose.yml up -d
```

The first run builds the KeenySpace image from source and pulls the pinned images for
Postgres 17, Authentik 2026.2, Redis, and Caddy. Authentik takes 60-90 seconds to become
healthy on first boot; the KeenySpace server waits for it (`depends_on` healthchecks).

### Volumes

All persistent state lives in named Docker volumes:

| Volume | Holds | Back up? |
|--------|-------|----------|
| `keenyspace-fs` | The canon markdown workspaces (your actual knowledge graphs) | Yes — covered by `keenyspace backup` |
| `postgres-data` | KeenySpace Postgres: workspace registry, users, hashed API keys, audit log, compile cursors | Yes — covered by `keenyspace backup` |
| `authentik-postgresql` | Authentik database: users, groups, providers | Yes — IdP state, not covered by `keenyspace backup` |
| `authentik-media`, `authentik-templates`, `authentik-certs` | Authentik media, templates, certificates | Recommended |
| `authentik-redis` | Authentik task queue cache | No (rebuildable) |
| `caddy-data`, `caddy-config` | Let's Encrypt certificates and Caddy state | Recommended (avoids re-issuing certificates) |

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
- **Single worker:** KeenySpace v1 runs single-worker uvicorn by design. Do not add
  replicas or `--workers` flags.

## 8. Next steps

- Wire an MCP client (Claude Code) to your server: [docs/mcp-setup.md](mcp-setup.md)
- Set up backups before you put real data in: [docs/backup-restore.md](backup-restore.md)
- Production hardening checklist: [docs/oidc-authentik-setup.md](oidc-authentik-setup.md)
