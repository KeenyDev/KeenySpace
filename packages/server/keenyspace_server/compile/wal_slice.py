from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ulid import ULID

from keenyspace_server.wal.framing import format_entry
from keenyspace_server.wal.parser import WalEntry, parse_wal

# WAL files are named by the UTC append date while entry ids carry their own
# timestamp; one day of slack absorbs midnight rollover and clock skew between the two.
_FILE_DATE_SAFETY_MARGIN = timedelta(days=1)


@dataclass
class WalSlice:
    entries: list[WalEntry] = field(default_factory=list)
    formatted_text: str = ""
    has_more: bool = False

    @property
    def wal_first_id(self) -> str | None:
        return str(self.entries[0].id) if self.entries else None

    @property
    def wal_last_id(self) -> str | None:
        return str(self.entries[-1].id) if self.entries else None


def _cutoff_date(last_wal_id: str | None) -> date | None:
    if last_wal_id is None:
        return None
    try:
        cursor_date = ULID.from_str(last_wal_id).datetime.date()
    except ValueError:
        return None
    return cursor_date - _FILE_DATE_SAFETY_MARGIN


def _log_files_since(logs_dir: Path, cutoff: date | None) -> list[Path]:
    files: list[Path] = []
    for log_file in sorted(logs_dir.glob("*.md")):
        if cutoff is not None:
            try:
                file_date = date.fromisoformat(log_file.stem)
            except ValueError:
                file_date = None
            if file_date is not None and file_date < cutoff:
                continue
        files.append(log_file)
    return files


def _format(entry: WalEntry) -> bytes:
    return format_entry(
        entry_id=ULID.from_str(str(entry.id)),
        ts=entry.ts,
        actor=entry.actor,
        source=entry.source,
        client_version=entry.client_version,
        content_hash=entry.content_hash,
        parent_id=entry.parent_id,
        content=entry.content,
    )


def extract_wal_slice(
    ws_root: Path, last_wal_id: str | None, *, max_bytes: int | None = None
) -> WalSlice:
    """Return WAL entries with id > last_wal_id (ULID lex order), oldest first.

    Only log files dated on or after the cursor's UTC date (minus a safety margin)
    are read, so a pass costs O(recent WAL) rather than O(total history).

    When `max_bytes` is set, the slice stops before the entry that would push the
    serialized text past the budget (at least one entry is always included) and
    `has_more` reports whether newer entries remain for a follow-up pass.
    """
    logs_dir = ws_root / "logs"
    if not logs_dir.is_dir():
        return WalSlice()

    all_entries: list[WalEntry] = []
    for log_file in _log_files_since(logs_dir, _cutoff_date(last_wal_id)):
        try:
            text = log_file.read_text(encoding="utf-8")
        except OSError:
            continue
        all_entries.extend(parse_wal(text))

    all_entries.sort(key=lambda e: str(e.id))

    if last_wal_id is not None:
        pending = [e for e in all_entries if str(e.id) > last_wal_id]
    else:
        pending = all_entries

    chunks: list[bytes] = []
    total = 0
    for entry in pending:
        chunk = _format(entry)
        if max_bytes is not None and chunks and total + len(chunk) > max_bytes:
            break
        chunks.append(chunk)
        total += len(chunk)

    return WalSlice(
        entries=pending[: len(chunks)],
        formatted_text=b"".join(chunks).decode("utf-8"),
        has_more=len(chunks) < len(pending),
    )
