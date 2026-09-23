# Open questions

Решения сознательно отложены первого прохода спеки. Закрытые в дискуссии — убраны из этого списка.

## Workspace model
- Поддерживаем ли nested workspaces (workspace inside workspace) или строго flat? V1 — flat.
- Workspace rename — как обрабатывается active client'ами с локальными pulled vault'ами?

## Authorization (целиком deferred)

Design ролей, ACL, mapping IdP groups → permissions, audit, custom roles — фиксируется после выбора OIDC-провайдера. До того момента в спеке только authentication ([Auth](concepts/rbac-and-auth.md)).

## API surface (deferred to implementation)

Конкретные HTTP routes, MCP tool definitions, request/response schemas, streaming semantics, pagination — designed ближе к реализации. На уровне vision/concept зафиксированы только prefix-конвенция (`/v1/api`, `/v1/mcp`, `/v1/admin`) и shape (FastAPI + FastMCP в одном app).

## Privacy / encryption (отложено)
- Encryption-at-rest для содержимого workspace — workspace-level, all-or-nothing, per-page?
- Содержимое логов часто содержит секреты/код — клиент должен дистиллировать локально или сервер обрабатывает as-is?
- Audit log redaction для compliance use cases?

## Multi-machine / replication
- v2 strategy: git-backed (репликация через git remotes), shared storage, или application-level replication?
- Sync semantics при temporary partitions: optimistic vs. pessimistic?

## Productization
- License choice: MIT / Apache 2.0 / AGPL? Влияет на правила self-hostability и forking.
- Repo structure: monorepo (server + client + helm chart + docs) или split repos?
- Telemetry / analytics — opt-in default off, или вообще отсутствует?
- Plugin system: server-side hooks для third-party extensions?

## Не-цели для напоминания
- Hosted SaaS — не строим.
- Notion-like editor — не строим.
- Built-in vector search в v1 — не строим (поиск только через wikilink traversal).
- Real-time collab cursors — не строим.
- Auto-update клиента — не строим.
- Offline mode для клиента — не в v1.
- Push страниц клиентом — не существует (pages пишет server-side compile из логов).
