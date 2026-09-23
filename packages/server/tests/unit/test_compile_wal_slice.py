from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from keenyspace_server.compile.wal_slice import WalSlice, extract_wal_slice
from keenyspace_server.wal.framing import format_entry
from ulid import ULID

_DAY = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)


def _make_entry_bytes(
    at: datetime = _DAY, actor: str = "dev:test", content: str = "hello"
) -> tuple[ULID, bytes]:
    eid = ULID.from_datetime(at)
    b = format_entry(
        entry_id=eid,
        ts=at,
        actor=actor,
        source="api",
        client_version=None,
        content_hash="0" * 64,
        parent_id=None,
        content=content,
    )
    return eid, b


def _log_file(ws_root: Path, at: datetime) -> Path:
    logs = ws_root / "logs"
    logs.mkdir(exist_ok=True)
    return logs / f"{at.date().isoformat()}.md"


def test_extract_wal_slice_empty_logs_dir_returns_empty(tmp_path: Path) -> None:
    slice_ = extract_wal_slice(tmp_path, last_wal_id=None)
    assert isinstance(slice_, WalSlice)
    assert slice_.entries == []
    assert slice_.formatted_text == ""
    assert slice_.wal_first_id is None
    assert slice_.wal_last_id is None
    assert slice_.has_more is False


def test_extract_wal_slice_last_wal_id_none_returns_all(tmp_path: Path) -> None:
    next_day = _DAY + timedelta(days=1)
    _eid_a, ba = _make_entry_bytes(_DAY, content="a")
    _eid_b, bb = _make_entry_bytes(_DAY + timedelta(seconds=1), content="b")
    _eid_c, bc = _make_entry_bytes(next_day, content="c")
    _log_file(tmp_path, _DAY).write_bytes(ba + bb)
    _log_file(tmp_path, next_day).write_bytes(bc)

    slice_ = extract_wal_slice(tmp_path, last_wal_id=None)
    assert len(slice_.entries) == 3
    ids_in_order = [str(e.id) for e in slice_.entries]
    assert ids_in_order == sorted(ids_in_order)
    assert slice_.wal_first_id == ids_in_order[0]
    assert slice_.wal_last_id == ids_in_order[-1]


def test_extract_wal_slice_filters_by_last_wal_id(tmp_path: Path) -> None:
    _eid_a, ba = _make_entry_bytes(_DAY, content="a")
    eid_b, bb = _make_entry_bytes(_DAY + timedelta(seconds=1), content="b")
    eid_c, bc = _make_entry_bytes(_DAY + timedelta(seconds=2), content="c")
    _log_file(tmp_path, _DAY).write_bytes(ba + bb + bc)

    slice_ = extract_wal_slice(tmp_path, last_wal_id=str(eid_b))
    assert [str(e.id) for e in slice_.entries] == [str(eid_c)]


def test_extract_wal_slice_multi_file_global_ordering(tmp_path: Path) -> None:
    earlier = _DAY - timedelta(days=1)
    _eid_a, ba = _make_entry_bytes(_DAY, content="a")
    _eid_b, bb = _make_entry_bytes(_DAY + timedelta(seconds=1), content="b")
    _eid_c, bc = _make_entry_bytes(earlier, content="c")
    _log_file(tmp_path, _DAY).write_bytes(ba + bb)
    _log_file(tmp_path, earlier).write_bytes(bc)

    slice_ = extract_wal_slice(tmp_path, last_wal_id=None)
    ids = [str(e.id) for e in slice_.entries]
    assert ids == sorted(ids)


def test_extract_wal_slice_skips_log_files_older_than_cursor_date(tmp_path: Path) -> None:
    cursor_id, cursor_bytes = _make_entry_bytes(_DAY, content="cursor")
    _log_file(tmp_path, _DAY).write_bytes(cursor_bytes)
    # A newer entry stranded in a file dated two days before the cursor can only be
    # returned if that file is read, so its absence proves the file was pruned.
    stale_day = _DAY - timedelta(days=2)
    _stale_id, stale_bytes = _make_entry_bytes(_DAY + timedelta(hours=1), content="stale-file")
    _log_file(tmp_path, stale_day).write_bytes(stale_bytes)
    margin_day = _DAY - timedelta(days=1)
    margin_id, margin_bytes = _make_entry_bytes(_DAY + timedelta(hours=2), content="margin-file")
    _log_file(tmp_path, margin_day).write_bytes(margin_bytes)

    slice_ = extract_wal_slice(tmp_path, last_wal_id=str(cursor_id))

    assert [e.content for e in slice_.entries] == ["margin-file"]
    assert slice_.wal_last_id == str(margin_id)


def test_extract_wal_slice_unparseable_cursor_reads_every_file(tmp_path: Path) -> None:
    old_day = _DAY - timedelta(days=30)
    _eid, b = _make_entry_bytes(old_day, content="old")
    _log_file(tmp_path, old_day).write_bytes(b)

    slice_ = extract_wal_slice(tmp_path, last_wal_id="0")

    assert [e.content for e in slice_.entries] == ["old"]


def _three_entries(ws_root: Path) -> list[bytes]:
    chunks = [
        _make_entry_bytes(_DAY + timedelta(seconds=i), content=f"entry-{i}")[1]
        for i in range(3)
    ]
    _log_file(ws_root, _DAY).write_bytes(b"".join(chunks))
    return chunks


@pytest.mark.parametrize(
    ("budget_of", "expected_contents", "expected_has_more"),
    [
        pytest.param(lambda c: None, ["entry-0", "entry-1", "entry-2"], False, id="unbounded"),
        pytest.param(lambda c: 1, ["entry-0"], True, id="budget-below-one-entry-still-yields-one"),
        pytest.param(
            lambda c: len(c[0]) + len(c[1]) - 1, ["entry-0"], True, id="second-entry-does-not-fit"
        ),
        pytest.param(
            lambda c: len(c[0]) + len(c[1]), ["entry-0", "entry-1"], True, id="exact-fit"
        ),
        pytest.param(
            lambda c: sum(len(x) for x in c), ["entry-0", "entry-1", "entry-2"], False,
            id="budget-covers-backlog",
        ),
    ],
)
def test_extract_wal_slice_caps_serialized_bytes(
    tmp_path: Path,
    budget_of: Callable[[list[bytes]], int | None],
    expected_contents: list[str],
    expected_has_more: bool,
) -> None:
    chunks = _three_entries(tmp_path)
    max_bytes = budget_of(chunks)

    slice_ = extract_wal_slice(tmp_path, last_wal_id=None, max_bytes=max_bytes)

    assert [e.content for e in slice_.entries] == expected_contents
    assert slice_.has_more is expected_has_more
    assert slice_.formatted_text.encode() == b"".join(chunks[: len(expected_contents)])
