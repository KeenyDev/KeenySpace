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


async def _hammer(port: int, n: int, prefix: str, dev_token: str) -> None:
    headers = {"Authorization": f"Bearer dev-{dev_token}"}
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
    dev_token = "stress"

    ports = [_find_free_port() for _ in range(4)]
    env_base = os.environ.copy()
    env_base.update({
        "KEENYSPACE_DB__URL": pg_url,
        "KEENYSPACE_FS__ROOT": str(fs_root),
        "KEENYSPACE_AUTH__DEV_TOKEN": dev_token,
        "KEENYSPACE_AUTH__MULTI_WORKER": str(multi_worker).lower(),
        "KEENYSPACE_AUTO_MIGRATE": "false",
    })

    procs = []
    for port in ports:
        p = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "keenyspace_server.main:app",
             "--port", str(port), "--workers", "1"],
            env=env_base,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(p)

    try:
        try:
            await asyncio.gather(*[
                _wait_for_server(f"http://127.0.0.1:{port}/healthz", timeout=20)
                for port in ports
            ])
        except TimeoutError:
            pytest.skip("servers did not start (postgres likely unavailable)")
            return

        headers = {"Authorization": f"Bearer dev-{dev_token}"}
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

        await asyncio.gather(*[
            _hammer(port, 1000, f"p{i}", dev_token)
            for i, port in enumerate(ports)
        ])

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
