"""Unit tests for daemon/transcript.py — tail heuristic + asyncio offload."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from keenyspace.daemon.transcript import (
    MAX_EXCERPT_CHARS,
    read_transcript_excerpt,
)


@pytest.mark.asyncio
async def test_read_short_transcript(tmp_path: Path) -> None:
    f = tmp_path / "short.jsonl"
    body = '{"role":"user","content":"hello"}\n' * 3
    f.write_text(body, encoding="utf-8")
    out = await read_transcript_excerpt(f)
    assert out == body


@pytest.mark.asyncio
async def test_read_long_transcript_truncates_to_max_chars(tmp_path: Path) -> None:
    f = tmp_path / "long.jsonl"
    line = '{"role":"user","content":"' + ("x" * 180) + '"}\n'
    f.write_text(line * 100, encoding="utf-8")
    assert f.stat().st_size > 5000
    out = await read_transcript_excerpt(f, max_chars=5000)
    assert out is not None
    assert len(out) <= 5000
    assert out.startswith("{"), f"expected line-boundary trim, got: {out[:50]!r}"


@pytest.mark.asyncio
async def test_read_missing_file_returns_none(tmp_path: Path) -> None:
    out = await read_transcript_excerpt(tmp_path / "nope.jsonl")
    assert out is None


@pytest.mark.asyncio
async def test_read_empty_file_returns_empty_string(tmp_path: Path) -> None:
    f = tmp_path / "empty.jsonl"
    f.touch()
    out = await read_transcript_excerpt(f)
    assert out == ""


@pytest.mark.asyncio
async def test_read_uses_asyncio_to_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "obs.jsonl"
    f.write_text("line\n", encoding="utf-8")

    calls = {"n": 0}
    real_to_thread = asyncio.to_thread

    async def wrapper(func: Any, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(
        "keenyspace.daemon.transcript.asyncio.to_thread", wrapper
    )
    await read_transcript_excerpt(f)
    assert calls["n"] == 1


def test_max_excerpt_chars_default() -> None:
    assert MAX_EXCERPT_CHARS == 12_000
