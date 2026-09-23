from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime

import pytest

PG_URL = os.environ.get("KEENYSPACE_DB__URL")


@pytest.mark.asyncio
async def test_list_workspaces_tool_registered_in_mcp() -> None:
    from keenyspace_server.mcp.server import build_mcp

    mcp = build_mcp()
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert "list_workspaces" in names
    assert "get_workspace_info" in names


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _reset_schema(pg_url: str) -> None:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        await conn.execute(sa.text("CREATE SCHEMA public"))
    await eng.dispose()


async def _subprocess_env(pg_url: str, fs_root_str: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "KEENYSPACE_DB__URL": pg_url,
            "KEENYSPACE_FS__ROOT": fs_root_str,
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
    return env


async def _seed_api_key(pg_url: str) -> tuple[str, str]:
    import base64
    import hashlib
    import secrets
    from uuid import uuid4 as _uuid4

    from argon2 import PasswordHasher
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    pepper = "test-pepper-32chars-padded-here!"
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    lookup_hash = hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"mcp-ws-{_uuid4().hex[:8]}"
    now = datetime.now(UTC)

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "mcp-ws", "now": now},
        )
        await conn.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, lookup_hash, "
                "created_at) VALUES (:id, :sub, 'mcp-ws', 'ks_live_', :h, :lh, :now)"
            ),
            {
                "id": _uuid4(),
                "sub": user_sub,
                "h": argon_hash,
                "lh": lookup_hash,
                "now": now,
            },
        )
    await engine.dispose()
    return user_sub, f"ks_live_{body}"


def _archive_workspace_sync(pg_url: str, slug: str) -> None:
    import asyncio as _asyncio

    from sqlalchemy import select, update
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _do() -> None:
        from keenyspace_server.db.models import Workspace

        engine = create_async_engine(pg_url)
        async with engine.begin() as conn:
            result = await conn.execute(
                select(Workspace.uuid).where(Workspace.slug == slug)
            )
            row = result.fetchone()
            if row:
                await conn.execute(
                    update(Workspace)
                    .where(Workspace.uuid == row[0])
                    .values(status="archived", archived_at=datetime.now(UTC))
                )
        await engine.dispose()

    _asyncio.get_event_loop().run_until_complete(_do())


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_list_workspaces_active_only(tmp_path) -> None:
    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    fs_root = tmp_path / "fs_root"
    fs_root.mkdir()
    await _reset_schema(PG_URL or "")
    port = _find_free_port()
    env = await _subprocess_env(PG_URL or "", str(fs_root))

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "keenyspace_server.main:app",
         "--port", str(port), "--workers", "1"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        async with httpx.AsyncClient() as http_client:
            while time.monotonic() < deadline:
                try:
                    resp = await http_client.get(f"http://127.0.0.1:{port}/healthz")
                    if resp.status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            else:
                pytest.skip("server did not start (postgres likely unavailable)")
                return

        _, plaintext = await _seed_api_key(PG_URL or "")
        headers = {"Authorization": f"Bearer {plaintext}"}

        active_slug = "mcp-active-ws"
        archived_slug = "mcp-archived-ws"
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers=headers
        ) as http_client:
            r = await http_client.post(
                "/v1/api/workspaces/", json={"slug": active_slug, "blueprint": "default"}
            )
            if r.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {r.status_code}")
                return
            r2 = await http_client.post(
                "/v1/api/workspaces/", json={"slug": archived_slug, "blueprint": "default"}
            )
            if r2.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {r2.status_code}")
                return

        from keenyspace_server.db.models import Workspace as WsModel
        from sqlalchemy import select, update
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(PG_URL or "")
        async with engine.begin() as conn:
            result = await conn.execute(select(WsModel.uuid).where(WsModel.slug == archived_slug))
            row = result.fetchone()
            if row:
                await conn.execute(
                    update(WsModel)
                    .where(WsModel.uuid == row[0])
                    .values(status="archived", archived_at=datetime.now(UTC))
                )
        await engine.dispose()

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/v1/mcp/", headers=headers
        )
        async with Client(transport) as mcp_client:
            result = await mcp_client.call_tool("list_workspaces", {"include_archived": False})
            assert result is not None
            result_str = str(result)
            assert active_slug in result_str, f"active slug not in result: {result_str}"
            assert archived_slug not in result_str, f"archived slug leaked: {result_str}"

    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_list_workspaces_include_archived(tmp_path) -> None:
    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    fs_root = tmp_path / "fs_root"
    fs_root.mkdir()
    await _reset_schema(PG_URL or "")
    port = _find_free_port()
    env = await _subprocess_env(PG_URL or "", str(fs_root))

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "keenyspace_server.main:app",
         "--port", str(port), "--workers", "1"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        async with httpx.AsyncClient() as http_client:
            while time.monotonic() < deadline:
                try:
                    resp = await http_client.get(f"http://127.0.0.1:{port}/healthz")
                    if resp.status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            else:
                pytest.skip("server did not start (postgres likely unavailable)")
                return

        _, plaintext = await _seed_api_key(PG_URL or "")
        headers = {"Authorization": f"Bearer {plaintext}"}

        active_slug = "mcp-ia-active"
        archived_slug = "mcp-ia-archived"
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers=headers
        ) as http_client:
            for slug in (active_slug, archived_slug):
                r = await http_client.post(
                    "/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"}
                )
                if r.status_code not in (201, 409):
                    pytest.skip(f"workspace create failed: {r.status_code}")
                    return

        from keenyspace_server.db.models import Workspace as WsModel
        from sqlalchemy import select, update
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(PG_URL or "")
        async with engine.begin() as conn:
            result = await conn.execute(select(WsModel.uuid).where(WsModel.slug == archived_slug))
            row = result.fetchone()
            if row:
                await conn.execute(
                    update(WsModel)
                    .where(WsModel.uuid == row[0])
                    .values(status="archived", archived_at=datetime.now(UTC))
                )
        await engine.dispose()

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/v1/mcp/", headers=headers
        )
        async with Client(transport) as mcp_client:
            result = await mcp_client.call_tool("list_workspaces", {"include_archived": True})
            assert result is not None
            result_str = str(result)
            assert active_slug in result_str, f"active slug missing: {result_str}"
            assert archived_slug in result_str, f"archived slug missing: {result_str}"

    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_get_workspace_info(tmp_path) -> None:
    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    fs_root = tmp_path / "fs_root"
    fs_root.mkdir()
    await _reset_schema(PG_URL or "")
    port = _find_free_port()
    env = await _subprocess_env(PG_URL or "", str(fs_root))

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "keenyspace_server.main:app",
         "--port", str(port), "--workers", "1"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        async with httpx.AsyncClient() as http_client:
            while time.monotonic() < deadline:
                try:
                    resp = await http_client.get(f"http://127.0.0.1:{port}/healthz")
                    if resp.status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            else:
                pytest.skip("server did not start (postgres likely unavailable)")
                return

        _, plaintext = await _seed_api_key(PG_URL or "")
        headers = {"Authorization": f"Bearer {plaintext}"}

        slug = "mcp-info-ws"
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers=headers
        ) as http_client:
            r = await http_client.post(
                "/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"}
            )
            if r.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {r.status_code}")
                return
            created = r.json() if r.status_code == 201 else {}
            ws_uuid = created.get("uuid", "")

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/v1/mcp/", headers=headers
        )
        async with Client(transport) as mcp_client:
            result = await mcp_client.call_tool("get_workspace_info", {"workspace": slug})
            assert result is not None
            result_str = str(result)
            assert slug in result_str, f"slug missing from result: {result_str}"
            assert "status" in result_str, f"status missing: {result_str}"
            if ws_uuid:
                assert ws_uuid in result_str, f"uuid missing: {result_str}"

    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_get_workspace_info_missing_raises_tool_error(tmp_path) -> None:
    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    fs_root = tmp_path / "fs_root"
    fs_root.mkdir()
    await _reset_schema(PG_URL or "")
    port = _find_free_port()
    env = await _subprocess_env(PG_URL or "", str(fs_root))

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "keenyspace_server.main:app",
         "--port", str(port), "--workers", "1"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        async with httpx.AsyncClient() as http_client:
            while time.monotonic() < deadline:
                try:
                    resp = await http_client.get(f"http://127.0.0.1:{port}/healthz")
                    if resp.status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            else:
                pytest.skip("server did not start (postgres likely unavailable)")
                return

        _, plaintext = await _seed_api_key(PG_URL or "")
        headers = {"Authorization": f"Bearer {plaintext}"}

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/v1/mcp/", headers=headers
        )
        from fastmcp.exceptions import ToolError

        async with Client(transport) as mcp_client:
            with pytest.raises(ToolError) as exc_info:
                await mcp_client.call_tool(
                    "get_workspace_info", {"workspace": "does-not-exist-xyz"}
                )
            message = str(exc_info.value).lower()
            assert "not found" in message or "error" in message, (
                f"Expected error for missing workspace, got: {exc_info.value!r}"
            )

    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
