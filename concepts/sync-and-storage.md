# Sync и storage

Принцип: markdown-файлы в FS — канон. Сервер посредничает между клиентами и FS, обеспечивая authz, per-workspace WAL и delta-sync. Index/embeddings — derived; в v1 не существуют (см. ниже).

## Write model

Клиент **не пишет страницы напрямую**. Единственный writeable surface для клиента — append в WAL конкретного workspace.

```
Client appends knowledge fragment
  ─→ Append entry в <workspace>/logs/YYYY-MM-DD.md (per-workspace WAL)

Server-side compile (по триггеру или периодически)
  ─→ LLM читает WAL, обновляет страницы (атомарный write: tmp + fsync + rename)
```

Несколько сессий могут конкурентно писать в один WAL — coordination описан в секции WAL ниже. Compile-агент может обновлять одну страницу несколько раз; последний результат побеждает — LLM сама разрешает семантические конфликты при следующем compile pass.

## Read paths

Два режима, зависят от типа клиента:

### Local Obsidian
1. Пользователь делает `keenyspace pull <ws>` — стягивает workspace в `~/keenyspace/<slug>/`.
2. Открывает в Obsidian, читает локально.
3. Периодически `keenyspace pull` — refresh локальной копии.

Latency = 0 для чтения; pull — explicit. Локальная копия read-only с точки зрения server canon — любые правки в Obsidian остаются локальными и перетираются при следующем pull.

### MCP / HTTP (agent или thin client)
1. Агент зовёт MCP read tool, или клиент HTTP read endpoint.
2. Server читает с диска, возвращает.

Всегда видна свежая server-side версия; нет локальной копии.

## WAL per workspace (append-only)

Каждый workspace имеет **свой WAL** — daily-rotated файлы (`<workspace>/logs/YYYY-MM-DD.md`), append-only, единственный writeable surface для клиентов.

Concurrency model:

- **Per-workspace изоляция** — WAL разных workspace'ов не конкурируют между собой; запись в один не блокирует запись в другой.
- **Asyncio coordination внутри процесса** — append-операции одного workspace выполняются под per-workspace asyncio lock. Несколько concurrent appends к одному WAL обрабатываются в очереди без блокировки event loop; запись в разные workspace'ы идёт параллельно.
- **Cross-process safety** — если deployment запускает несколько worker-процессов, поверх asyncio lock добавляется `fcntl.flock` для cross-process сериализации того же WAL. Single-process deployment может полагаться только на asyncio.

Логи — feedstock для compile. Server-side compile-агент периодически или по триггеру читает накопившиеся записи WAL workspace'а и обновляет страницы.

## Conflict semantics

- **На страницах**: клиент не пишет напрямую → нет client-side conflicts. Compile может обновлять одну страницу несколько раз; последняя запись побеждает. LLM при следующем compile разруливает семантические дубли.
- **На WAL**: append-only под per-workspace asyncio lock (+ flock в multi-process deployment) — lost writes исключены; нет конфликтов как таковых.

Manual conflict resolution UX (CLI prompts, 3-way merge) в v1 не нужен.

## Atomic write

Любой server-side write страницы: `tmp/<uuid>.md` → `fsync` → `rename` в финальный путь. Гарантирует, что reader не увидит partial write.

## Sync (client pull)

Pull семантика: клиент держит cursor (last seen state), сервер отдаёт delta. Конкретные wire schemas — implementation detail (см. [MCP + HTTP surface](mcp-and-http-surface.md)).

Push клиентом не существует — у клиента нет writable surface на страницы.

## File system layout (server-side)

См. [Workspace model](workspace-model.md) для полного дерева. Ключевое:
- Один process — один FS root.
- Workspace registry, ACL, audit — в Postgres, не в файлах.

## Backup story

Source-of-truth backup = `tar`/`rsync` от FS root + dump Postgres. Файловый rsync и Postgres dump — не требуется атомарная пара (страницы регенерируются compile'ом из логов в worst case).

## Indexing (derived state)

В v1 — **нет index'а**. Search осуществляется через wikilink traversal: страницы соединены `[[wikilinks]]`, навигация идёт от MOC и index страниц по графу.

Сознательно НЕ делаем:
- Полнотекстовый grep (требует индекс / cost-modeled scan).
- SQLite/DuckDB-derivative index.
- Embeddings / vector search.

Если в future поймём, что traversal не масштабируется, добавим vector search или другой index — но это уже выход за рамки v1 (v1 принципиально про graph navigation).

## Multi-machine / replication

Не в v1. Один server, один FS root, один Postgres.

## Retention

Daily logs архивируются после конфигурируемого периода. Audit log ротатируется. Реализация — server-side cron, не клиентская задача.

## Open questions

См. [open-questions.md](../open-questions.md).
