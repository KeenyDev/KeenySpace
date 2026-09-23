from __future__ import annotations

import os
from pathlib import Path


def _touch(p: Path, mtime_ns: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    os.utime(p, ns=(mtime_ns, mtime_ns))


def test_scan_recent_empty_dir(tmp_path: Path) -> None:
    from keenyspace_server.ws.recent import scan_recent_changes

    ws = tmp_path / "ws"
    ws.mkdir()
    result = scan_recent_changes(ws)
    assert result == []


def test_scan_recent_descending_mtime(tmp_path: Path) -> None:
    from keenyspace_server.ws.recent import scan_recent_changes

    ws = tmp_path / "ws"
    ws.mkdir()
    _touch(ws / "a.md", 1_000_000_000_000_000_000)
    _touch(ws / "b.md", 3_000_000_000_000_000_000)
    _touch(ws / "c.md", 2_000_000_000_000_000_000)
    result = scan_recent_changes(ws)
    assert [path for _, path in result] == ["b.md", "c.md", "a.md"]


def test_scan_recent_path_asc_tiebreak(tmp_path: Path) -> None:
    from keenyspace_server.ws.recent import scan_recent_changes

    ws = tmp_path / "ws"
    ws.mkdir()
    same_ns = 2_000_000_000_000_000_000
    _touch(ws / "z.md", same_ns)
    _touch(ws / "a.md", same_ns)
    result = scan_recent_changes(ws)
    assert [path for _, path in result] == ["a.md", "z.md"]


def test_scan_recent_since_filter(tmp_path: Path) -> None:
    from keenyspace_server.ws.recent import scan_recent_changes

    ws = tmp_path / "ws"
    ws.mkdir()
    _touch(ws / "old.md", 1_000_000_000_000_000_000)
    _touch(ws / "mid.md", 2_000_000_000_000_000_000)
    _touch(ws / "new.md", 3_000_000_000_000_000_000)
    result = scan_recent_changes(ws, since_ns=2_000_000_000_000_000_000)
    paths = [path for _, path in result]
    assert "old.md" not in paths
    assert "mid.md" in paths
    assert "new.md" in paths


def test_scan_recent_skips_keenyspace(tmp_path: Path) -> None:
    from keenyspace_server.ws.recent import scan_recent_changes

    ws = tmp_path / "ws"
    ws.mkdir()
    _touch(ws / "visible.md", 2_000_000_000_000_000_000)
    _touch(ws / ".keenyspace" / "secret.md", 3_000_000_000_000_000_000)
    result = scan_recent_changes(ws)
    paths = [path for _, path in result]
    assert "visible.md" in paths
    assert ".keenyspace/secret.md" not in paths
