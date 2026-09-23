# Workspace model

Workspace — first-class абстракция KeenySpace. Один workspace = одна Obsidian vault = одна директория в FS.

## Аналогия с Postgres

| Postgres | KeenySpace |
|---|---|
| `database` | workspace |
| `template1` (или `template0`) | blueprint |
| `CREATE DATABASE foo TEMPLATE template1` | `keenyspace workspace create --blueprint default --name foo` |

## Файловая структура (server-side)

```
<server-data-root>/
  workspaces/
    <ws-uuid>/                   ← live workspace = Obsidian vault
      .keenyspace/               ← workspace metadata (private)
        config.yaml              ← name, slug, blueprint ref, version
      .obsidian/                 ← Obsidian config (per-user, sync игнорирует)
      CLAUDE.md                  ← schema контракт workspace для агентов
      _templates/                ← markdown templates для новых страниц
      <category-1>/              ← напр. concepts/, services/, decisions/
      <category-2>/
      raw/                       ← immutable sources (если категория есть)
      logs/                      ← daily logs (если категория есть)

  blueprints/
    default/                     ← такой же layout, но immutable for non-admins
      .keenyspace/
        blueprint.yaml           ← версия, описание
      CLAUDE.md
      _templates/
      ...
    minimal/
    research-log/
```

Workspace registry, users, ACL — в Postgres (managed server-side, не в файлах workspace).

Коротко:
- **workspace UUID** — стабильный идентификатор на диске. При rename человекочитаемого slug UUID не меняется.
- **`.keenyspace/`** — приватная служебная директория workspace; клиент по умолчанию её не показывает, sync игнорирует на стороне Obsidian (как `.git`).
- **`blueprints/`** — параллельная директория с workspaces. Видна admin'у; обычным пользователям — через `list_blueprints` API.

## Blueprint

Blueprint — заготовка для cloning. Как `template1` в Postgres: можно прочитать, нельзя писать вне admin-режима.

Каждый blueprint имеет:
- **`blueprint.yaml`** — версия (`v1.3`), описание.
- **CLAUDE.md** — будущая schema workspace (что значат категории, как агент должен с ним работать).
- **`_templates/`** — стартеры для новых страниц.
- Опциональный начальный контент (например `index.md` skeleton, `concepts/index.md` skeleton).

### Cloning

```
keenyspace workspace create --blueprint default --name "platform-research"
```

Server-side:
1. Проверяет admin-чтение blueprint + права caller на CREATE workspace.
2. Копирует `blueprints/default/` → `workspaces/<new-uuid>/`.
3. Записывает `<new-uuid>/.keenyspace/config.yaml` с `blueprint: default@v1.3`.
4. Регистрирует workspace в Postgres.

### Blueprint versioning

Workspace pin'ится на версию blueprint в момент cloning. Изменение blueprint не ломает существующие workspace'ы. Blueprint upgrade — auto-merge при `keenyspace workspace upgrade <ws> --blueprint default@v1.4`.

## Obsidian compatibility

- **`.obsidian/` per-user**. Client.sync игнорирует `.obsidian/` — стандартная практика Obsidian, у каждого пользователя локальный.
- **Wikilinks**. `[[page-name]]` resolved Obsidian'ом локально без участия сервера. Сервер не парсит wikilinks при write — это просто текст. Lint-валидация (`keenyspace lint`) проверяет, что targets существуют.
- **Attachments**. По умолчанию `raw/assets/` (configurable через `.keenyspace/config.yaml`).

## Workspace identity

| Layer | Что используется |
|---|---|
| FS storage | UUID (`019234ab-...`) |
| Server API path | UUID или slug (slug → UUID lookup в Postgres) |
| Client UX | slug (`platform-research`) |

Slug: lowercase kebab-case, unique within deployment, mutable. UUID: immutable.

## Workspace metadata (`.keenyspace/config.yaml`)

```yaml
uuid: 019234ab-...
slug: platform-research
display_name: Platform Research
blueprint: default@v1.3
created_at: 2026-05-05T10:00:00Z
schema_version: 1
```

## Lifecycle

1. **Create** (admin или авторизованный пользователь с правом CREATE) — clone из blueprint.
2. **Populate** — клиент гонит команды (`ingest`, hook-driven sync), агенты пишут через MCP.
3. **Read/edit** — Obsidian локально (после `keenyspace pull`), через клиентские команды, или через MCP.
4. **Archive** — flag в `config.yaml`; только-чтение, выводится из листингов по умолчанию.
5. **Delete** — admin-only, soft delete (move в `archived/`) → hard delete после периода (configurable).

## Open questions

- Поддерживаем ли nested workspaces (workspace inside workspace) или строго flat? V1 — flat.

См. [open-questions.md](../open-questions.md) для полного списка.
