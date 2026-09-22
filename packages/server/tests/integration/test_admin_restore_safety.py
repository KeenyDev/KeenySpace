"""POST /v1/admin/restore validates everything before touching DB or FS state.

A rejected or failed restore — unsafe dump, incomplete archive, psql failure —
must leave the existing workspaces (rows and directories) exactly as they were.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

PG_URL = os.environ.get("KEENYSPACE_DB__URL")
HAS_PSQL = shutil.which("pg_dump") is not None and shutil.which("psql") is not None

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not PG_URL, reason="postgres unavailable; KEENYSPACE_DB__URL not set"),
    pytest.mark.skipif(not HAS_PSQL, reason="pg_dump/psql binary unavailable"),
]

_IMAGE_BLUEPRINTS = Path(__file__).resolve().parents[4] / "blueprints"


@pytest.fixture(autouse=True)
def _seed_blueprints(fs_root: Path) -> None:
    from keenyspace_server.fs.bootstrap import ensure_fs_root_layout

    ensure_fs_root_layout(fs_root, _IMAGE_BLUEPRINTS)


async def _alembic_head() -> str:
    from keenyspace_server.db.session import get_db_session

    async with get_db_session() as session:
        row = await session.execute(text("SELECT version_num FROM alembic_version"))
        return str(row.scalar_one())


async def _workspace_slugs() -> list[str]:
    from keenyspace_server.db.session import get_db_session

    async with get_db_session() as session:
        rows = await session.execute(text("SELECT slug FROM workspaces ORDER BY slug"))
        return [r[0] for r in rows]


def _add_file(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    info.mtime = int(datetime.now(UTC).timestamp())
    tar.addfile(info, io.BytesIO(payload))


def _add_dir(tar: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    tar.addfile(info)


def _archive(
    head: str,
    *,
    pg_dump: bytes | None,
    with_workspaces_tree: bool = True,
    extra: list[tarfile.TarInfo] | None = None,
) -> bytes:
    manifest = {
        "version": 1,
        "keenyspace_version": "0.1.0",
        "schema_version": 1,
        "alembic_head": head,
        "created_at": datetime.now(UTC).isoformat(),
        "created_by": "test",
        "fs_root_size_bytes": 0,
        "workspaces": {"count": 0, "uuids": []},
        "blueprints": {"count": 0, "names": []},
        "pg_tables_dumped": ["alembic_version"],
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add_file(tar, "manifest.json", json.dumps(manifest).encode())
        if pg_dump is not None:
            _add_file(tar, "pg_dump.sql", pg_dump)
        if with_workspaces_tree:
            _add_dir(tar, "fs_root/workspaces")
        for member in extra or []:
            tar.addfile(member)
    return buf.getvalue()


async def _seed_workspace(client: AsyncClient, fs_root: Path) -> Path:
    resp = await client.post(
        "/v1/api/workspaces/", json={"slug": "keep-me", "blueprint": "default"}
    )
    assert resp.status_code == 201, resp.text
    ws_dir = fs_root / "workspaces" / resp.json()["uuid"]
    assert ws_dir.is_dir()
    return ws_dir


async def _force_restore(client: AsyncClient, archive: bytes) -> Any:
    return await client.post(
        "/v1/admin/restore",
        params={"force": "true"},
        files={"file": ("backup.tar.gz", archive, "application/gzip")},
    )


def _assert_no_restore_scratch(fs_root: Path) -> None:
    leftovers = sorted(p.name for p in (fs_root / "tmp").iterdir())
    assert leftovers == [], leftovers


@pytest.mark.parametrize(
    "meta_line",
    [
        pytest.param("\\! touch {marker}", id="line-start"),
        pytest.param("SELECT 1; \\! touch {marker}", id="mid-line"),
    ],
)
async def test_force_restore_rejects_shell_meta_command_before_wiping(
    client: AsyncClient, fs_root: Path, tmp_path: Path, meta_line: str
) -> None:
    ws_dir = await _seed_workspace(client, fs_root)
    marker = tmp_path / "pwned"
    dump = f"SELECT 1;\n{meta_line.format(marker=marker)}\n".encode()

    resp = await _force_restore(client, _archive(await _alembic_head(), pg_dump=dump))

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "unsafe_pg_dump"
    assert not marker.exists()
    assert await _workspace_slugs() == ["keep-me"]
    assert (ws_dir / ".keenyspace" / "config.yaml").is_file()
    _assert_no_restore_scratch(fs_root)


async def test_force_restore_without_pg_dump_keeps_existing_state(
    client: AsyncClient, fs_root: Path
) -> None:
    ws_dir = await _seed_workspace(client, fs_root)

    resp = await _force_restore(client, _archive(await _alembic_head(), pg_dump=None))

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "missing_pg_dump"
    assert await _workspace_slugs() == ["keep-me"]
    assert ws_dir.is_dir()


async def test_force_restore_without_workspaces_tree_keeps_existing_state(
    client: AsyncClient, fs_root: Path
) -> None:
    ws_dir = await _seed_workspace(client, fs_root)
    archive = _archive(
        await _alembic_head(), pg_dump=b"SELECT 1;\n", with_workspaces_tree=False
    )

    resp = await _force_restore(client, archive)

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "missing_fs_tree"
    assert await _workspace_slugs() == ["keep-me"]
    assert ws_dir.is_dir()


async def test_force_restore_psql_failure_rolls_back_db_and_fs(
    client: AsyncClient, fs_root: Path
) -> None:
    ws_dir = await _seed_workspace(client, fs_root)
    marker_file = ws_dir / "index.md"
    before = marker_file.read_bytes()
    dump = b"SELECT 1;\nSELECT * FROM table_that_does_not_exist;\n"

    resp = await _force_restore(client, _archive(await _alembic_head(), pg_dump=dump))

    assert resp.status_code == 500, resp.text
    assert resp.json()["detail"]["error"] == "psql_restore_failed"
    assert await _workspace_slugs() == ["keep-me"]
    assert marker_file.read_bytes() == before
    assert (fs_root / "blueprints" / "default").is_dir()
    _assert_no_restore_scratch(fs_root)


async def test_restore_rejects_link_to_extraction_root(
    client: AsyncClient, fs_root: Path
) -> None:
    link = tarfile.TarInfo(name="fs_root/workspaces/escape")
    link.type = tarfile.SYMTYPE
    link.linkname = "../.."

    archive = _archive(await _alembic_head(), pg_dump=b"SELECT 1;\n", extra=[link])
    resp = await client.post(
        "/v1/admin/restore",
        files={"file": ("backup.tar.gz", archive, "application/gzip")},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "symlink"
    assert not (fs_root / "workspaces" / "escape").is_symlink()


async def test_force_restore_psql_timeout_rolls_back_fs(
    client: AsyncClient, fs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keenyspace_server.api.admin as admin_mod

    ws_dir = await _seed_workspace(client, fs_root)
    monkeypatch.setattr(admin_mod, "PG_CLIENT_TIMEOUT_S", 0.5)
    monkeypatch.setattr(admin_mod, "_psql_argv", lambda _db_url: ["sleep", "30"])

    resp = await _force_restore(
        client, _archive(await _alembic_head(), pg_dump=b"SELECT 1;\n")
    )

    assert resp.status_code == 500, resp.text
    assert resp.json()["detail"]["error"] == "psql_restore_failed"
    assert await _workspace_slugs() == ["keep-me"]
    assert (ws_dir / "index.md").is_file()
    _assert_no_restore_scratch(fs_root)


async def test_force_restore_reports_failed_fs_rollback(
    client: AsyncClient, fs_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keenyspace_server.api.admin as admin_mod

    ws_dir = await _seed_workspace(client, fs_root)

    def _broken_rollback(self: Any) -> None:
        raise OSError("device busy")

    monkeypatch.setattr(admin_mod._FsSwap, "rollback", _broken_rollback)
    dump = b"SELECT * FROM table_that_does_not_exist;\n"

    resp = await _force_restore(client, _archive(await _alembic_head(), pg_dump=dump))

    assert resp.status_code == 500, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "restore_rollback_failed"
    asides = [p for p in (fs_root / "tmp").iterdir() if p.name.endswith(".aside")]
    assert len(asides) == 1
    assert str(asides[0]) in detail["detail"]
    assert (asides[0] / "0" / ws_dir.name / "index.md").is_file()
    assert await _workspace_slugs() == ["keep-me"]
