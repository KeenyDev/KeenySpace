# KeenySpace

**v0.1.0-alpha**

KeenySpace is a self-hosted, open-source knowledge graph platform for teams and LLM agents.
Each workspace is a plain Obsidian-compatible directory of markdown files. A lightweight server
adds authentication, multi-user access control, and first-class MCP (Model Context Protocol)
support so LLM agents can safely read and append to the shared knowledge graph.

The design principle: markdown files are canon. The server is a sync, permission, and query
layer. Losing the server means falling back to single-user mode on the same files — no data
loss. Agents connect via MCP using long-lived API keys; humans connect via OIDC and the
`keenyspace` CLI. Server-side WAL compile turns accumulated log entries into wiki pages through
a pydantic-ai + instructor agent, keeping all LLM logic on the server where it can be updated
without upgrading clients.

## Quickstart

Requirements: Docker, Docker Compose, and an OIDC provider (the reference setup uses
Authentik, included in the compose stack).

```sh
git clone https://github.com/uber910/KeenySpace.git
cd KeenySpace
./deploy/gen-secrets.sh
docker compose -f deploy/docker-compose.yml up -d
```

After containers are healthy (check with `docker compose -f deploy/docker-compose.yml ps`):

```sh
uv tool install --from ./packages/client keenyspace
keenyspace login
```

See [docs/install.md](docs/install.md) for detailed setup, environment variables, and volume
configuration. See [docs/oidc-authentik-setup.md](docs/oidc-authentik-setup.md) for
configuring Authentik as the reference IdP. See [docs/mcp-setup.md](docs/mcp-setup.md) for
wiring Claude Code or another MCP client to your workspace.

## What it is / is not

**What it is:**

- Single-org, single-host knowledge graph server (v1)
- AGPL-3.0 licensed, self-hosted only
- Deployed via docker-compose (Helm chart deferred to v1.1)
- Uses Authentik as the reference OIDC identity provider; any standards-compliant OIDC
  provider works at the protocol level
- Uses Anthropic as the default LLM provider for server-side compile; OpenAI is opt-in via
  environment variable

**What it is not:**

- No hosted SaaS offering
- No vector search or embedding-based retrieval (v1 navigation is wikilink traversal)
- No real-time collaborative cursors (non-goal)
- Not a general-purpose CMS or publishing platform

## Documentation

| Document | Description |
|----------|-------------|
| [docs/install.md](docs/install.md) | Full installation guide, environment variables, volumes |
| [docs/oidc-authentik-setup.md](docs/oidc-authentik-setup.md) | Authentik IdP setup guide |
| [docs/mcp-setup.md](docs/mcp-setup.md) | Connecting Claude Code or any MCP client |
| [docs/upgrade.md](docs/upgrade.md) | Version upgrade procedure and backup-before-upgrade gate |
| [docs/backup-restore.md](docs/backup-restore.md) | Backup and restore operational procedure |
| [docs/design/](docs/design/) | As-built design documentation |

## License

KeenySpace is licensed under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0).

## Contributing

Contributions are welcome. All commits must include a `Signed-off-by` trailer (`git commit -s`).
See [CONTRIBUTING.md](CONTRIBUTING.md) for the full DCO sign-off requirement, development
setup, and pull request checklist.
