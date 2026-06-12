---
status: as-built design (v0.1.0-alpha), migrated from concepts/ and verified against the implementation.
---

# Client model

The `keenyspace` CLI client is a Python package installed from the cloned repo via
`uv tool install --from ./packages/client keenyspace`. It provides:

- CLI commands for interacting with workspaces.
- A background daemon for hook delivery and periodic tasks.
- Claude Code hooks integration.
- **Server-driven behavior**: command logic (prompts, steps, instructions) is fetched from
  the server via MCP at runtime. Prompts and instructions can be updated server-side without
  releasing a new client version.

## CLI commands

### Setup / lifecycle

| Command | Description |
|---------|-------------|
| `keenyspace init` | Initial setup: prompts for server URL, starts login |
| `keenyspace login` | OIDC device-code flow, stores session in `~/.config/keenyspace/auth.json` |
| `keenyspace logout` | Clear local session |
| `keenyspace status` | Server reachability, current user, default workspace, daemon status |

### Workspace operations

| Command | Description |
|---------|-------------|
| `keenyspace workspace list` | List accessible workspaces |
| `keenyspace workspace create --blueprint default --name foo` | Clone a blueprint |
| `keenyspace workspace use <slug>` | Set default workspace |
| `keenyspace workspace register` | Register current directory as a workspace by path |
| `keenyspace workspace pull` | Pull workspace content to local disk (`~/keenyspace/<slug>/`) |

### Content operations

| Command | Description |
|---------|-------------|
| `keenyspace ingest <path>` | Feed a file or directory into the workspace WAL |
| `keenyspace query "question"` | Q&A over the workspace (server-driven, LLM) |
| `keenyspace lint` | Workspace health check (dead wikilinks, missing pages) |
| `keenyspace compile [--wait]` | Trigger server-side compile (WAL to pages) |
| `keenyspace compile status` | Check compile job status |

### Hooks

| Command | Description |
|---------|-------------|
| `keenyspace hook session-start` | Claude Code SessionStart hook handler |
| `keenyspace hook session-end` | Claude Code SessionEnd hook handler |
| `keenyspace hook pre-compact` | Claude Code PreCompact hook handler |
| `keenyspace hook post-tool` | Claude Code PostToolUse hook handler |
| `keenyspace hooks install` | Install hooks into Claude Code settings |
| `keenyspace hooks uninstall` | Remove hooks from Claude Code settings |
| `keenyspace hooks status` | Show current hook installation status |

### Daemon

| Command | Description |
|---------|-------------|
| `keenyspace daemon start` | Start the background daemon |
| `keenyspace daemon stop` | Stop the background daemon |
| `keenyspace daemon status` | Show daemon status |
| `keenyspace service install` | Install and enable the launchd (macOS) or systemd user service |

## Client configuration

Config lives in `~/.config/keenyspace/` (macOS and Linux):

- `config.yaml` — server URL, default workspace, log level.
- `auth.json` — API key or OIDC session tokens (file mode 0600).
- `llm.env` — LLM provider configuration (gitignored).

```yaml
# config.yaml example
server: https://keeny.example.com
default_workspace: platform-research
log_level: info
```

## Server-driven model

Each non-trivial command follows this pattern:

```
1. Client → MCP get_instructions(workspace, command_name, context)
2. Server returns {prompt, steps, mcp_tools_to_use}
3. Client runs a local LLM call with the returned prompt
4. LLM calls MCP tools (read_page, append_log, search_workspace, ...)
5. Server validates auth and applies each tool call
```

New prompts deploy server-side only. The client does not need to be updated to get improved
behavior.

The LLM provider for client-side calls is configurable: Anthropic (default), OpenAI,
or any OpenAI-compatible endpoint. Each user provides their own API key.

## Background daemon

The daemon handles hook event delivery and periodic workspace tasks:

- **macOS**: launchd user agent installed by `keenyspace service install`.
- **Linux**: systemd user service installed by `keenyspace service install`.
- The daemon receives hook events forwarded by hook commands and delivers them asynchronously.
- A kill switch file at `~/.config/keenyspace/disabled` causes the daemon to exit immediately on start.

## Claude Code hooks integration

Hooks are registered in Claude Code settings (either project-scoped
`.claude/settings.local.json` or global):

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "keenyspace hook session-start"}]}],
    "SessionEnd":   [{"hooks": [{"type": "command", "command": "keenyspace hook session-end"}]}],
    "PreCompact":   [{"hooks": [{"type": "command", "command": "keenyspace hook pre-compact"}]}],
    "PostToolUse":  [{"hooks": [{"type": "command", "command": "keenyspace hook post-tool"}]}]
  }
}
```

Workspace routing from within a hook is determined by the current working directory via
`keenyspace workspace from-cwd` (uses a registered workspace map). Events for unregistered
paths are dropped to `dropped.json` for inspection.

## Workspace pull semantics

`keenyspace pull` refuses to overwrite local changes without `--force`. The `.obsidian/`
directory is excluded from the pull (server canon does not include it). Local Obsidian edits
are ephemeral — they will be overwritten by the next pull. Users who want to contribute
content should use `keenyspace ingest` or MCP tools, not direct Obsidian edits.
