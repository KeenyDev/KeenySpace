from __future__ import annotations

import re
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
    pattern = re.compile("bravo", re.IGNORECASE)
    result = search_workspace_files(ws, pattern)
    assert "notes/foo.md" in result
    assert "other.md" not in result


def test_search_workspace_files_filename_match(tmp_path: Path) -> None:
    from keenyspace_server.ws.search import search_workspace_files

    ws = tmp_path / "ws"
    (ws / "notes").mkdir(parents=True)
    (ws / "notes" / "foo.md").write_text("nothing relevant")
    (ws / "bar.md").write_text("also nothing")
    pattern = re.compile("foo", re.IGNORECASE)
    result = search_workspace_files(ws, pattern)
    assert "notes/foo.md" in result
    assert "bar.md" not in result


def test_search_workspace_files_case_insensitive(tmp_path: Path) -> None:
    from keenyspace_server.ws.search import search_workspace_files

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "page.md").write_text("Contains Foobar in content")
    pattern = re.compile("foobar", re.IGNORECASE)
    result = search_workspace_files(ws, pattern)
    assert "page.md" in result


def test_search_workspace_files_skips_keenyspace(tmp_path: Path) -> None:
    from keenyspace_server.ws.search import search_workspace_files

    ws = tmp_path / "ws"
    (ws / ".keenyspace").mkdir(parents=True)
    (ws / ".keenyspace" / "secret.md").write_text("secret content foobar")
    (ws / "public.md").write_text("public content")
    pattern = re.compile("foobar", re.IGNORECASE)
    result = search_workspace_files(ws, pattern)
    assert not any(".keenyspace" in p for p in result)
