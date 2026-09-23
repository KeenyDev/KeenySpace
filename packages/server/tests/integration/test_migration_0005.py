from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import SERVER_DIR, _reset_schema

PG_URL = os.environ.get("KEENYSPACE_DB__URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"
)

_INSERT_WORKSPACE = (
    "INSERT INTO workspaces "
    "(uuid, slug, display_name, blueprint_ref, status, created_at, archived_at) "
    "VALUES (:uuid, :slug, :slug, 'default', :status, now(), :archived_at)"
)


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=SERVER_DIR,
        env={**os.environ, "KEENYSPACE_DB__URL": PG_URL or ""},
        capture_output=True,
        text=True,
    )


def _sql(statements: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    async def _run() -> list[Any]:
        eng = create_async_engine(PG_URL or "")
        results: list[Any] = []
        try:
            async with eng.begin() as conn:
                for stmt, params in statements:
                    res = await conn.execute(sa.text(stmt), params)
                    results.append(res.fetchall() if res.returns_rows else None)
        finally:
            await eng.dispose()
        return results

    return asyncio.run(_run())


def test_0005_refuses_rows_that_violate_new_constraints() -> None:
    _reset_schema()
    assert _alembic("upgrade", "0004").returncode == 0
    _sql(
        [
            (
                _INSERT_WORKSPACE,
                {"uuid": uuid4(), "slug": "bad", "status": "archived", "archived_at": None},
            )
        ]
    )

    up = _alembic("upgrade", "head")

    assert up.returncode != 0
    assert "ck_workspaces_archived_at_matches_status" in up.stderr
    assert _sql([("SELECT version_num FROM alembic_version", {})])[0] == [("0004",)]


def test_0005_downgrade_drops_intent_only_cursors_and_reupgrades() -> None:
    _reset_schema()
    assert _alembic("upgrade", "head").returncode == 0
    committed_ws, intent_only_ws = uuid4(), uuid4()
    _sql(
        [
            (
                _INSERT_WORKSPACE,
                {"uuid": committed_ws, "slug": "a", "status": "active", "archived_at": None},
            ),
            (
                _INSERT_WORKSPACE,
                {"uuid": intent_only_ws, "slug": "b", "status": "active", "archived_at": None},
            ),
            (
                "INSERT INTO compile_cursors (workspace_uuid, last_wal_id, "
                "last_compile_hash, updated_at, "
                "pending_wal_last_id, pending_plan_hash, pending_plan) VALUES "
                "(:a, 'A', 'ha', now(), 'B', 'hb', '{\"ops\": []}'), "
                "(:b, NULL, NULL, now(), 'C', 'hc', '{\"ops\": []}')",
                {"a": committed_ws, "b": intent_only_ws},
            ),
        ]
    )

    down = _alembic("downgrade", "0004")

    assert down.returncode == 0, down.stderr
    assert _sql([("SELECT workspace_uuid, last_wal_id FROM compile_cursors", {})])[0] == [
        (committed_ws, "A")
    ]
    up = _alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert _sql([("SELECT pending_wal_last_id FROM compile_cursors", {})])[0] == [(None,)]


def test_0005_cursor_rows_follow_workspace_deletion() -> None:
    _reset_schema()
    assert _alembic("upgrade", "head").returncode == 0
    ws = uuid4()
    rows = _sql(
        [
            (
                _INSERT_WORKSPACE,
                {"uuid": ws, "slug": "gone", "status": "active", "archived_at": None},
            ),
            (
                "INSERT INTO compile_cursors "
                "(workspace_uuid, last_wal_id, last_compile_hash, updated_at) "
                "VALUES (:ws, 'A', 'h', now())",
                {"ws": ws},
            ),
            ("DELETE FROM workspaces WHERE uuid = :ws", {"ws": ws}),
            ("SELECT count(*) FROM compile_cursors", {}),
        ]
    )
    assert rows[-1] == [(0,)]
