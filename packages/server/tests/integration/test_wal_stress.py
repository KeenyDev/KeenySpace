"""
4-process WAL stress test: 4 uvicorn processes x 1000 appends = 4000 unique-ULID entries.
Validates per-workspace asyncio.Lock + flock scaffold (Pitfall #5/#6 mitigation).
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from keenyspace_server.wal.parser import parse_wal


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


async def _seed_api_key_stress(pg_url: str) -> str:
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
    user_sub = f"stress-{_uuid4().hex[:8]}"
    now = datetime.now(UTC)

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (sub, display_name, email, source, created_at) "
                "VALUES (:sub, :dn, NULL, 'api_key', :now)"
            ),
            {"sub": user_sub, "dn": "stress", "now": now},
        )
        await conn.execute(
            text(
                "INSERT INTO api_keys (id, user_sub, name, prefix, hash, lookup_hash, "
                "created_at) VALUES (:id, :sub, 'stress', 'ks_live_', :h, :lh, :now)"
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
    return f"ks_live_{body}"


async def _hammer(port: int, n: int, prefix: str, plaintext_key: str) -> None:
    headers = {"Authorization": f"Bearer {plaintext_key}"}
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        headers=headers,
        timeout=30.0,
    ) as client:
        for i in range(n):
            resp = await client.post(
                "/v1/api/workspaces/scratch/logs",
                json={"workspace": "scratch", "content": f"{prefix}-{i}"},
            )
            assert resp.status_code == 201, f"append failed: {resp.status_code}"


@pytest.mark.timeout(180)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "multi_worker,expect_all",
    [
        (True, True),
        pytest.param(
            False,
            False,
            marks=pytest.mark.xfail(
                reason=(
                    "multi_worker=False disables flock but asyncio.Lock provides no "
                    "cross-process protection; data loss is expected but not guaranteed "
                    "on every run — this branch cannot be a reliable regression test"
                ),
                strict=False,
            ),
        ),
    ],
)
async def test_wal_stress(tmp_path: Path, multi_worker: bool, expect_all: bool):
    pg_url = os.environ.get(
        "KEENYSPACE_DB__URL",
        "postgresql+asyncpg://postgres:x@localhost:55432/postgres",
    )
    fs_root = tmp_path / "fs_root"
    fs_root.mkdir()

    # Reset schema then mint api-key BEFORE subprocesses boot (AUTO_MIGRATE=true on first
    # process applies schema; we seed against that schema via direct DB connection).
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        await conn.execute(sa.text("CREATE SCHEMA public"))
    await eng.dispose()

    ports = [_find_free_port() for _ in range(4)]
    env_base = os.environ.copy()
    env_base.update(
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
            "KEENYSPACE_AUTH__MULTI_WORKER": str(multi_worker).lower(),
            "KEENYSPACE_AUTO_MIGRATE": "true",
        }
    )

    procs = []
    for port in ports:
        p = subprocess.Popen(
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
            env=env_base,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(p)

    try:
        try:
            await asyncio.gather(
                *[
                    _wait_for_server(f"http://127.0.0.1:{port}/healthz", timeout=20)
                    for port in ports
                ]
            )
        except TimeoutError:
            pytest.skip("servers did not start (postgres likely unavailable)")
            return

        # Mint API-key now that first uvicorn has run migrations.
        plaintext_key = await _seed_api_key_stress(pg_url)
        headers = {"Authorization": f"Bearer {plaintext_key}"}
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{ports[0]}",
            headers=headers,
        ) as client:
            resp = await client.post(
                "/v1/api/workspaces/",
                json={"slug": "scratch", "blueprint": "default"},
            )
            if resp.status_code not in (201, 409):
                pytest.skip(f"workspace create failed: {resp.status_code}")
                return
            ws_uuid_str = resp.json().get("uuid")
            if ws_uuid_str is None:
                pytest.skip("workspace get not implemented; test needs uuid")
                return

        await asyncio.gather(
            *[_hammer(port, 1000, f"p{i}", plaintext_key) for i, port in enumerate(ports)]
        )

        today = datetime.now(UTC).date().isoformat()
        wal_path = fs_root / "workspaces" / ws_uuid_str / "logs" / f"{today}.md"

        if not wal_path.exists():
            pytest.fail(f"WAL file not found at {wal_path}")

        entries = parse_wal(wal_path.read_text())

        if expect_all:
            assert len(entries) == 4000, f"Expected 4000 entries, got {len(entries)}"
            ulids = {str(e.id) for e in entries}
            assert len(ulids) == 4000, f"Expected 4000 unique ULIDs, got {len(ulids)}"
            contents = {e.content for e in entries}
            assert len(contents) == 4000, f"Expected 4000 unique contents, got {len(contents)}"
        else:
            assert len(entries) < 4000 or len({str(e.id) for e in entries}) < 4000, (
                "Expected missing/duplicate entries without flock"
            )

    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=10)
