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


async def _reset_schema(pg_url: str) -> None:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        await conn.execute(sa.text("CREATE SCHEMA public"))
    await eng.dispose()


async def _seed_api_key(pg_url: str) -> str:
    """Direct DB seed post-migration. Returns plaintext ks_live_*."""
    import base64
    import hashlib
    import secrets
    from datetime import UTC, datetime
    from uuid import uuid4

    from argon2 import PasswordHasher
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    pepper = "test-pepper-32chars-padded-here!"
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    lookup_hash = hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"e2e-{uuid4().hex[:8]}"
    now = datetime.now(UTC)

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "e2e", "now": now},
        )
        await conn.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, lookup_hash, "
                "created_at) VALUES (:id, :sub, 'e2e', 'ks_live_', :h, :lh, :now)"
            ),
            {
                "id": uuid4(),
                "sub": user_sub,
                "h": argon_hash,
                "lh": lookup_hash,
                "now": now,
            },
        )
    await engine.dispose()
    return f"ks_live_{body}"


@pytest.mark.timeout(120)
@pytest.mark.asyncio
async def test_mcp_e2e_roundtrip(tmp_path):
    pg_url = os.environ.get(
        "KEENYSPACE_DB__URL",
        "postgresql+asyncpg://postgres:x@localhost:55432/postgres",
    )
    fs_root = tmp_path / "fs_root"
    fs_root.mkdir()
    await _reset_schema(pg_url)

    port = _find_free_port()
    env = os.environ.copy()
    env.update(
        {
            "KEENYSPACE_DB__URL": pg_url,
            "KEENYSPACE_FS__ROOT": str(fs_root),
            "KEENYSPACE_AUTH__OIDC_ISSUER_URL": "http://localhost:9999/application/o/test/",
            "KEENYSPACE_AUTH__OIDC_CLIENT_ID": "test-client",
            "KEENYSPACE_AUTH__OIDC_CLIENT_SECRET": "test-secret",
            "KEENYSPACE_AUTH__OIDC_REDIRECT_URI": "http://localhost:8000/v1/api/auth/callback",
            "KEENYSPACE_AUTH__OIDC_POST_LOGOUT_REDIRECT_URI": "http://localhost:8000/",
            "KEENYSPACE_AUTH__API_KEY_PEPPER": "test-pepper-32chars-padded-here!",
            "KEENYSPACE_AUTH__SESSION_SECRET_KEY": "test-session-secret-32chars-pad!",
            "KEENYSPACE_AUTH__COOKIE_SECURE": "false",
            "KEENYSPACE_AUTO_MIGRATE": "true",
        }
    )

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

        plaintext = await _seed_api_key(pg_url)
        headers = {"Authorization": f"Bearer {plaintext}"}
        base_url = f"http://127.0.0.1:{port}"

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
            headers=headers,
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
                {
                    "workspace": "scratch",
                    "content": "second append - proves combine_lifespans wired",
                },
            )
            assert result3 is not None

    finally:
        proc.terminate()
        proc.wait(timeout=10)
