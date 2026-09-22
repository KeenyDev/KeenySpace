from __future__ import annotations

import socket
import threading

import httpx
import pytest
from keenyspace_server.observability import metrics as app_metrics
from keenyspace_server.observability.metrics_server import (
    DEFAULT_METRICS_PORT,
    METRICS_ADDR_ENV,
    METRICS_PORT_ENV,
    metrics_port_from_env,
    metrics_server_lifespan,
)
from structlog.testing import capture_logs

pytestmark = pytest.mark.asyncio


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _scrape(port: int) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.get(f"http://127.0.0.1:{port}/metrics", timeout=5)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", DEFAULT_METRICS_PORT), ("0", 0), (" 9200 ", 9200)],
    ids=["unset-uses-default", "zero-disables", "explicit-port"],
)
async def test_metrics_port_from_env(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    monkeypatch.setenv(METRICS_PORT_ENV, raw)
    assert metrics_port_from_env() == expected


@pytest.mark.parametrize("raw", ["abc", "-1", "70000"], ids=["not-int", "negative", "too-big"])
async def test_metrics_port_from_env_rejects_invalid(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(METRICS_PORT_ENV, raw)
    with pytest.raises(ValueError, match=METRICS_PORT_ENV):
        metrics_port_from_env()


async def test_lifespan_serves_metrics_on_configured_port_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _free_port()
    monkeypatch.setenv(METRICS_PORT_ENV, str(port))
    monkeypatch.setenv(METRICS_ADDR_ENV, "127.0.0.1")

    async with metrics_server_lifespan(object()):
        resp = await _scrape(port)
        assert resp.status_code == 200
        assert app_metrics.ADMIN_BACKUP_TOTAL.describe()[0].name in resp.text

    with pytest.raises(httpx.ConnectError):
        await _scrape(port)


async def test_lifespan_port_zero_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(METRICS_PORT_ENV, "0")
    threads_before = threading.active_count()
    with capture_logs() as logs:
        async with metrics_server_lifespan(object()):
            assert threading.active_count() == threads_before
    assert [entry["event"] for entry in logs] == ["metrics.server_disabled"]


async def test_lifespan_survives_port_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = int(blocker.getsockname()[1])
        monkeypatch.setenv(METRICS_PORT_ENV, str(port))
        monkeypatch.setenv(METRICS_ADDR_ENV, "127.0.0.1")

        with capture_logs() as logs:
            async with metrics_server_lifespan(object()):
                pass
    assert [entry["event"] for entry in logs] == ["metrics.server_bind_failed"]
    assert logs[0]["port"] == port


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, "127.0.0.1", id="unset-is-loopback"),
        pytest.param("", "127.0.0.1", id="empty-is-loopback"),
        pytest.param("0.0.0.0", "0.0.0.0", id="explicit-all-interfaces"),
    ],
)
async def test_metrics_addr_defaults_to_loopback(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: str
) -> None:
    from keenyspace_server.observability.metrics_server import metrics_addr_from_env

    if raw is None:
        monkeypatch.delenv(METRICS_ADDR_ENV, raising=False)
    else:
        monkeypatch.setenv(METRICS_ADDR_ENV, raw)

    assert metrics_addr_from_env() == expected
