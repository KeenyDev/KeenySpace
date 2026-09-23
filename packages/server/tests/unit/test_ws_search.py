from __future__ import annotations

from pathlib import Path


def test_list_md_paths_empty_dir(tmp_path: Path) -> None:
    from keenyspace_server.ws.search import list_md_paths

    ws = tmp_path / "ws"
    ws.mkdir()
    assert list_md_paths(ws) == []


def test_list_md_paths_skips_keenyspace_obsidian_logs(tmp_path: Path) -> None:
    from keenyspace_server.ws.search import list_md_paths

    ws = tmp_path / "ws"
    for sub in (".keenyspace", ".obsidian", "logs"):
        (ws / sub).mkdir(parents=True)
        (ws / sub / "skip.md").write_text("nope")
    (ws / "kept.md").write_text("yes")
    result = list_md_paths(ws)
    assert result == ["kept.md"]


def test_list_md_paths_prefix_filter(tmp_path: Path) -> None:
    from keenyspace_server.ws.search import list_md_paths

    ws = tmp_path / "ws"
    (ws / "concepts").mkdir(parents=True)
    (ws / "notes").mkdir(parents=True)
    (ws / "concepts" / "a.md").write_text("alpha")
    (ws / "concepts" / "b.md").write_text("bravo")
    (ws / "notes" / "c.md").write_text("charlie")
    (ws / "index.md").write_text("index")
    result = list_md_paths(ws, prefix="concepts/")
    assert result == ["concepts/a.md", "concepts/b.md"]


def test_list_md_paths_sorted_stable(tmp_path: Path) -> None:
    from keenyspace_server.ws.search import list_md_paths

    ws = tmp_path / "ws"
    ws.mkdir()
    names = ["e.md", "c.md", "a.md", "d.md", "b.md"]
    for name in names:
        (ws / name).write_text("content")
    result = list_md_paths(ws)
    assert result == sorted(result)
    assert set(result) == set(names)


def test_search_workspace_files_content_match(tmp_path: Path) -> None:
    from keenyspace_server.ws.search import search_workspace_files

    ws = tmp_path / "ws"
    (ws / "notes").mkdir(parents=True)
    (ws / "notes" / "foo.md").write_text("alpha bravo charlie")
    (ws / "other.md").write_text("delta echo")
    result = search_workspace_files(ws, "bravo")
    assert "notes/foo.md" in result
    assert "other.md" not in result


def test_search_workspace_files_filename_match(tmp_path: Path) -> None:
    from keenyspace_server.ws.search import search_workspace_files

    ws = tmp_path / "ws"
    (ws / "notes").mkdir(parents=True)
    (ws / "notes" / "foo.md").write_text("nothing relevant")
    (ws / "bar.md").write_text("also nothing")
    result = search_workspace_files(ws, "foo")
    assert "notes/foo.md" in result
    assert "bar.md" not in result


def test_search_workspace_files_case_insensitive(tmp_path: Path) -> None:
    from keenyspace_server.ws.search import search_workspace_files

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "page.md").write_text("Contains Foobar in content")
    result = search_workspace_files(ws, "foobar")
    assert "page.md" in result


def test_search_workspace_files_skips_keenyspace(tmp_path: Path) -> None:
    from keenyspace_server.ws.search import search_workspace_files

    ws = tmp_path / "ws"
    (ws / ".keenyspace").mkdir(parents=True)
    (ws / ".keenyspace" / "secret.md").write_text("secret content foobar")
    (ws / "public.md").write_text("public content")
    result = search_workspace_files(ws, "foobar")
    assert not any(".keenyspace" in p for p in result)


def _seed_search_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    for name in ("a.md", "b.md", "c.md", "d.md", "e.md"):
        (ws / name).write_text("needle here")
    for name in ("aa.md", "bb.md"):
        (ws / name).write_text("nothing")
    return ws


def test_search_workspace_files_returns_sorted_matches(tmp_path: Path) -> None:
    from keenyspace_server.ws.search import search_workspace_files

    ws = _seed_search_ws(tmp_path)
    assert search_workspace_files(ws, "needle") == ["a.md", "b.md", "c.md", "d.md", "e.md"]


def test_search_workspace_files_resumes_strictly_after_keyset(tmp_path: Path) -> None:
    from keenyspace_server.ws.search import search_workspace_files

    ws = _seed_search_ws(tmp_path)
    assert search_workspace_files(ws, "needle", after="b.md", limit=2) == ["c.md", "d.md"]
    assert search_workspace_files(ws, "needle", after="bb.md") == ["c.md", "d.md", "e.md"]
    assert search_workspace_files(ws, "needle", after="e.md") == []


def test_search_workspace_files_skip_discards_leading_matches(tmp_path: Path) -> None:
    from keenyspace_server.ws.search import search_workspace_files

    ws = _seed_search_ws(tmp_path)
    assert search_workspace_files(ws, "needle", skip=3, limit=5) == ["d.md", "e.md"]


def test_search_workspace_files_stops_reading_once_limit_reached(
    tmp_path: Path, monkeypatch
) -> None:
    from keenyspace_server.ws.search import search_workspace_files

    ws = _seed_search_ws(tmp_path)
    real_read_bytes = Path.read_bytes
    read: list[str] = []

    def _tracking_read_bytes(self: Path) -> bytes:
        read.append(self.name)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _tracking_read_bytes)
    assert search_workspace_files(ws, "needle", after="a.md", limit=2) == ["b.md", "c.md"]
    assert read == ["aa.md", "b.md", "bb.md", "c.md"]


def test_search_workspace_files_missing_root_is_empty(tmp_path: Path) -> None:
    from keenyspace_server.ws.search import search_workspace_files

    assert search_workspace_files(tmp_path / "absent", "x", limit=3) == []
