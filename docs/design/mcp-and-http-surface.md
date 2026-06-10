---
status: as-built design (v0.1.0-alpha), migrated from concepts/ and verified against the implementation.
---

# MCP and HTTP surface

KeenySpace server is a single ASGI application serving two API surfaces:

- **HTTP** — for the `keenyspace` CLI client, programmatic integrations, and the admin API.
- **MCP** — for LLM agents (Claude Code and other MCP clients).

Both surfaces are mounted in one FastAPI app and share a single auth middleware.

## App composition

```python
from fastapi import FastAPI
from fastmcp import FastMCP

app = FastAPI(title="KeenySpace")
mcp = FastMCP("keenyspace")

mcp_app = mcp.http_app(path="/")
app.mount("/v1/mcp", mcp_app)

# mandatory — without this, StreamableHTTP silently fails on second call
app.router.lifespan_context = combine_lifespans(app_lifespan, mcp_app.lifespan)
```

The `combine_lifespans()` call is required. Without it, FastMCP's StreamableHTTP transport
silently fails after the first request.

Auth middleware is installed on the FastAPI root **before** the MCP mount. This is the
correct order per PrefectHQ/fastmcp issue #1862; reversing it breaks MCP auth.

## URL prefix convention

| Prefix | Purpose |
|--------|---------|
| `/v1/api/*` | Public HTTP API for CLI and integrations (auth, workspaces, pages, logs, compile) |
| `/v1/mcp` | MCP transport for agents (StreamableHTTP) |
| `/v1/admin/*` | Admin API (workspace import, backup, restore; requires `KEENYSPACE_ADMIN_API_ENABLED=1`) |

## MCP tools (Tier-1, v1)

11 Tier-1 tools are registered at v1:

| Tool | Purpose |
|------|---------|
| `list_workspaces` | List all accessible workspaces |
| `get_workspace_info` | Get metadata for a specific workspace |
| `read_page` | Read a single markdown page from a workspace |
| `list_pages` | List pages in a workspace, optionally filtered |
| `search_workspace` | Search page content and filenames by keyword |
| `append_log` | Append a WAL entry to the workspace log |
| `get_instructions` | Fetch server-driven prompt/steps for a named command |
| `list_blueprints` | List available workspace blueprints |
| `get_recent_changes` | Get recently modified pages |
| `compile` | Trigger a server-side WAL compile pass (fire-and-forget) |
| `compile_status` | Poll the status of an in-progress compile |

8 Tier-2 tools (backlinks, orphans, sections, etc.) are deferred to v1.1.

Tool names registered in `mcp/server.py` via `Tool.from_function(fn, name=...)` match the
contract names in the table above exactly. The `*_tool`-suffixed function names are
implementation details; wire names are the bare names.

## Compile tool surface

The server-side compile agent has a restricted tool surface by design:

| Tool available to compile agent | Notes |
|---------------------------------|-------|
| `read_page` | Read existing vault pages |
| `search` | Keyword search over vault filenames and content |

The compile agent does **not** have `write_page`, `fetch`, shell access, or network access.
The coordinator applies the compiled plan (`CompilePlan`) after the agent returns; the agent
itself only reads. WAL entries are wrapped in `<wal_entry>` delimiters with an explicit system
prompt instruction to treat them as data, not instructions. Temperature is hard-coded to 0.

## Key HTTP endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/api/auth/login` | GET | Initiate OIDC device-code flow |
| `/v1/api/auth/callback` | GET | OIDC callback |
| `/v1/api/auth/discovery` | GET | OIDC discovery for CLI login without env config |
| `/v1/api/auth/api-keys` | POST | Mint a new API key |
| `/v1/api/workspaces/` | GET | List workspaces |
| `/v1/api/workspaces/{id}` | GET | Get workspace info |
| `/v1/api/workspaces/{id}/pages/{path}` | GET | Read a page |
| `/v1/api/workspaces/{id}/logs` | POST | Append a WAL entry |
| `/v1/api/workspaces/{id}/compile` | POST | Trigger compile |
| `/healthz` | GET | Liveness probe (no auth required) |
| `/readyz` | GET | Readiness probe (no auth required) |
| `/metrics` | GET | Prometheus metrics |
