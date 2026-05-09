from __future__ import annotations

from prometheus_client import Counter, Histogram
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


def build_instrumentator() -> Instrumentator:
    return Instrumentator(
        should_group_status_codes=False,
        should_ignore_untemplated=True,
        should_respect_env_var=False,
        excluded_handlers=["/healthz", "/readyz", "/metrics"],
    )
