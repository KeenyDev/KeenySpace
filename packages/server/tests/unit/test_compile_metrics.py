from __future__ import annotations

from keenyspace_server.observability.metrics import (
    COMPILE_DAILY_TOKENS,
    COMPILE_PAGES_WRITTEN_TOTAL,
    COMPILE_PASS_DURATION,
    COMPILE_PAUSED_TOTAL,
    COMPILE_RUNS_TOTAL,
    COMPILE_TOKENS_TOTAL,
)
from prometheus_client import REGISTRY


def test_six_phase_2_metrics_exist_with_correct_names() -> None:
    expected = {
        "keenyspace_compile_runs_total",
        "keenyspace_compile_tokens_total",
        "keenyspace_compile_daily_tokens",
        "keenyspace_compile_paused_total",
        "keenyspace_compile_pass_duration_seconds",
        "keenyspace_compile_pages_written_total",
    }
    collected_names: set[str] = set()
    for collector in REGISTRY._collector_to_names.values():
        collected_names.update(collector)
    normalized = {n.replace("_total", "") for n in collected_names} | collected_names
    for metric in expected:
        base = metric.replace("_total", "")
        assert metric in collected_names or base in normalized, f"{metric} not registered"


def test_compile_runs_total_increments() -> None:
    before = COMPILE_RUNS_TOTAL.labels(workspace="w1", status="success")._value.get()
    COMPILE_RUNS_TOTAL.labels(workspace="w1", status="success").inc()
    after = COMPILE_RUNS_TOTAL.labels(workspace="w1", status="success")._value.get()
    assert after - before == 1


def test_compile_pass_duration_observes() -> None:
    with COMPILE_PASS_DURATION.labels(workspace="w1").time():
        pass
    assert COMPILE_PASS_DURATION.labels(workspace="w1")._sum.get() >= 0


def test_compile_paused_total_label_combinations() -> None:
    for reason in ["loop_abort", "budget_exceeded", "plan_invalid", "daily_ceiling", "timeout"]:
        COMPILE_PAUSED_TOTAL.labels(workspace="wX", reason=reason).inc()


def test_compile_daily_tokens_gauge_set() -> None:
    COMPILE_DAILY_TOKENS.labels(workspace="w2").set(12345)
    assert COMPILE_DAILY_TOKENS.labels(workspace="w2")._value.get() == 12345


def test_compile_pages_written_total_actions() -> None:
    for action in ["create", "update"]:
        COMPILE_PAGES_WRITTEN_TOTAL.labels(workspace="w3", action=action).inc()


def test_compile_tokens_total_directions() -> None:
    for direction in ["input", "output"]:
        COMPILE_TOKENS_TOTAL.labels(workspace="w4", direction=direction).inc()
