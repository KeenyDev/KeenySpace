"""RED tests for ws/archive.py — fail before implementation exists."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


def test_archive_conflict_error_importable() -> None:
    from keenyspace_server.ws.archive import ArchiveConflictError

    assert issubclass(ArchiveConflictError, ValueError)


def test_archive_workspace_importable() -> None:
    from keenyspace_server.ws.archive import archive_workspace

    assert callable(archive_workspace)


def test_unarchive_workspace_importable() -> None:
    from keenyspace_server.ws.archive import unarchive_workspace

    assert callable(unarchive_workspace)


def test_archive_workspace_is_coroutine() -> None:
    import inspect

    from keenyspace_server.ws.archive import archive_workspace

    assert inspect.iscoroutinefunction(archive_workspace)


def test_unarchive_workspace_is_coroutine() -> None:
    import inspect

    from keenyspace_server.ws.archive import unarchive_workspace

    assert inspect.iscoroutinefunction(unarchive_workspace)


@pytest.mark.asyncio
async def test_archive_workspace_raises_conflict_when_no_rows(tmp_path: Path) -> None:
    from keenyspace_server.ws.archive import ArchiveConflictError, archive_workspace
    from sqlalchemy.ext.asyncio import AsyncSession

    mock_session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(ArchiveConflictError):
        await archive_workspace(
            mock_session,
            ws_uuid=uuid4(),
            ws_dir=tmp_path,
            actor_sub="test-sub",
            slug="test-ws",
        )


@pytest.mark.asyncio
async def test_unarchive_workspace_raises_conflict_when_no_rows(tmp_path: Path) -> None:
    from keenyspace_server.ws.archive import ArchiveConflictError, unarchive_workspace
    from sqlalchemy.ext.asyncio import AsyncSession

    mock_session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(ArchiveConflictError):
        await unarchive_workspace(
            mock_session,
            ws_uuid=uuid4(),
            ws_dir=tmp_path,
            actor_sub="test-sub",
            slug="test-ws",
        )


def test_mirror_archived_at_to_config_writes_archived_at(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    import yaml
    from keenyspace_server.ws.archive import _mirror_archived_at_to_config

    config_dir = tmp_path / ".keenyspace"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    existing = {"uuid": "abc", "slug": "test"}
    config_path.write_text(yaml.dump(existing))

    now = datetime.now(UTC)
    _mirror_archived_at_to_config(tmp_path, now)

    data = yaml.safe_load(config_path.read_text())
    assert "archived_at" in data
    assert data["archived_at"] == now.isoformat()


def test_mirror_archived_at_to_config_removes_archived_at_on_none(tmp_path: Path) -> None:

    import yaml
    from keenyspace_server.ws.archive import _mirror_archived_at_to_config

    config_dir = tmp_path / ".keenyspace"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    existing = {"uuid": "abc", "slug": "test", "archived_at": "2026-01-01T00:00:00+00:00"}
    config_path.write_text(yaml.dump(existing))

    _mirror_archived_at_to_config(tmp_path, None)

    data = yaml.safe_load(config_path.read_text())
    assert "archived_at" not in data


def test_mirror_archived_at_swallows_missing_config(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from keenyspace_server.ws.archive import _mirror_archived_at_to_config

    now = datetime.now(UTC)
    _mirror_archived_at_to_config(tmp_path / "nonexistent_ws", now)
