from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from keenyspace_server.wal import writer as writer_module
from keenyspace_server.wal.framing import format_entry
from keenyspace_server.wal.locks import WorkspaceLockRegistry
from keenyspace_server.wal.parser import WalEntry, parse_wal
from ulid import ULID

pytestmark = pytest.mark.asyncio

_SETTINGS = SimpleNamespace(
    wal=SimpleNamespace(max_entry_bytes=1024),
    auth=SimpleNamespace(multi_worker=False),
)


def _clock(*values: datetime) -> type:
    sequence = list(values)

    class _Clock:
        @staticmethod
        def now(tz: object = None) -> datetime:
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]

    return _Clock


async def _append(
    ws_uuid: UUID,
    ws_root: Path,
    locks: WorkspaceLockRegistry,
    content: str,
) -> writer_module.AppendResult:
    return await writer_module.append_log(
        ws_uuid=ws_uuid,
        ws_root=ws_root,
        content=content,
        actor="dev:test",
        source="test",
        client_version=None,
        settings=_SETTINGS,  # type: ignore[arg-type]
        locks=locks,
    )


def _all_entries(ws_root: Path) -> list[WalEntry]:
    entries: list[WalEntry] = []
    for log_file in sorted((ws_root / "logs").glob("*.md")):
        entries.extend(parse_wal(log_file.read_text()))
    return entries


async def test_same_millisecond_appends_get_strictly_increasing_ids(tmp_path: Path) -> None:
    ws_uuid, locks = uuid4(), WorkspaceLockRegistry()
    fixed = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)

    with patch.object(writer_module, "datetime", _clock(fixed)):
        results = [await _append(ws_uuid, tmp_path, locks, f"fact {i}") for i in range(50)]

    ids = [str(r.entry_id) for r in results]
    assert ids == sorted(ids)
    assert len(set(ids)) == 50
    assert [str(e.id) for e in _all_entries(tmp_path)] == ids


async def test_backward_clock_step_does_not_mint_lower_id(tmp_path: Path) -> None:
    ws_uuid, locks = uuid4(), WorkspaceLockRegistry()
    later = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
    earlier = later - timedelta(hours=1)

    with patch.object(writer_module, "datetime", _clock(later, earlier)):
        first = await _append(ws_uuid, tmp_path, locks, "before step")
        second = await _append(ws_uuid, tmp_path, locks, "after step")

    assert str(second.entry_id) > str(first.entry_id)
    assert second.ts == earlier


async def test_id_sequence_is_seeded_from_newest_log_after_restart(tmp_path: Path) -> None:
    ws_uuid = uuid4()
    future = datetime(2030, 1, 1, tzinfo=UTC)
    newest_logged = ULID.from_datetime(future)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "2026-05-09.md").write_bytes(
        format_entry(
            entry_id=newest_logged,
            ts=future,
            actor="dev:test",
            source="test",
            client_version=None,
            content_hash="sha256:x",
            parent_id=None,
            content="logged before restart",
        )
    )

    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
    with patch.object(writer_module, "datetime", _clock(now)):
        result = await _append(ws_uuid, tmp_path, WorkspaceLockRegistry(), "after restart")

    assert str(result.entry_id) > str(newest_logged)


async def test_filename_and_entry_ts_come_from_one_clock_reading(tmp_path: Path) -> None:
    ws_uuid, locks = uuid4(), WorkspaceLockRegistry()
    before_midnight = datetime(2026, 5, 9, 23, 59, 59, 999000, tzinfo=UTC)
    after_midnight = datetime(2026, 5, 10, 0, 0, 0, tzinfo=UTC)

    with patch.object(
        writer_module, "datetime", _clock(before_midnight, after_midnight, after_midnight)
    ):
        first = await _append(ws_uuid, tmp_path, locks, "last of day")
        second = await _append(ws_uuid, tmp_path, locks, "first of next day")

    day_one = parse_wal((tmp_path / "logs" / "2026-05-09.md").read_text())
    day_two = parse_wal((tmp_path / "logs" / "2026-05-10.md").read_text())
    assert [e.ts for e in day_one] == [first.ts] == [before_midnight]
    assert [e.ts for e in day_two] == [second.ts] == [after_midnight]


async def test_returned_ts_matches_written_entry(tmp_path: Path) -> None:
    result = await _append(uuid4(), tmp_path, WorkspaceLockRegistry(), "a fact")

    (entry,) = _all_entries(tmp_path)
    assert entry.id == result.entry_id
    assert entry.ts == result.ts


@pytest.mark.parametrize("content", ["", "   \n\t "])
async def test_empty_content_is_rejected_without_writing(tmp_path: Path, content: str) -> None:
    with pytest.raises(writer_module.EmptyContentError):
        await _append(uuid4(), tmp_path, WorkspaceLockRegistry(), content)

    assert not (tmp_path / "logs").exists()


async def test_oversized_content_is_rejected_before_taking_the_lock(tmp_path: Path) -> None:
    class _NoLockRegistry(WorkspaceLockRegistry):
        async def for_workspace(self, ws_uuid: UUID) -> object:  # type: ignore[override]
            raise AssertionError("lock must not be acquired for oversized content")

    with pytest.raises(writer_module.PayloadTooLargeError):
        await _append(uuid4(), tmp_path, _NoLockRegistry(), "x" * 1025)


async def test_multibyte_content_size_is_measured_in_bytes(tmp_path: Path) -> None:
    with pytest.raises(writer_module.PayloadTooLargeError):
        await _append(uuid4(), tmp_path, WorkspaceLockRegistry(), "é" * 600)
