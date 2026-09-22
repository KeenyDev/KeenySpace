from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import Thread
from wsgiref.simple_server import WSGIServer

import structlog
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.registry import CollectorRegistry

log = structlog.get_logger(__name__)

METRICS_PORT_ENV = "KEENYSPACE_METRICS_PORT"
METRICS_ADDR_ENV = "KEENYSPACE_METRICS_ADDR"
DEFAULT_METRICS_PORT = 9100
DEFAULT_METRICS_ADDR = "127.0.0.1"


def metrics_port_from_env() -> int:
    raw = os.environ.get(METRICS_PORT_ENV, "").strip()
    if not raw:
        return DEFAULT_METRICS_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"{METRICS_PORT_ENV} must be an integer, got {raw!r}") from exc
    if not 0 <= port <= 65535:
        raise ValueError(f"{METRICS_PORT_ENV} must be in 0..65535, got {port}")
    return port


def metrics_addr_from_env() -> str:
    return os.environ.get(METRICS_ADDR_ENV, "").strip() or DEFAULT_METRICS_ADDR


class MetricsServer:
    def __init__(self, httpd: WSGIServer, thread: Thread) -> None:
        self._httpd = httpd
        self._thread = thread

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def start_metrics_server(
    port: int, addr: str = DEFAULT_METRICS_ADDR, registry: CollectorRegistry = REGISTRY
) -> MetricsServer:
    httpd, thread = start_http_server(port, addr=addr, registry=registry)
    return MetricsServer(httpd, thread)


@asynccontextmanager
async def metrics_server_lifespan(_app: object) -> AsyncIterator[None]:
    # Prometheus metrics expose workspace slugs, activity and token usage, so
    # they are served on a separate internal port instead of the public API port.
    port = metrics_port_from_env()
    if port == 0:
        log.info("metrics.server_disabled")
        yield
        return
    addr = metrics_addr_from_env()
    try:
        server = start_metrics_server(port, addr)
    except OSError as exc:
        # Metrics are secondary: a port clash must not take the API down.
        log.error("metrics.server_bind_failed", addr=addr, port=port, error=str(exc))
        yield
        return
    log.info("metrics.server_started", addr=addr, port=server.port)
    try:
        yield
    finally:
        await asyncio.to_thread(server.stop)
