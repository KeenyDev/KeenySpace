from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

WAL_APPENDS_TOTAL = Counter(
    "keenyspace_wal_appends_total",
    "Total WAL append operations",
    ["workspace", "source"],
)

WAL_APPEND_LATENCY = Histogram(
    "keenyspace_wal_append_latency_seconds",
    "WAL append latency in seconds",
    ["workspace"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
)

ATOMIC_WRITE_LATENCY = Histogram(
    "keenyspace_atomic_write_latency_seconds",
    "Atomic page write latency in seconds",
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
)

MCP_TOOL_CALL_DURATION = Histogram(
    "keenyspace_mcp_tool_call_duration_seconds",
    "MCP tool call duration in seconds",
    ["tool"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)


COMPILE_RUNS_TOTAL = Counter(
    "keenyspace_compile_runs_total",
    "Total compile pass attempts",
    ["workspace", "status"],
)

COMPILE_TOKENS_TOTAL = Counter(
    "keenyspace_compile_tokens_total",
    "Cumulative LLM tokens consumed by compile (input/output)",
    ["workspace", "direction"],
)

COMPILE_DAILY_TOKENS = Gauge(
    "keenyspace_compile_daily_tokens",
    "Per-workspace daily token usage (reset at 00:00 UTC by APScheduler cron)",
    ["workspace"],
)

COMPILE_PAUSED_TOTAL = Counter(
    "keenyspace_compile_paused_total",
    "Total compile passes that triggered a pause",
    ["workspace", "reason"],
)

COMPILE_PASS_DURATION = Histogram(
    "keenyspace_compile_pass_duration_seconds",
    "Compile pass wall-clock duration",
    ["workspace"],
    buckets=[1, 5, 10, 30, 60, 180],
)

COMPILE_PAGES_WRITTEN_TOTAL = Counter(
    "keenyspace_compile_pages_written_total",
    "Total pages written by compile (create vs update)",
    ["workspace", "action"],
)

WORKSPACE_ARCHIVE_TOTAL = Counter(
    "keenyspace_workspace_archive_total",
    "Total workspace archive/unarchive operations",
    ["action"],
)

WORKSPACE_EXPORT_BYTES_TOTAL = Counter(
    "keenyspace_workspace_export_bytes_total",
    "Total bytes exported as zip",
    ["workspace"],
)

WORKSPACE_IMPORT_TOTAL = Counter(
    "keenyspace_workspace_import_total",
    "Total workspace import operations",
    ["outcome"],
)

WORKSPACE_MANIFEST_TOTAL = Counter(
    "keenyspace_workspace_manifest_total",
    "Total /<slug>/manifest invocations",
    ["outcome"],
)


ADMIN_BACKUP_TOTAL = Counter(
    "keenyspace_admin_backup_total",
    "Total /v1/admin/backup invocations",
)

ADMIN_BACKUP_BYTES = Counter(
    "keenyspace_admin_backup_bytes_total",
    "Total bytes streamed by /v1/admin/backup",
)

ADMIN_RESTORE_TOTAL = Counter(
    "keenyspace_admin_restore_total",
    "Total /v1/admin/restore invocations",
    ["outcome"],
)

ADMIN_RESTORE_WIPED_TOTAL = Counter(
    "keenyspace_admin_restore_wiped_total",
    "Total /v1/admin/restore --force wipes",
)


def build_instrumentator() -> Instrumentator:
    return Instrumentator(
        should_group_status_codes=False,
        should_ignore_untemplated=True,
        should_respect_env_var=False,
        excluded_handlers=["/healthz", "/readyz", "/metrics"],
    )
