# Client model

KeenySpace client — single binary / package, который:
- Заменяет существующий набор hook-скриптов и launchd-демон prototype implementation.
- Даёт пользователю набор CLI-команд для interaction с workspace.
- Ведёт background-процесс для hooks/checkpoints.
- **Server-driven**: поведение команд (промпты, шаги, инструкции) тянется из MCP сервера во время исполнения, не хардкодится в клиент.

## Что заменяет

| Предшественник | KeenySpace client |
|---|---|
| `scripts/hooks/session_start.py` | `keenyspace hook session-start` |
| `scripts/hooks/session_end.py` | `keenyspace hook session-end` |
| `scripts/hooks/pre_compact.py` | `keenyspace hook pre-compact` |
| `scripts/hooks/_summarize.py` (worker) | внутренняя логика после `hook session-end` |
| `scripts/hooks/_checkpoint.py` (launchd) | `keenyspace daemon` (long-running) |
| `scripts/hooks/post_tool.py` | `keenyspace hook post-tool` |
| `~/Library/LaunchAgents/wiki.checkpoint.plist` | service templates (см. ниже) |
| `scripts/compile.py` | `keenyspace compile` (триггер; работа на сервере) |
| `scripts/query.py` | `keenyspace query "..."` |
| `scripts/lint.py` | `keenyspace lint` |
| `scripts/retention.py` | server-side cron, не клиентская задача |

User-visible behavior сохраняется (hooks по-прежнему срабатывают на SessionStart/End/PreCompact/PostTool в Claude Code), но имплементация — единый клиент.

## Установка

Приоритет: **`uv tool install keenyspace`** (Python tool, минимум deps; согласуется с FastAPI/FastMCP сервером). Дополнительно — Homebrew формула для macOS и standalone binary для случаев, где Python нежелателен.

`npm` пакет не делаем (если только не появится отдельный TS-клиент).

**Auto-update не делаем** — обновление через тот же канал, что и установка (uv / brew / curl).

Конфиг клиента живёт в platform-appropriate user config dir:
- macOS / Linux: `~/.config/keenyspace/`
- Windows: `%APPDATA%\keenyspace\`

Файлы:
- `config.yaml` — server URL, default workspace, log level.
- `auth.json` — session/refresh tokens (file mode 0600 на Unix).

```yaml
# config.yaml
server: https://keeny.example.com
default_workspace: platform-research
log_level: info
```

## Команды

### Lifecycle / setup
- `keenyspace init` — initial setup, prompts for server URL, kicks off `login`.
- `keenyspace login` — OIDC flow в браузере, сохраняет session.
- `keenyspace logout` — clear session.
- `keenyspace status` — server reachability, current user, default workspace, daemon status.

### Workspace operations
- `keenyspace workspace list` — workspaces visible тебе.
- `keenyspace workspace create --blueprint default --name foo` — clone blueprint.
- `keenyspace workspace use foo` — set default.
- `keenyspace pull <ws>` — fetch workspace на локальный диск (default `~/keenyspace/<slug>/`).

### Content operations
- `keenyspace ingest <path>` — feed source file/folder в workspace; pulls instructions from server.
- `keenyspace query "вопрос"` — Q&A над workspace; pulls instructions.
- `keenyspace lint` — wiki health check.
- `keenyspace compile` — trigger server-side compile (logs → wiki pages).

### Sync / hooks
- `keenyspace pull <ws>` — pull актуальную версию workspace с сервера в локальный vault.
- `keenyspace hook <name>` — Claude Code hook entrypoint (session-start, session-end, pre-compact, post-tool).
- `keenyspace daemon start|stop|status` — управление background-сервисом.

(Push клиентом отсутствует — pages пишет server-side compile из логов; см. [Sync и storage](sync-and-storage.md).)

## Server-driven model

Каждая нетривиальная команда работает по pattern:

```
1. Client → MCP get_instructions(workspace, command_name, context)
2. Server возвращает {prompt, steps, mcp_tools_to_use}
3. Client запускает локального LLM-провайдера (Anthropic / OpenAI / local Ollama)
   с этим промптом
4. LLM вызывает MCP tools (read_page, append_log, search, ...) на сервере
5. Server validates authn/authz + applies каждый tool call
```

Преимущество: новый prompt deploy'ится только на сервер. Клиент устаревает медленнее.

LLM provider клиента — конфигурируемый: `anthropic` (default), `openai`, `ollama`. Каждый клиент со своим API key.

## Background daemon

Для долгих background-задач (checkpoint loops, file watchers) клиент имеет `keenyspace daemon`:

- **macOS**: launchd plist, шаблон в `examples/launchd/com.keenyspace.daemon.plist`. `keenyspace service install` копирует.
- **Linux**: systemd user service, шаблон в `examples/systemd/keenyspace.service`. `keenyspace service install` развёртывает.
- **Windows**: NSSM или native service registration (отложено).

Daemon тонкий: tail транскриптов Claude Code, периодический checkpoint-эквивалент через MCP tools, application-level health.

Kill switches:
- `~/.config/keenyspace/disabled` — файл-флаг, daemon сразу exits на старте если он есть.

## Hooks integration с Claude Code

Project настройки (`.claude/settings.local.json`) ссылаются на client:

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

Каждый hook определяет workspace из cwd (через lookup table в `~/.config/keenyspace/workspace-map.yaml` или `keenyspace workspace from-cwd`).

## Open questions

См. [open-questions.md](../open-questions.md).
