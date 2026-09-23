# Vision

## Что такое KeenySpace

KeenySpace — self-hosted opensource система для построения и совместного использования knowledge graph'ов между людьми и LLM-агентами. Каждый граф хранится как набор markdown-файлов в директории, совместимой с Obsidian; над файлами стоит сервер с RBAC, multi-tenancy и MCP-интерфейсом для агентов.

Грубо: «Obsidian vault как managed multi-user resource с auth и MCP-доступом для агентов».

## Проблема

Современные agent-driven workflows (Claude Code, Cursor, Aider, кастомные агенты) генерируют большое количество разрозненной семантической информации: суммаризации сессий, найденные конструкции, принятые решения, инциденты. Эта информация теряется между сессиями.

Существующие инструменты не закрывают сценарий:

- **Notion / Confluence / Outline** — построены под человеческое авторство; агенты могут писать через API, но семантика wiki (категории, перекрёстные ссылки, frontmatter-конвенции) не выражена в schema, а данные привязаны к SaaS.
- **Obsidian / Logseq / Foam** — single-user, локальные, без auth/RBAC. Не годятся для команды.
- **Vector DB / RAG** — embeddings без human-readable layer; невозможно прочитать глазами или поправить.
- **Self-hosted markdown wikis (BookStack, Wiki.js)** — human-first, без MCP-доступа агентам, без graph-семантики Zettelkasten.

Что нужно: продукт, в котором markdown-файлы канон, structure графа first-class, auth работает по-взрослому, и агенты получают доступ через MCP так же натурально, как через файловую систему.

## Почему сейчас

- **MCP стал стандартом** (2024-2025). Server-side knowledge becomes доступным агенту как набор tools без custom wiring.
- **Multi-agent workflows** требуют shared memory между сессиями и ролями.
- **Self-hosted ethos** востребован: data-sensitive команды (security, healthcare, enterprise R&D) не отдают knowledge базу в SaaS.

## Целевая аудитория

- **Small / mid teams** (3-30 чел), запускающие свою инфру и ценящие control over data: research labs, security teams, internal platform teams.
- **Data-sensitive организации**: legal, healthcare, financial, R&D — те, кто не отдаёт корпоративную память в SaaS.
- **Opensource self-hosters** в духе Outline / Gitea / Forgejo deployer'ов.
- **Multi-agent workflow operators**: команды, гоняющие несколько агентов параллельно и нуждающиеся в shared semantic memory.

Не аудитория: solo Obsidian-пользователи (overkill), publishing-ориентированные wiki (Wiki.js справляется), Notion-pivoting стартапы (мы не feature-parity конкурент).

## Ethos

- **Markdown primary**. Файлы — source of truth; БД, индексы, embeddings — derived. Любой markdown-редактор работает с workspace без сервера.
- **Obsidian-compatible**. Workspace = vault. `[[wikilinks]]`, frontmatter, `_templates/`, `.obsidian/` — всё как ожидается.
- **No vendor lock-in**. Потерять сервер и остаться с папкой markdown — не потеря, а откат к single-user режиму.
- **External identity**. Не своя auth-система — OIDC через Ory / Keycloak / Authentik / Zitadel. Один identity provider на всю инфру пользователя.
- **Server-driven thin client**. Логика команд клиента живёт на сервере (промпты, инструкции). Клиент — тонкий runtime, обновлять часто не надо.
- **Linux-style governance**. Opensource (license TBD), self-hosted-first, без built-in billing, без telemetry по умолчанию.

## Non-goals

- **Hosted SaaS**. Можно построить поверх, но не цель.
- **Notion-replacement**. Нет real-time collab cursors, databases-as-tables, embed-everything.
- **General-purpose CMS**. KeenySpace — knowledge graph, не публикационная платформа.
- **Built-in vector search / embeddings** в v1. Network-as-search принцип (поиск через wikilink-навигацию).
- **Code wiki / docs site generator**. Если нужно публиковать docs из репо — есть mkdocs / docusaurus.

## Связанные документы

- [Workspace model](concepts/workspace-model.md)
- [RBAC и auth](concepts/rbac-and-auth.md)
- [MCP + HTTP surface](concepts/mcp-and-http-surface.md)
- [Client model](concepts/client-model.md)
- [Sync и storage](concepts/sync-and-storage.md)
- [Architecture](concepts/architecture.md)
- [Open questions](open-questions.md)
