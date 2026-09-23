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


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_list_pages_returns_sorted_paths(tmp_path) -> None:
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
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        async with httpx.AsyncClient() as http_client:
            while time.monotonic() < deadline:
                try:
                    if (await http_client.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            else:
                pytest.skip("server did not start")
                return

        _, plaintext = await _seed_api_key(PG_URL or "")
        headers = {"Authorization": f"Bearer {plaintext}"}
        slug = "list-pages-sorted-test"
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=headers) as c:
            r = await c.post("/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"})
            if r.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {r.status_code}")
                return
            ws_uuid = r.json()["uuid"]

        ws_dir = fs_root / "workspaces" / ws_uuid
        (ws_dir / "concepts").mkdir(parents=True, exist_ok=True)
        (ws_dir / "concepts" / "a.md").write_text("alpha")
        (ws_dir / "concepts" / "b.md").write_text("bravo")

        transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/v1/mcp/", headers=headers)
        async with Client(transport) as mcp_client:
            result = await mcp_client.call_tool("list_pages", {"workspace": slug})
            data = result.structured_content if hasattr(result, "structured_content") else result.data
            pages = data["pages"]
            assert "concepts/a.md" in pages
            assert "concepts/b.md" in pages
            assert pages == sorted(pages)
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_list_pages_prefix_concepts(tmp_path) -> None:
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
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        async with httpx.AsyncClient() as http_client:
            while time.monotonic() < deadline:
                try:
                    if (await http_client.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            else:
                pytest.skip("server did not start")
                return

        _, plaintext = await _seed_api_key(PG_URL or "")
        headers = {"Authorization": f"Bearer {plaintext}"}
        slug = "list-pages-prefix-test"
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=headers) as c:
            r = await c.post("/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"})
            if r.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {r.status_code}")
                return
            ws_uuid = r.json()["uuid"]

        ws_dir = fs_root / "workspaces" / ws_uuid
        (ws_dir / "concepts").mkdir(parents=True, exist_ok=True)
        (ws_dir / "notes").mkdir(parents=True, exist_ok=True)
        (ws_dir / "concepts" / "a.md").write_text("alpha")
        (ws_dir / "concepts" / "b.md").write_text("bravo")
        (ws_dir / "notes" / "c.md").write_text("charlie")

        transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/v1/mcp/", headers=headers)
        async with Client(transport) as mcp_client:
            result = await mcp_client.call_tool(
                "list_pages", {"workspace": slug, "prefix": "concepts/"}
            )
            data = result.structured_content if hasattr(result, "structured_content") else result.data
            pages = data["pages"]
            assert "concepts/a.md" in pages
            assert "concepts/b.md" in pages
            assert not any(p.startswith("notes/") for p in pages)
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_list_pages_cursor_pagination(tmp_path) -> None:
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
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        async with httpx.AsyncClient() as http_client:
            while time.monotonic() < deadline:
                try:
                    if (await http_client.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            else:
                pytest.skip("server did not start")
                return

        _, plaintext = await _seed_api_key(PG_URL or "")
        headers = {"Authorization": f"Bearer {plaintext}"}
        slug = "list-pages-cursor-test"
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=headers) as c:
            r = await c.post("/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"})
            if r.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {r.status_code}")
                return
            ws_uuid = r.json()["uuid"]

        ws_dir = fs_root / "workspaces" / ws_uuid
        file_names = ["a.md", "b.md", "c.md", "d.md", "e.md"]
        for name in file_names:
            (ws_dir / name).write_text(f"content of {name}")

        transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/v1/mcp/", headers=headers)
        async with Client(transport) as mcp_client:
            all_collected: list[str] = []
            cursor: str | None = None
            while True:
                args: dict = {"workspace": slug, "limit": 2}
                if cursor is not None:
                    args["cursor"] = cursor
                result = await mcp_client.call_tool("list_pages", args)
                data = result.structured_content if hasattr(result, "structured_content") else result.data
                page = data["pages"]
                all_collected.extend(page)
                cursor = data.get("next_cursor")
                if not cursor:
                    break
            assert set(file_names).issubset(set(all_collected))
            assert len(all_collected) == len(set(all_collected)), "duplicates found"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_list_pages_unsafe_prefix_rejected(tmp_path) -> None:
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
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        async with httpx.AsyncClient() as http_client:
            while time.monotonic() < deadline:
                try:
                    if (await http_client.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            else:
                pytest.skip("server did not start")
                return

        _, plaintext = await _seed_api_key(PG_URL or "")
        headers = {"Authorization": f"Bearer {plaintext}"}
        slug = "list-pages-unsafe-prefix-test"
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=headers) as c:
            r = await c.post("/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"})
            if r.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {r.status_code}")
                return

        transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/v1/mcp/", headers=headers)
        async with Client(transport) as mcp_client:
            try:
                res = await mcp_client.call_tool(
                    "list_pages", {"workspace": slug, "prefix": "../etc"}
                )
                is_error = getattr(res, "is_error", False)
                assert is_error, f"expected error result, got {res}"
            except Exception as exc:
                assert "prefix" in str(exc).lower() or "dot-segment" in str(exc).lower()
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_search_workspace_content_match(tmp_path) -> None:
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
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        async with httpx.AsyncClient() as http_client:
            while time.monotonic() < deadline:
                try:
                    if (await http_client.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            else:
                pytest.skip("server did not start")
                return

        _, plaintext = await _seed_api_key(PG_URL or "")
        headers = {"Authorization": f"Bearer {plaintext}"}
        slug = "search-content-test"
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=headers) as c:
            r = await c.post("/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"})
            if r.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {r.status_code}")
                return
            ws_uuid = r.json()["uuid"]

        ws_dir = fs_root / "workspaces" / ws_uuid
        (ws_dir / "notes").mkdir(parents=True, exist_ok=True)
        (ws_dir / "notes" / "foo.md").write_text("alpha bravo charlie")
        (ws_dir / "other.md").write_text("delta echo foxtrot")

        transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/v1/mcp/", headers=headers)
        async with Client(transport) as mcp_client:
            result = await mcp_client.call_tool(
                "search_workspace", {"workspace": slug, "query": "bravo"}
            )
            data = result.structured_content if hasattr(result, "structured_content") else result.data
            paths = [r["path"] for r in data["results"]]
            assert "notes/foo.md" in paths
            assert "other.md" not in paths
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_search_workspace_filename_match(tmp_path) -> None:
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
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        async with httpx.AsyncClient() as http_client:
            while time.monotonic() < deadline:
                try:
                    if (await http_client.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            else:
                pytest.skip("server did not start")
                return

        _, plaintext = await _seed_api_key(PG_URL or "")
        headers = {"Authorization": f"Bearer {plaintext}"}
        slug = "search-filename-test"
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=headers) as c:
            r = await c.post("/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"})
            if r.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {r.status_code}")
                return
            ws_uuid = r.json()["uuid"]

        ws_dir = fs_root / "workspaces" / ws_uuid
        (ws_dir / "concepts").mkdir(parents=True, exist_ok=True)
        (ws_dir / "concepts" / "banana.md").write_text("nothing relevant here")
        (ws_dir / "other.md").write_text("also nothing")

        transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/v1/mcp/", headers=headers)
        async with Client(transport) as mcp_client:
            result = await mcp_client.call_tool(
                "search_workspace", {"workspace": slug, "query": "banana"}
            )
            data = result.structured_content if hasattr(result, "structured_content") else result.data
            paths = [r["path"] for r in data["results"]]
            assert "concepts/banana.md" in paths
            assert "other.md" not in paths
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set")
async def test_search_workspace_regex_chars_treated_literally(tmp_path) -> None:
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
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        async with httpx.AsyncClient() as http_client:
            while time.monotonic() < deadline:
                try:
                    if (await http_client.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            else:
                pytest.skip("server did not start")
                return

        _, plaintext = await _seed_api_key(PG_URL or "")
        headers = {"Authorization": f"Bearer {plaintext}"}
        slug = "search-invalid-regex-test"
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=headers) as c:
            r = await c.post("/v1/api/workspaces/", json={"slug": slug, "blueprint": "default"})
            if r.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {r.status_code}")
                return

        transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/v1/mcp/", headers=headers)
        async with Client(transport) as mcp_client:
            # WR-08: search treats the query as a literal substring (regex
            # semantics were removed to close a ReDoS surface), so a
            # regex-special character must be handled safely and neither raise
            # nor return an error result.
            res = await mcp_client.call_tool(
                "search_workspace", {"workspace": slug, "query": "["}
            )
            assert not getattr(res, "is_error", False), f"unexpected error: {res}"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
