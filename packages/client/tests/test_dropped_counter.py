"""dropped.json atomic counter — flock invariants plus a schema that carries no PII."""

from __future__ import annotations

import importlib
import json
import stat
import threading
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def _reload_dropped(temp_config_dir: dict[str, Path], short_xdg_state: Path) -> object:
    import keenyspace.paths as paths_mod

    importlib.reload(paths_mod)
    import keenyspace.hooks.dropped as dropped_mod

    importlib.reload(dropped_mod)
    return dropped_mod


def test_increment_appends_kind(_reload_dropped) -> None:
    dropped_mod = _reload_dropped
    for _ in range(3):
        dropped_mod.increment("post-tool")
    import keenyspace.paths as paths_mod

    state = json.loads(paths_mod.DROPPED_JSON.read_text())
    assert state["by_kind"]["post-tool"]["count"] == 3
    assert state["by_kind"]["post-tool"]["last_ts"] is not None
    assert state["version"] == 1


def test_dropped_json_mode_0600(_reload_dropped) -> None:
    dropped_mod = _reload_dropped
    dropped_mod.increment("post-tool")
    import keenyspace.paths as paths_mod

    mode = stat.S_IMODE(paths_mod.DROPPED_JSON.stat().st_mode)
    assert mode == 0o600, f"dropped.json mode {oct(mode)} != 0600"


def test_increment_never_raises(_reload_dropped) -> None:
    dropped_mod = _reload_dropped

    def boom(*_args: object, **_kwargs: object) -> object:
        raise OSError("simulated FS failure")

    with patch("builtins.open", boom):
        # Must not raise — hook MUST exit 0.
        dropped_mod.increment("post-tool")


def test_dropped_json_only_counts_no_pii(_reload_dropped) -> None:
    dropped_mod = _reload_dropped
    dropped_mod.increment("post-tool")
    dropped_mod.increment("session-start.compact")
    import keenyspace.paths as paths_mod

    state = json.loads(paths_mod.DROPPED_JSON.read_text())
    # Schema must contain only count + last_ts per kind. No payload, no transcript,
    # no user identifiers — the counter must not become a PII sink.
    allowed_keys = {"count", "last_ts"}
    for kind, bucket in state["by_kind"].items():
        assert set(bucket.keys()) <= allowed_keys, (
            f"kind {kind} has extra keys {set(bucket.keys()) - allowed_keys}"
        )


def test_concurrent_increments_serialise_under_flock(
    _reload_dropped,
) -> None:
    """Threads racing on increment never produce a corrupt file.

    Because we use LOCK_NB (Pitfall #6), losing threads skip rather than
    block — so the final count is bounded by [1, 100], not strictly 100.
    The invariant we MUST hold: the file remains parseable JSON and the
    count is consistent (no torn writes).
    """
    dropped_mod = _reload_dropped
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(10):
                dropped_mod.increment("post-tool")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    import keenyspace.paths as paths_mod

    # File must be valid JSON despite the race
    state = json.loads(paths_mod.DROPPED_JSON.read_text())
    bucket = state["by_kind"]["post-tool"]
    assert 1 <= bucket["count"] <= 100
