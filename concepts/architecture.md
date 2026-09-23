# Architecture

Concept-уровневый обзор того, как KeenySpace разворачивается и работает в runtime. Implementation детали (порты, env var имена, Postgres schema, cron expressions) сознательно отложены до фазы реализации.

## Process composition

KeenySpace состоит из:

- **Single ASGI app** — FastAPI + FastMCP в одном процессе (см. [MCP + HTTP surface](mcp-and-http-surface.md)). Обслуживает `/v1/api/*`, `/v1/mcp`, `/v1/admin/*`.
- **Compile-агент** — server-side LLM-driven процесс, который читает накопившиеся записи WAL workspace и обновляет страницы (см. [Sync и storage](sync-and-storage.md)). Может быть встроен в основной process как background task или вынесен в отдельный worker — implementation choice.
- **Scheduler** — системный cron / k8s CronJob для периодических задач (retention, плановый compile, log rotation). Custom long-running scheduler не делаем.

## LLM integration

Все server-side LLM-операции (compile-агент, server-driven instructions для клиентских команд, query-операции) идут через единый стек:

- **`instructor`** — structured outputs от LLM в Pydantic-модели; устраняет необходимость в ad-hoc parsing.
- **`pydantic-ai`** — agent framework: tool calls, model abstraction, streaming, observability.

Provider-agnostic — Anthropic, OpenAI, local Ollama — всё через единый pydantic-ai интерфейс. Конкретный провайдер для server-side compile задаётся конфигом deployment'а.

## Persistent state

| Где | Что |
|---|---|
| FS root (`<server-data-root>/`) | `workspaces/<uuid>/` — markdown vault'ы; `blueprints/` — admin-only заготовки. Канон контента. |
| Postgres | workspace registry (uuid ↔ slug, blueprint ref), users, ACL refs, audit. Server-side meta. |

Никакого отдельного blob storage. Attachments живут в FS workspace под `raw/assets/` как обычные файлы (см. [Workspace model](workspace-model.md)).

Один process — один FS root, один Postgres. Multi-machine репликация — out of scope v1.

## Local self-hosted (docker-compose)

Минимальный shape для команды-одиночки или single-host self-host:

```yaml
# illustrative — не production config
services:
  keenyspace:
    image: keenyspace:<tag>
    depends_on: [postgres]
    volumes:
      - keenyspace-data:/var/lib/keenyspace
  postgres:
    image: postgres:16
    volumes:
      - postgres-data:/var/lib/postgresql/data

volumes:
  keenyspace-data:
  postgres-data:
```

Reverse proxy (Caddy / Traefik / nginx) — отдельным сервисом или снаружи compose'а; TLS на нём (см. ниже).

## Cluster deployment (helm)

Helm chart shape (без values.yaml dump):

```
keenyspace/
  Chart.yaml
  values.yaml
  templates/
    deployment.yaml         # или statefulset.yaml — зависит от PV strategy для FS root
    service.yaml
    ingress.yaml
    configmap.yaml
    secret.yaml
  charts/
    postgresql/             # subchart (например bitnami/postgresql)
                            # или externally-managed Postgres — values.postgresql.enabled=false
```

PV для FS root — обязательный (markdown канон). Postgres — sub-chart или externally-managed instance.

Sizing, replication, NetworkPolicies — out of scope v1.

## Reverse proxy / TLS

KeenySpace **сам TLS не терминирует**. Серверный процесс слушает plain HTTP; TLS-терминатор (Caddy / Traefik / nginx / cloud load balancer) ставится снаружи.

Что server ожидает от proxy:
- Forward `X-Forwarded-Proto`, `X-Forwarded-Host`, `X-Forwarded-For` (стандарт).
- Соблюдать timeout'ы достаточные для compile-операций (минуты, не секунды).

Конфигурация proxy — admin-side; репо может содержать пример snippet'а для одного варианта (Caddy самый простой).

## Backup / restore

Source-of-truth backup = пара:
- **FS root** — `tar` / `rsync` снапшот.
- **Postgres** — `pg_dump`.

**Атомарная пара не требуется**: при несовпадении pages регенерируются compile'ом из логов (см. [Sync и storage](sync-and-storage.md)). Worst case — последний неcompiled batch теряется; компилируется при следующем запуске.

Restore: positioning файлов в volume → restore Postgres → start KeenySpace. Server boot читает Postgres registry и видит workspace'ы по UUID на диске.

## Observability

Default стек — **Loki + Grafana** (с Prometheus для метрик):

- **`/healthz`** — liveness (process alive).
- **`/readyz`** — readiness (FS root writeable, Postgres reachable, IdP discovery endpoint reachable).
- **Structured logs** — jsonl на stdout, собираются Promtail / Alloy → Loki.
- **Metrics endpoint** — `/metrics` в Prometheus-compatible exposition format → Prometheus → Grafana.
- **Dashboards / alerts** — Grafana поверх Loki + Prometheus.

Альтернативные стеки (ELK, OTel collector, cloud logging) admin может подключить через стандартные mechanisms — KeenySpace ничего Loki-специфичного на уровне приложения не использует, jsonl на stdout универсален.

Tracing и detailed alert rules — реализационные решения, не concept.

## Secrets

KeenySpace ожидает sensitive config (Postgres URL с паролем, OIDC client secret, и т.д.) через стандартные механизмы:
- Env vars напрямую.
- File references (env var указывает на путь до файла с секретом, для совместимости с k8s secrets / Docker secrets).

**Built-in vault не делаем.** Admin интегрирует со своим секрет-стором (HashiCorp Vault, Sealed Secrets, sops, dotenv) как сочтёт нужным.

## Open questions

См. [open-questions.md](../open-questions.md) — multi-machine replication, observability stack defaults, helm chart governance относятся к этому документу.
