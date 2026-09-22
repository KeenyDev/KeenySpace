from __future__ import annotations

from datetime import UTC, datetime, timedelta

from keenyspace_server.wal.framing import format_entry
from keenyspace_server.wal.parser import parse_wal
from ulid import ULID


def _entry(content: str, offset_ms: int) -> bytes:
    ts = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC) + timedelta(milliseconds=offset_ms)
    return format_entry(
        entry_id=ULID.from_datetime(ts),
        ts=ts,
        actor="dev:default",
        source="api",
        client_version=None,
        content_hash="sha256:abc",
        parent_id=None,
        content=content,
    )


def test_empty_entry_does_not_swallow_next_entry() -> None:
    text = b"".join(
        [_entry("", 0), _entry("second fact", 1), _entry("third", 2)]
    ).decode()

    entries = parse_wal(text)

    assert [e.content for e in entries] == ["", "second fact", "third"]


def test_empty_entry_round_trips_alone() -> None:
    entries = parse_wal(_entry("", 0).decode())

    assert len(entries) == 1
    assert entries[0].content == ""
