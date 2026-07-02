"""Unit tests for daemon/session_reader.py — the implicit-capture write path."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from keenyspace.daemon.session_reader import (
    _extract_text,
    _read_delta,
    _tick,
)


def _transcript(cwd: str, turns: list[tuple[str, str]]) -> str:
    lines = [json.dumps({"cwd": cwd, "type": "summary"})]
    for role, text in turns:
        lines.append(json.dumps({"message": {"role": role, "content": text}}))
    return "\n".join(lines) + "\n"


def _registered(_cwd: str) -> tuple[str | None, str]:
    return "bsw", "workspace-map"


def _unregistered(_cwd: str) -> tuple[str | None, str]:
    return "metrikus-dogfood", "default"


def _write_session(projects: Path, name: str, body: str) -> Path:
    proj = projects / "-Users-dmitrydankov-BSW"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / name
    f.write_text(body, encoding="utf-8")
    return f


def test_extract_text_pulls_user_and_assistant_turns() -> None:
    raw = _transcript("/x", [("user", "hello"), ("assistant", "hi there")])
    out = _extract_text(raw)
    assert "user: hello" in out
    assert "assistant: hi there" in out
    # The cwd/summary record carries no message -> excluded.
    assert "summary" not in out


def test_read_delta_drops_partial_trailing_line(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    f.write_bytes(b'{"a":1}\n{"b":2}\n{"partial"')
    text, new_off = _read_delta(f, 0)
    assert text == '{"a":1}\n{"b":2}\n'
    assert new_off == len(b'{"a":1}\n{"b":2}\n')


def test_read_delta_caps_to_max_bytes(tmp_path: Path) -> None:
    f = tmp_path / "big.jsonl"
    f.write_text('{"n":1}\n' * 1000, encoding="utf-8")  # 8000 bytes
    text, new_off = _read_delta(f, 0, max_bytes=100)
    # Capped well under the file; only complete lines, offset advanced by them.
    assert 0 < new_off <= 100
    assert new_off < f.stat().st_size
    assert text.endswith("\n")


@pytest.mark.asyncio
async def test_tick_ingests_registered_session(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    body = _transcript("/Users/dmitrydankov/BSW", [("user", "x" * 5000)])
    f = _write_session(projects, "s1.jsonl", body)
    calls: list[tuple[str, str, str]] = []

    async def fake_ingest(slug: str, text: str, src: str) -> None:
        calls.append((slug, text, src))

    cursors: dict[str, int] = {}
    await _tick(
        cursors,
        {},
        projects_dir=projects,
        ingest_fn=fake_ingest,
        resolve_fn=_registered,
        min_delta_chars=4_000,
    )
    assert len(calls) == 1
    assert calls[0][0] == "bsw"
    assert "user: " in calls[0][1]
    assert cursors[str(f)] == f.stat().st_size


@pytest.mark.asyncio
async def test_tick_skips_unregistered_without_ingest(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    body = _transcript("/Users/dmitrydankov/Other", [("user", "x" * 5000)])
    f = _write_session(projects, "s1.jsonl", body)
    calls: list[tuple[str, str, str]] = []

    async def fake_ingest(slug: str, text: str, src: str) -> None:
        calls.append((slug, text, src))

    cursors: dict[str, int] = {}
    await _tick(
        cursors,
        {},
        projects_dir=projects,
        ingest_fn=fake_ingest,
        resolve_fn=_unregistered,
        min_delta_chars=4_000,
    )
    assert calls == []
    # Skipped forward so the unregistered session is never reprocessed.
    assert cursors[str(f)] == f.stat().st_size


@pytest.mark.asyncio
async def test_tick_below_threshold_buffers_and_advances(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    body = _transcript("/Users/dmitrydankov/BSW", [("user", "tiny")])
    f = _write_session(projects, "s1.jsonl", body)
    calls: list[tuple[str, str, str]] = []

    async def fake_ingest(slug: str, text: str, src: str) -> None:
        calls.append((slug, text, src))

    cursors: dict[str, int] = {}
    buffers: dict[str, str] = {}
    await _tick(
        cursors,
        buffers,
        projects_dir=projects,
        ingest_fn=fake_ingest,
        resolve_fn=_registered,
        min_delta_chars=4_000,
    )
    assert calls == []  # below threshold: not ingested yet
    assert cursors[str(f)] == f.stat().st_size  # cursor advanced (no wedge)
    assert "tiny" in buffers[str(f)]  # signal retained in the buffer


@pytest.mark.asyncio
async def test_tick_ingest_timeout_does_not_wedge(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    body = _transcript("/Users/dmitrydankov/BSW", [("user", "x" * 5000)])
    f = _write_session(projects, "s1.jsonl", body)

    async def hung_ingest(slug: str, text: str, src: str) -> None:
        await asyncio.sleep(3600)  # never returns within the tick's budget

    cursors: dict[str, int] = {}
    buffers: dict[str, str] = {}
    # A tiny timeout must make the tick return promptly instead of blocking.
    await asyncio.wait_for(
        _tick(
            cursors,
            buffers,
            projects_dir=projects,
            ingest_fn=hung_ingest,
            resolve_fn=_registered,
            min_delta_chars=4_000,
            ingest_timeout=0.05,
        ),
        timeout=5.0,
    )
    # Cursor advanced (window consumed) but buffer retained for retry.
    assert cursors[str(f)] == f.stat().st_size
    assert buffers[str(f)]


@pytest.mark.asyncio
async def test_tick_buffer_accumulates_across_ticks_then_ingests(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    proj = projects / "-Users-dmitrydankov-BSW"
    proj.mkdir(parents=True)
    f = proj / "s1.jsonl"
    calls: list[tuple[str, str, str]] = []

    async def fake_ingest(slug: str, text: str, src: str) -> None:
        calls.append((slug, text, src))

    cursors: dict[str, int] = {}
    buffers: dict[str, str] = {}

    # Two small appends, each below the threshold on its own.
    f.write_text(_transcript("/Users/dmitrydankov/BSW", [("user", "a" * 2500)]))
    await _tick(cursors, buffers, projects_dir=projects, ingest_fn=fake_ingest,
                resolve_fn=_registered, min_delta_chars=4_000)
    assert calls == []  # still under threshold

    with f.open("a") as fh:
        fh.write(json.dumps({"message": {"role": "assistant", "content": "b" * 2500}}) + "\n")
    await _tick(cursors, buffers, projects_dir=projects, ingest_fn=fake_ingest,
                resolve_fn=_registered, min_delta_chars=4_000)
    assert len(calls) == 1  # accumulated buffer crossed the threshold
    assert "a" * 100 in calls[0][1] and "b" * 100 in calls[0][1]
    assert buffers[str(f)] == ""  # cleared after ingest
