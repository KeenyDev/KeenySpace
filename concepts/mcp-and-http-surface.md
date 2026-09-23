# MCP + HTTP surface

KeenySpace server — единое ASGI-приложение, обслуживающее два API surface:
- **HTTP** — для клиента, admin web, programmatic интеграций.
- **MCP** — для агентов (Claude и другие MCP-клиенты).

Оба смонтированы в один FastAPI app, версионирование через URL prefix.

## App composition

```python
# illustrative; точный API confirmed during implementation
from fastapi import FastAPI
from fastmcp import FastMCP

app = FastAPI(title="KeenySpace")
mcp = FastMCP("keenyspace")

# mount FastMCP as ASGI sub-app
app.mount("/v1/mcp", mcp.asgi_app())
```

FastMCP экспонирует ASGI-совместимый app, монтируемый через стандартный FastAPI/Starlette `mount`. Один process, один port, один TLS endpoint.

## URL prefix convention

| Prefix | Назначение |
|---|---|
| `/v1/api/*` | Public HTTP API для клиентского приложения и интеграций (auth, workspaces, pages, logs). |
| `/v1/mcp` | MCP transport для агентов. |
| `/v1/admin/*` | Admin HTTP API (управление users, blueprints, server-level операции). |

Версионирование через `/v1/`; будущие breaking changes — в `/v2/` параллельно.

## Detailed inventory deferred

Конкретный список routes (HTTP) и MCP tools проектируется ближе к реализации. На уровне vision/concept зафиксированы только:
- Высокоуровневый shape (HTTP + MCP в одном app).
- Префиксы версионирования.
- Принципы authn: каждый запрос валидируется через единый middleware (см. [Auth](rbac-and-auth.md)) до попадания в storage layer.

Финальная surface (request/response schemas, MCP tool definitions, streaming semantics) — отдельная итерация спеки в фазе implementation.

## Open questions

См. [open-questions.md](../open-questions.md).
