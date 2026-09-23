# Post-compact context injection — execution plan

> Self-contained execution plan для свежей Claude-сессии, открытой в `cwd=~/Keeny/keenyspace-design/`. Всё необходимое — здесь; в auto-memory другого проекта лазить не надо.

## Что это за папка

KeenySpace — self-hosted opensource система для построения и совместного использования knowledge graph'ов: markdown vault'ы под server'ом с RBAC и MCP-доступом для агентов. Эта папка — **design phase first-pass spec**, не имплементация. 8 файлов:

- `vision.md` — что и зачем, целевая аудитория, non-goals.
- `README.md` — навигационная карта.
- `concepts/workspace-model.md` — workspace = Obsidian vault; blueprints; FS layout; Postgres meta.
- `concepts/rbac-and-auth.md` — auth flow (OIDC + Anthropic-style API keys); authorization отложена до выбора IdP.
- `concepts/mcp-and-http-surface.md` — единый FastAPI+FastMCP app, префиксы `/v1/api`, `/v1/mcp`, `/v1/admin`. Детальные routes/tools deferred.
- `concepts/client-model.md` — клиентское приложение, server-driven, Claude Code hooks. **Это файл, который будет редактироваться в этом плане.**
- `concepts/sync-and-storage.md` — write/read paths, WAL per workspace, конфликты, retention.
- `concepts/architecture.md` — process composition, deployment shapes, observability (Loki+Grafana+Prometheus), LLM stack (instructor+pydantic-ai).
- `open-questions.md` — отложенные решения.

Перед редактированием прочитай минимум `README.md`, `concepts/client-model.md` и `concepts/sync-and-storage.md` — поймёшь стиль и текущее состояние.

## Style guardrails

- **Концепт-уровень.** Без implementation-detail leak: специфические env var имена, точные ports, Postgres schema, regex'ы, parser code, точные wiring details — не сюда. Это будет в фазе реализации.
- **Не speculate.** Не добавляй фичи или детали, которые не описаны в этом плане. Если что-то ambiguous — оставь TODO или добавь вопрос в `open-questions.md`, не выдумывай.
- **Стиль и тон** — match существующим concept-доcам: текст по-русски, технические термины (workspace, MCP, FastAPI, etc.) на английском.
- Ничего не ломай в других доcах. Только `concepts/client-model.md`.

## Задача

Добавить **post-compact context injection** в client model:

- После каждого Claude Code compact'а клиент re-injects wiki context (compact выкидывает grounding из контекста агента).
- Что инжектится: base layer (workspace `CLAUDE.md` + root `index.md`) + smart selection релевантных концепт-страниц на основе recent transcript / latest user prompt.
- Mechanism для smart selection (keyword-based heuristic vs local LLM-call через `pydantic-ai`) — implementation choice; в концепт-док точную механику не пишем.
- Reactive MCP-путь (agent сам вызывает `read_page` / `search_workspace` / etc.) — **параллельный канал**, не меняется. Два независимых механизма coexist.

## Файл с правками

**Только** `concepts/client-model.md`. Все остальные доcы не трогаем.

## Конкретные правки

### 1. Команды → Sync / hooks

В разделе "Команды → Sync / hooks" обновить bullet `keenyspace hook <name>` чтобы `post-compact` был в списке доступных hook names. Например:

```
- `keenyspace hook <name>` — Claude Code hook entrypoint (session-start, session-end, pre-compact, post-compact, post-tool).
```

### 2. Новая короткая секция "Context injection"

Вставить **между** секциями "Server-driven model" и "Background daemon".

Структура (3-5 параграфов, ~15-20 строк):

- **Trigger points**: SessionStart (существует) и после compact (новое).
- **Content**: base layer (workspace `CLAUDE.md` + root `index.md`) + smart selection концепт-страниц.
- **Smart selection**: на основе recent transcript / latest user prompt. Mechanism (keyword-based heuristic vs local LLM-call через `pydantic-ai`) — implementation choice. В концепт-док точную механику не фиксируем.
- **Reactive MCP path**: упомянуть одним абзацем, что параллельно работает MCP — agent сам вызывает `read_page` / `search_workspace` / etc. через MCP tools, когда LLM решает, что нужна вики. Cross-link на `mcp-and-http-surface.md`. Подчеркнуть: два независимых канала, не альтернативы.

### 3. Hooks integration с Claude Code (последний раздел файла)

В JSON-snippet добавить entry для post-compact наряду с SessionStart/SessionEnd/PreCompact/PostToolUse. Например:

```json
{
  "hooks": {
    "SessionStart": [...],
    "SessionEnd":   [...],
    "PreCompact":   [...],
    "PostCompact":  [{"hooks": [{"type": "command", "command": "keenyspace hook post-compact"}]}],
    "PostToolUse":  [...]
  }
}
```

Добавить под snippet'ом одну фразу: hook surface зависит от того, что host (Claude Code) предоставляет — точная wiring (PostCompact / UserPromptSubmit-fallback / др.) — implementation detail.

## Out of scope

- Конкретный mechanism smart selection (heuristic vs LLM call) — implementation phase.
- Host-specific wiring (какой именно hook host'а под post-compact используется).
- Server-side изменения — не нужны (smart selection использует уже описанный pydantic-ai/instructor стек, если решат через LLM).
- Правки в других concept-доcах (`vision.md`, `architecture.md`, `sync-and-storage.md` и др.).
- `open-questions.md` update — mechanism подпадает под существующее "API surface deferred to implementation".

## Verification

После правок проверить:

- `wc -l concepts/client-model.md` — ожидаемый рост ~20-30 строк (было ~126).
- `grep -rn "#NOTE\|metrikus\|interexy\|метрикус\|интерекси" .` — пусто (no regression).
- Нет impl-leak: специфических env var имён, точных ports, кода parsing'а транскрипта, regex'ов для smart selection, конкретных версий библиотек.
- Спот-чек секции "Context injection" — читается как concept (что и зачем), не deployment guide.
- Reactive MCP path упомянут одной-двумя строчками с cross-link, не дублирует содержание `mcp-and-http-surface.md`.

## Done when

- `concepts/client-model.md` обновлён по трём пунктам выше.
- Verification grep'ы чистые.
- Никаких других файлов не создано/изменено.
