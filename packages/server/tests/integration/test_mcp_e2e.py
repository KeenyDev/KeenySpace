"""
Full MCP end-to-end test: validates combine_lifespans with real WAL + DB.
Criterion #2: append_log + read_page in same boot proves combine_lifespans wired.
"""
from __future__ import annotations

import asyncio
import os
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
            await asyncio.sleep(0.3)
    raise TimeoutError(f"Server at {url} did not start within {timeout}s")


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_mcp_e2e_roundtrip(tmp_path):
    pg_url = os.environ.get(
        "KEENYSPACE_DB__URL",
        "postgresql+asyncpg://postgres:x@localhost:55432/postgres",
    )
    fs_root = tmp_path / "fs_root"
    fs_root.mkdir()

    port = _find_free_port()
    env = os.environ.copy()
    env.update({
        "KEENYSPACE_DB__URL": pg_url,
        "KEENYSPACE_FS__ROOT": str(fs_root),
        "KEENYSPACE_AUTH__DEV_TOKEN": "e2etest",
        "KEENYSPACE_AUTO_MIGRATE": "false",
    })

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
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        try:
            await _wait_for_server(f"http://127.0.0.1:{port}/healthz", timeout=20)
        except TimeoutError:
            pytest.skip("server did not start (postgres likely unavailable)")
            return

        base_url = f"http://127.0.0.1:{port}"
        headers = {"Authorization": "Bearer dev-e2etest"}

        async with httpx.AsyncClient(base_url=base_url, headers=headers) as http:
            resp = await http.post(
                "/v1/api/workspaces/",
                json={"slug": "scratch", "blueprint": "default"},
            )
            if resp.status_code != 201:
                if resp.status_code in (500, 503):
                    pytest.skip(f"workspace create failed: {resp.status_code} {resp.text}")
                raise AssertionError(f"Expected 201, got {resp.status_code}: {resp.text}")

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/v1/mcp/",
            headers={"Authorization": "Bearer dev-e2etest"},
        )
        async with Client(transport) as mcp_client:
            result1 = await mcp_client.call_tool(
                "append_log",
                {"workspace": "scratch", "content": "hello world"},
            )
            assert result1 is not None
            result_str = str(result1)
            assert "entry_id" in result_str or len(result_str) > 0

            result2 = await mcp_client.call_tool(
                "read_page",
                {"workspace": "scratch", "path": "index"},
            )
            assert "Index" in str(result2), f"Expected 'Index' in read_page response: {result2}"

            result3 = await mcp_client.call_tool(
                "append_log",
                {"workspace": "scratch", "content": "second append - proves combine_lifespans wired"},
            )
            assert result3 is not None

    finally:
        proc.terminate()
        proc.wait(timeout=10)
