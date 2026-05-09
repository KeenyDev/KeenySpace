"""
Wave 2 early smoke test: validates combine_lifespans wiring against the skeleton app.

Tests that the stub 'ping' tool works for >= 2 sequential calls in the same boot,
which proves the FastMCP task group stays initialized (Pitfall #1).

Also includes a negative-test: stripping mcp_app.lifespan from combine_lifespans
causes the second call to fail, proving the test is load-bearing.
"""
from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_for_server(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.2)
    raise TimeoutError(f"Server at {url} did not start within {timeout}s")


@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_ping_two_calls_same_boot() -> None:
    port = _find_free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "keenyspace_server.main:app",
            "--port",
            str(port),
            "--workers",
            "1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await _wait_for_server(f"http://127.0.0.1:{port}/healthz")

        transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/v1/mcp/")
        async with Client(transport) as client:
            r1 = await client.call_tool("ping", {"message": "first"})
            assert "pong: first" in str(r1), f"Unexpected result: {r1}"

            r2 = await client.call_tool("ping", {"message": "second"})
            assert "pong: second" in str(r2), f"Unexpected result: {r2}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_negative_without_mcp_lifespan_second_call_fails() -> None:
    """
    Proves the test is load-bearing: if mcp_app.lifespan is not in combine_lifespans,
    the second MCP call raises an error.
    """
    from contextlib import asynccontextmanager
    from typing import AsyncIterator

    from fastapi import FastAPI
    from fastmcp.utilities.lifespan import combine_lifespans

    from keenyspace_server.mcp.server import build_mcp_skeleton

    mcp = build_mcp_skeleton()
    mcp_app = mcp.http_app(path="/")

    @asynccontextmanager
    async def app_lifespan_only(app: FastAPI) -> AsyncIterator[None]:
        yield

    broken_app = FastAPI(
        title="broken-skeleton",
        lifespan=combine_lifespans(app_lifespan_only),
    )

    from keenyspace_server.api import health
    broken_app.include_router(health.router)
    broken_app.mount("/v1/mcp", mcp_app)

    port = _find_free_port()

    import uvicorn

    config = uvicorn.Config(broken_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    async def run_server() -> None:
        await server.serve()

    server_task = asyncio.create_task(run_server())
    await asyncio.sleep(1.5)

    try:
        transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/v1/mcp/")
        error_raised = False
        try:
            async with Client(transport) as client:
                await client.call_tool("ping", {"message": "first"})
                await client.call_tool("ping", {"message": "second"})
        except Exception:
            error_raised = True

        assert error_raised, (
            "Expected second call to fail without mcp_app.lifespan in combine_lifespans"
        )
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=5.0)
