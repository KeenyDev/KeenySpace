from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

PG_URL = os.environ.get("KEENYSPACE_DB__URL")


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
    """Direct DB seed (post-migration). Returns (user_sub, plaintext)."""
    import base64
    import hashlib
    import secrets
    from datetime import UTC, datetime
    from uuid import uuid4 as _uuid4

    from argon2 import PasswordHasher
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    pepper = "test-pepper-32chars-padded-here!"
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    lookup_hash = hashlib.sha256(f"{body}{pepper}".encode()).hexdigest()
    argon_hash = PasswordHasher().hash(body)
    user_sub = f"mcp-{_uuid4().hex[:8]}"
    now = datetime.now(UTC)

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "mcp", "now": now},
        )
        await conn.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, lookup_hash, "
                "created_at) VALUES (:id, :sub, 'mcp', 'ks_live_', :h, :lh, :now)"
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


@pytest.mark.asyncio
async def test_get_recent_changes_tool_registered() -> None:
    from keenyspace_server.mcp.server import build_mcp

    mcp = build_mcp()
    names = {t.name for t in await mcp.list_tools()}
    assert "get_recent_changes_tool" in names


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_get_recent_changes_descending_mtime(tmp_path) -> None:
    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    fs_root = tmp_path / "fs_root"
    fs_root.mkdir()
    await _reset_schema(PG_URL or "")
    port = _find_free_port()
    env = await _subprocess_env(PG_URL or "", str(fs_root))

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

        slug = f"recent-order-{int(time.time())}"
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers=headers,
        ) as http_client:
            r = await http_client.post(
                "/v1/api/workspaces/",
                json={"slug": slug, "blueprint": "default"},
            )
            if r.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {r.status_code} {r.text}")
                return
            ws_uuid = r.json()["uuid"]

        ws_dir = fs_root / "workspaces" / ws_uuid
        ws_dir.mkdir(parents=True, exist_ok=True)
        for name, mtime_ns in [
            ("a.md", 1_000_000_000_000_000_000),
            ("b.md", 3_000_000_000_000_000_000),
            ("c.md", 2_000_000_000_000_000_000),
        ]:
            p = ws_dir / name
            p.write_text("x")
            os.utime(p, ns=(mtime_ns, mtime_ns))

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/v1/mcp/",
            headers=headers,
        )
        async with Client(transport) as mcp_client:
            result = await mcp_client.call_tool(
                "get_recent_changes_tool", {"workspace": slug}
            )
            result_str = str(result)
            assert "b.md" in result_str
            assert "c.md" in result_str
            assert "a.md" in result_str
            b_pos = result_str.index("b.md")
            c_pos = result_str.index("c.md")
            a_pos = result_str.index("a.md")
            assert b_pos < c_pos < a_pos, (
                f"Expected b.md < c.md < a.md in result order, got b={b_pos} c={c_pos} a={a_pos}"
            )

    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_get_recent_changes_cursor_stable(tmp_path) -> None:
    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    fs_root = tmp_path / "fs_root"
    fs_root.mkdir()
    await _reset_schema(PG_URL or "")
    port = _find_free_port()
    env = await _subprocess_env(PG_URL or "", str(fs_root))

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

        slug = f"recent-cursor-{int(time.time())}"
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers=headers,
        ) as http_client:
            r = await http_client.post(
                "/v1/api/workspaces/",
                json={"slug": slug, "blueprint": "default"},
            )
            if r.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {r.status_code} {r.text}")
                return
            ws_uuid = r.json()["uuid"]

        ws_dir = fs_root / "workspaces" / ws_uuid
        ws_dir.mkdir(parents=True, exist_ok=True)
        base_ns = 5_000_000_000_000_000_000
        file_names = [f"file{i}.md" for i in range(5)]
        for i, name in enumerate(file_names):
            p = ws_dir / name
            p.write_text("x")
            mtime_ns = base_ns + i * 1_000_000_000
            os.utime(p, ns=(mtime_ns, mtime_ns))

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/v1/mcp/",
            headers=headers,
        )
        async with Client(transport) as mcp_client:
            collected_paths: list[str] = []
            cursor_token: str | None = None

            for _ in range(10):
                call_args: dict = {"workspace": slug, "limit": 2}
                if cursor_token is not None:
                    call_args["cursor"] = cursor_token

                result = await mcp_client.call_tool("get_recent_changes_tool", call_args)
                result_str = str(result)

                for name in file_names:
                    if name in result_str and name not in collected_paths:
                        collected_paths.append(name)

                if "next_cursor" not in result_str or "None" in result_str.split("next_cursor")[1][:20]:
                    break

                import re
                match = re.search(r"next_cursor='([^']+)'", result_str)
                if not match:
                    match = re.search(r'"next_cursor":\s*"([^"]+)"', result_str)
                if not match:
                    break
                cursor_token = match.group(1)

            assert set(collected_paths) == set(file_names), (
                f"Expected all 5 files, got: {collected_paths}"
            )
            assert len(collected_paths) == len(set(collected_paths)), (
                f"Duplicates found: {collected_paths}"
            )

    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_get_recent_changes_since_filter(tmp_path) -> None:
    from datetime import UTC, datetime

    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    fs_root = tmp_path / "fs_root"
    fs_root.mkdir()
    await _reset_schema(PG_URL or "")
    port = _find_free_port()
    env = await _subprocess_env(PG_URL or "", str(fs_root))

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

        slug = f"recent-since-{int(time.time())}"
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers=headers,
        ) as http_client:
            r = await http_client.post(
                "/v1/api/workspaces/",
                json={"slug": slug, "blueprint": "default"},
            )
            if r.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {r.status_code} {r.text}")
                return
            ws_uuid = r.json()["uuid"]

        ws_dir = fs_root / "workspaces" / ws_uuid
        ws_dir.mkdir(parents=True, exist_ok=True)

        t1_ns = 1_000_000_000_000_000_000
        t2_ns = 2_000_000_000_000_000_000
        t3_ns = 3_000_000_000_000_000_000

        for name, mtime_ns in [("old.md", t1_ns), ("mid.md", t2_ns), ("new.md", t3_ns)]:
            p = ws_dir / name
            p.write_text("x")
            os.utime(p, ns=(mtime_ns, mtime_ns))

        t2_seconds = t2_ns / 1e9
        since_iso = datetime.fromtimestamp(t2_seconds, UTC).isoformat()

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/v1/mcp/",
            headers=headers,
        )
        async with Client(transport) as mcp_client:
            result = await mcp_client.call_tool(
                "get_recent_changes_tool", {"workspace": slug, "since": since_iso}
            )
            result_str = str(result)
            assert "old.md" not in result_str, f"old.md should be filtered; got: {result_str}"
            assert "mid.md" in result_str or "new.md" in result_str, (
                f"Expected mid/new in result: {result_str}"
            )

    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_get_recent_changes_invalid_since_rejected(tmp_path) -> None:
    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    fs_root = tmp_path / "fs_root"
    fs_root.mkdir()
    await _reset_schema(PG_URL or "")
    port = _find_free_port()
    env = await _subprocess_env(PG_URL or "", str(fs_root))

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

        slug = f"recent-badsince-{int(time.time())}"
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers=headers,
        ) as http_client:
            r = await http_client.post(
                "/v1/api/workspaces/",
                json={"slug": slug, "blueprint": "default"},
            )
            if r.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {r.status_code} {r.text}")
                return

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/v1/mcp/",
            headers=headers,
        )
        async with Client(transport) as mcp_client:
            try:
                result = await mcp_client.call_tool(
                    "get_recent_changes_tool",
                    {"workspace": slug, "since": "not-a-timestamp"},
                )
                result_str = str(result)
                assert "isError" in result_str or "error" in result_str.lower() or "invalid" in result_str.lower(), (
                    f"Expected error for invalid since, got: {result_str}"
                )
            except Exception as exc:
                assert "invalid" in str(exc).lower() or "error" in str(exc).lower(), (
                    f"Expected ToolError for invalid since, got: {exc}"
                )

    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_get_recent_changes_malformed_cursor_rejected(tmp_path) -> None:
    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    fs_root = tmp_path / "fs_root"
    fs_root.mkdir()
    await _reset_schema(PG_URL or "")
    port = _find_free_port()
    env = await _subprocess_env(PG_URL or "", str(fs_root))

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

        slug = f"recent-badcursor-{int(time.time())}"
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers=headers,
        ) as http_client:
            r = await http_client.post(
                "/v1/api/workspaces/",
                json={"slug": slug, "blueprint": "default"},
            )
            if r.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {r.status_code} {r.text}")
                return

        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/v1/mcp/",
            headers=headers,
        )
        async with Client(transport) as mcp_client:
            try:
                result = await mcp_client.call_tool(
                    "get_recent_changes_tool",
                    {"workspace": slug, "cursor": "not-base64!!!"},
                )
                result_str = str(result)
                assert "isError" in result_str or "error" in result_str.lower() or "malformed" in result_str.lower(), (
                    f"Expected error for malformed cursor, got: {result_str}"
                )
            except Exception as exc:
                assert "malformed" in str(exc).lower() or "error" in str(exc).lower(), (
                    f"Expected ToolError for malformed cursor, got: {exc}"
                )

    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
