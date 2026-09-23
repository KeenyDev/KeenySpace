"""Atomic increment of the dropped-event counter.

`fcntl.flock(LOCK_EX | LOCK_NB)`: if another hook is holding the lock, the
increment is skipped rather than blocked on — hooks MUST exit fast (<1s) even
when concurrent hooks race for the counter. Any OS-level error (read-only FS,
permission, etc.) is swallowed: a missed counter update is preferable to a
hook that does not exit cleanly.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
from datetime import UTC, datetime

from keenyspace.paths import DROPPED_JSON


def increment(kind: str) -> None:
    """Increment counter for `kind`. Never raises — hook MUST exit 0."""
    if sys.platform == "win32":
        return
    try:
        DROPPED_JSON.parent.mkdir(parents=True, exist_ok=True)
        # Pre-create with mode 0o600 if the file doesn't exist.
        # `open(... "a+")` on first install honours the process umask
        # (typically 0o644), exposing counter contents to other users
        # for the brief window before the atomic-replace finishes. The
        # rest of the security-by-default surface (auth.json) is 0o600
        # from creation; preserve that posture here.
        if not DROPPED_JSON.exists():
            with contextlib.suppress(FileExistsError):
                fd = os.open(
                    DROPPED_JSON,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                os.close(fd)
        with open(DROPPED_JSON, "r+") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Another hook holds the lock; skip rather than block.
                return
            try:
                f.seek(0)
                raw = f.read()
                parsed: object = json.loads(raw) if raw.strip() else {"version": 1, "by_kind": {}}
                state: dict[str, object]
                if isinstance(parsed, dict) and "by_kind" in parsed:
                    state = parsed
                else:
                    state = {"version": 1, "by_kind": {}}
                by_kind_raw = state.get("by_kind")
                by_kind: dict[str, dict[str, object]] = (
                    by_kind_raw if isinstance(by_kind_raw, dict) else {}
                )
                state["by_kind"] = by_kind
                bucket = by_kind.setdefault(kind, {"count": 0, "last_ts": None})
                current = bucket.get("count", 0)
                bucket["count"] = (current if isinstance(current, int) else 0) + 1
                now = datetime.now(UTC).isoformat()
                bucket["last_ts"] = now
                state["last_updated"] = now
                tmp = DROPPED_JSON.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(state, indent=2))
                os.chmod(tmp, 0o600)
                os.replace(tmp, DROPPED_JSON)
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError, json.JSONDecodeError:
        # Hook must always exit 0; missed increment is acceptable.
        return
