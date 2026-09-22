"""
WAL rotation race test: validates filename derivation inside asyncio.Lock (Pitfall #7).
Simulates 100 concurrent appends around UTC midnight boundary.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from keenyspace_server.wal.locks import WorkspaceLockRegistry
from keenyspace_server.wal.parser import parse_wal


class FakeDatetime(datetime):
    _sequence: list[datetime]
    _idx: int

    @classmethod
    def now(cls, tz=None):
        if cls._idx < len(cls._sequence):
            val = cls._sequence[cls._idx]
            cls._idx += 1
            return val
        return datetime.now(tz or UTC)


def _build_sequence(n_before: int, n_after: int) -> list[datetime]:
    seq = []
    for i in range(n_before):
        offset_us = (n_before - i) * 10000
        dt = datetime(2026, 5, 9, 23, 59, 59, 1000000 - offset_us, UTC)
        seq.append(dt)
    for i in range(n_after):
        offset_us = i * 10000
        dt = datetime(2026, 5, 10, 0, 0, 0, offset_us, UTC)
        seq.append(dt)
    return seq


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_wal_rotation_race(tmp_path: Path):
    ws_uuid = uuid.uuid4()
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    (ws_root / "logs").mkdir()

    locks = WorkspaceLockRegistry()

    n_before = 50
    n_after = 50
    sequence = _build_sequence(n_before, n_after)

    class _MockSettings:
        class wal:  # noqa: N801
            max_entry_bytes = 256 * 1024

        class auth:  # noqa: N801
            multi_worker = False

    call_count = [0]

    original_now = datetime.now

    def mock_now(tz=None):
        idx = call_count[0]
        call_count[0] += 1
        if idx < len(sequence):
            return sequence[idx]
        return original_now(tz or UTC)

    import keenyspace_server.wal.writer as writer_module

    class _PatchedDatetime:
        @staticmethod
        def now(tz=None):
            return mock_now(tz)

    with patch.object(writer_module, "datetime", _PatchedDatetime):
        from keenyspace_server.wal.writer import append_log

        tasks = [
            append_log(
                ws_uuid=ws_uuid,
                ws_root=ws_root,
                content=f"entry-{i}",
                actor="dev:test",
                source="test",
                client_version=None,
                settings=_MockSettings(),
                locks=locks,
            )
            for i in range(n_before + n_after)
        ]
        await asyncio.gather(*tasks)

    day_before = ws_root / "logs" / "2026-05-09.md"
    day_after = ws_root / "logs" / "2026-05-10.md"

    entries_before = parse_wal(day_before.read_text()) if day_before.exists() else []
    entries_after = parse_wal(day_after.read_text()) if day_after.exists() else []

    total = len(entries_before) + len(entries_after)
    assert total == 100, f"Expected 100 entries total, got {total} ({len(entries_before)} + {len(entries_after)})"

    all_ulids = [str(e.id) for e in entries_before + entries_after]
    assert len(set(all_ulids)) == 100, f"Expected 100 unique ULIDs, got {len(set(all_ulids))}"

    assert day_before.exists(), "day-before file must exist"
    assert day_after.exists(), "day-after file must exist"
