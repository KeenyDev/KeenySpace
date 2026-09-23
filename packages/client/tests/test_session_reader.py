"""Unit tests for daemon/session_reader.py — the implicit-capture write path."""

from __future__ import annotations

import asyncio
import itertools
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import keenyspace.daemon.session_reader as session_reader
import pytest
from keenyspace.daemon.session_reader import (
    MAX_INGEST_ATTEMPTS,
    RETRY_MAX_SECONDS,
    IngestRetry,
    IngestSkippedError,
    ReaderState,
    _dead_letter,
    _default_ingest,
    _extract_text,
    _read_delta,
    _tick,
    load_state,
    save_state,
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

    state = ReaderState()
    cursors = state.cursors
    await _tick(
        state,
        dead_letter_path=tmp_path / "dead.jsonl",
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

    state = ReaderState()
    cursors = state.cursors
    await _tick(
        state,
        dead_letter_path=tmp_path / "dead.jsonl",
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

    state = ReaderState()
    cursors, buffers = state.cursors, state.buffers
    await _tick(
        state,
        dead_letter_path=tmp_path / "dead.jsonl",
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

    state = ReaderState()
    cursors, buffers = state.cursors, state.buffers
    # A tiny timeout must make the tick return promptly instead of blocking.
    await asyncio.wait_for(
        _tick(
            state,
            dead_letter_path=tmp_path / "dead.jsonl",
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

    state = ReaderState()
    buffers = state.buffers

    # Two small appends, each below the threshold on its own.
    f.write_text(_transcript("/Users/dmitrydankov/BSW", [("user", "a" * 2500)]))
    await _tick(
        state,
        dead_letter_path=tmp_path / "dead.jsonl",
        projects_dir=projects,
        ingest_fn=fake_ingest,
        resolve_fn=_registered,
        min_delta_chars=4_000,
    )
    assert calls == []  # still under threshold

    with f.open("a") as fh:
        fh.write(json.dumps({"message": {"role": "assistant", "content": "b" * 2500}}) + "\n")
    await _tick(
        state,
        dead_letter_path=tmp_path / "dead.jsonl",
        projects_dir=projects,
        ingest_fn=fake_ingest,
        resolve_fn=_registered,
        min_delta_chars=4_000,
    )
    assert len(calls) == 1  # accumulated buffer crossed the threshold
    assert "a" * 100 in calls[0][1] and "b" * 100 in calls[0][1]
    assert buffers[str(f)] == ""  # cleared after ingest


@pytest.mark.asyncio
async def test_default_ingest_without_token_signals_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_token(*, interactive: bool = True) -> str | None:
        return None

    settings = SimpleNamespace(llm=SimpleNamespace(api_key_env="KS_TEST_LLM_KEY"))
    monkeypatch.setattr("keenyspace.config.get_client_settings", lambda: settings)
    monkeypatch.setattr("keenyspace.cli.login.ensure_token", no_token)

    with pytest.raises(IngestSkippedError):
        await _default_ingest("bsw", "text", "/src.jsonl")


@pytest.mark.asyncio
async def test_default_ingest_without_llm_key_signals_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def token(*, interactive: bool = True) -> str | None:
        return "ks_live_testkey"

    settings = SimpleNamespace(llm=SimpleNamespace(api_key_env="KS_TEST_LLM_KEY"))
    monkeypatch.delenv("KS_TEST_LLM_KEY", raising=False)
    monkeypatch.setattr("keenyspace.config.get_client_settings", lambda: settings)
    monkeypatch.setattr("keenyspace.cli.login.ensure_token", token)

    with pytest.raises(IngestSkippedError):
        await _default_ingest("bsw", "text", "/src.jsonl")


@pytest.mark.asyncio
async def test_tick_skipped_ingest_keeps_buffer_and_retries_when_idle(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects"
    body = _transcript("/Users/dmitrydankov/BSW", [("user", "x" * 5000)])
    f = _write_session(projects, "s1.jsonl", body)
    calls: list[str] = []
    credential_available = False

    async def ingest(slug: str, text: str, src: str) -> None:
        if not credential_available:
            raise IngestSkippedError("no_token")
        calls.append(text)

    state = ReaderState()
    cursors, buffers = state.cursors, state.buffers
    await _tick(
        state,
        dead_letter_path=tmp_path / "dead.jsonl",
        projects_dir=projects,
        ingest_fn=ingest,
        resolve_fn=_registered,
        min_delta_chars=4_000,
    )
    assert calls == []
    assert cursors[str(f)] == f.stat().st_size
    kept = buffers[str(f)]
    assert "x" * 5000 in kept

    # No new transcript bytes: the pending buffer alone must trigger the retry.
    credential_available = True
    await _tick(
        state,
        dead_letter_path=tmp_path / "dead.jsonl",
        projects_dir=projects,
        ingest_fn=ingest,
        resolve_fn=_registered,
        min_delta_chars=4_000,
    )
    assert calls == [kept]
    assert buffers[str(f)] == ""


@pytest.mark.asyncio
async def test_tick_caps_buffer_while_ingest_keeps_failing(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    proj = projects / "-Users-dmitrydankov-BSW"
    proj.mkdir(parents=True)
    f = proj / "s1.jsonl"
    f.write_text(_transcript("/Users/dmitrydankov/BSW", []))

    async def failing_ingest(slug: str, text: str, src: str) -> None:
        raise IngestSkippedError("no_token")

    state = ReaderState()
    buffers = state.buffers
    for marker in "abcde":
        with f.open("a") as fh:
            fh.write(json.dumps({"message": {"role": "user", "content": marker * 3000}}) + "\n")
        await _tick(
            state,
            dead_letter_path=tmp_path / "dead.jsonl",
            projects_dir=projects,
            ingest_fn=failing_ingest,
            resolve_fn=_registered,
            min_delta_chars=1_000,
            max_buffer_chars=7_000,
        )

    kept = buffers[str(f)]
    assert len(kept) <= 7_000
    assert "e" * 3000 in kept  # newest text survives
    assert "a" * 100 not in kept  # oldest text dropped first
    assert kept.startswith("user: ")  # trimmed on a line boundary


class _FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_tick_failing_ingest_backs_off_then_dead_letters(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    body = _transcript("/Users/dmitrydankov/BSW", [("user", "x" * 5000)])
    f = _write_session(projects, "s1.jsonl", body)
    dead = tmp_path / "dead.jsonl"
    clock = _FakeClock()
    calls: list[float] = []

    async def failing_ingest(slug: str, text: str, src: str) -> None:
        calls.append(clock.now)
        raise RuntimeError("server rejected")

    state = ReaderState()
    # 10-minute ticks over two days: far more ticks than allowed attempts.
    for _ in range(6 * 48):
        await _tick(
            state,
            dead_letter_path=dead,
            projects_dir=projects,
            ingest_fn=failing_ingest,
            resolve_fn=_registered,
            min_delta_chars=4_000,
            clock=clock,
        )
        clock.now += 600

    assert len(calls) == MAX_INGEST_ATTEMPTS
    gaps = [later - earlier for earlier, later in itertools.pairwise(calls)]
    assert gaps == [600, 1200, 2400, 4800]  # exponential backoff between attempts

    records = [json.loads(line) for line in dead.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["file"] == str(f)
    assert records[0]["attempts"] == MAX_INGEST_ATTEMPTS
    assert "x" * 5000 in records[0]["text"]
    assert stat.S_IMODE(dead.stat().st_mode) == 0o600
    assert state.buffers[str(f)] == ""
    assert str(f) not in state.retries


@pytest.mark.asyncio
async def test_tick_success_after_failure_clears_retry_state(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    body = _transcript("/Users/dmitrydankov/BSW", [("user", "x" * 5000)])
    f = _write_session(projects, "s1.jsonl", body)
    clock = _FakeClock()
    fail = True
    calls = 0

    async def flaky_ingest(slug: str, text: str, src: str) -> None:
        nonlocal calls
        calls += 1
        if fail:
            raise RuntimeError("transient")

    state = ReaderState()

    async def tick() -> None:
        await _tick(
            state,
            dead_letter_path=tmp_path / "dead.jsonl",
            projects_dir=projects,
            ingest_fn=flaky_ingest,
            resolve_fn=_registered,
            min_delta_chars=4_000,
            clock=clock,
        )

    await tick()
    assert state.retries[str(f)].attempts == 1

    clock.now += 300  # still inside the backoff window
    await tick()
    assert calls == 1

    fail = False
    clock.now += 300
    await tick()
    assert calls == 2
    assert state.buffers[str(f)] == ""
    assert str(f) not in state.retries


@pytest.mark.asyncio
async def test_tick_skipped_ingest_does_not_count_as_attempt(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    body = _transcript("/Users/dmitrydankov/BSW", [("user", "x" * 5000)])
    f = _write_session(projects, "s1.jsonl", body)
    calls = 0

    async def skipping_ingest(slug: str, text: str, src: str) -> None:
        nonlocal calls
        calls += 1
        raise IngestSkippedError("no_token")

    state = ReaderState()
    for _ in range(MAX_INGEST_ATTEMPTS + 2):
        await _tick(
            state,
            dead_letter_path=tmp_path / "dead.jsonl",
            projects_dir=projects,
            ingest_fn=skipping_ingest,
            resolve_fn=_registered,
            min_delta_chars=4_000,
        )

    assert calls == MAX_INGEST_ATTEMPTS + 2  # free skips retry every tick
    assert state.retries == {}
    assert "x" * 5000 in state.buffers[str(f)]
    assert not (tmp_path / "dead.jsonl").exists()


def test_state_round_trips_in_one_owner_only_file(tmp_path: Path) -> None:
    path = tmp_path / "ingest-state.json"
    state = ReaderState(
        cursors={"/a.jsonl": 42},
        buffers={"/a.jsonl": "user: hi", "/b.jsonl": ""},
        retries={"/a.jsonl": IngestRetry(2, 1234.5)},
    )
    save_state(path, state)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_state(path) == ReaderState(
        cursors={"/a.jsonl": 42},
        buffers={"/a.jsonl": "user: hi"},
        retries={"/a.jsonl": IngestRetry(2, 1234.5)},
    )


def test_state_loads_legacy_split_files_then_replaces_them(tmp_path: Path) -> None:
    path = tmp_path / "ingest-state.json"
    legacy_cursors = tmp_path / "ingest-cursors.json"
    legacy_buffers = tmp_path / "ingest-buffers.json"
    legacy_cursors.write_text(json.dumps({"/a.jsonl": 42}))
    legacy_buffers.write_text(json.dumps({"/a.jsonl": "user: hi"}))

    state = load_state(path)
    assert state == ReaderState(cursors={"/a.jsonl": 42}, buffers={"/a.jsonl": "user: hi"})

    save_state(path, state)
    assert not legacy_cursors.exists()
    assert not legacy_buffers.exists()
    assert load_state(path) == state


@pytest.mark.asyncio
async def test_tick_keeps_buffer_when_dead_letter_unwritable(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    body = _transcript("/Users/dmitrydankov/BSW", [("user", "x" * 5000)])
    f = _write_session(projects, "s1.jsonl", body)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    clock = _FakeClock()

    async def failing_ingest(slug: str, text: str, src: str) -> None:
        raise RuntimeError("server rejected")

    state = ReaderState(retries={str(f): IngestRetry(MAX_INGEST_ATTEMPTS - 1, 0.0)})
    await _tick(
        state,
        dead_letter_path=blocker / "dead.jsonl",
        projects_dir=projects,
        ingest_fn=failing_ingest,
        resolve_fn=_registered,
        min_delta_chars=4_000,
        clock=clock,
    )

    assert "x" * 5000 in state.buffers[str(f)]
    assert state.retries[str(f)].next_retry_at == clock.now + RETRY_MAX_SECONDS


def test_dead_letter_rotates_when_over_cap_and_keeps_one_rotation(tmp_path: Path) -> None:
    dead = tmp_path / "dead.jsonl"
    rotated = tmp_path / "dead.jsonl.1"

    def park(text: str) -> None:
        assert _dead_letter(
            dead, key="/s.jsonl", slug="bsw", text=text, attempts=5, now=1.0, max_bytes=100
        )

    park("a" * 200)
    assert not rotated.exists()
    park("b")
    assert "a" * 200 in rotated.read_text()
    assert [json.loads(line)["text"] for line in dead.read_text().splitlines()] == ["b"]

    park("c" * 200)
    park("d")
    assert "a" * 200 not in rotated.read_text()
    assert "c" * 200 in rotated.read_text()
    assert [json.loads(line)["text"] for line in dead.read_text().splitlines()] == ["d"]
    assert not (tmp_path / "dead.jsonl.2").exists()
    assert stat.S_IMODE(dead.stat().st_mode) == 0o600
    assert stat.S_IMODE(rotated.stat().st_mode) == 0o600


def test_dead_letter_under_cap_appends_without_rotating(tmp_path: Path) -> None:
    dead = tmp_path / "dead.jsonl"
    for text in ("one", "two"):
        assert _dead_letter(dead, key="/s.jsonl", slug="bsw", text=text, attempts=5, now=1.0)
    assert len(dead.read_text().splitlines()) == 2
    assert not (tmp_path / "dead.jsonl.1").exists()


async def _idle_tick(state: ReaderState, tmp_path: Path, dead: Path) -> None:
    async def never_ingest(slug: str, text: str, src: str) -> None:
        raise AssertionError("no ingest expected")

    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    await _tick(
        state,
        dead_letter_path=dead,
        projects_dir=projects,
        ingest_fn=never_ingest,
        resolve_fn=_registered,
        min_delta_chars=4_000,
        clock=_FakeClock(),
    )


@pytest.mark.asyncio
async def test_tick_prunes_state_of_deleted_transcript(tmp_path: Path) -> None:
    gone = str(tmp_path / "projects" / "p" / "gone.jsonl")
    dead = tmp_path / "dead.jsonl"
    state = ReaderState(
        cursors={gone: 42},
        buffers={gone: ""},
        retries={gone: IngestRetry(2, 0.0)},
    )

    await _idle_tick(state, tmp_path, dead)

    assert state == ReaderState()
    assert not dead.exists()


@pytest.mark.asyncio
async def test_tick_dead_letters_unsent_buffer_of_deleted_transcript(tmp_path: Path) -> None:
    gone = str(tmp_path / "projects" / "p" / "gone.jsonl")
    dead = tmp_path / "dead.jsonl"
    state = ReaderState(
        cursors={gone: 42},
        buffers={gone: "user: unsent"},
        retries={gone: IngestRetry(3, 0.0)},
    )

    await _idle_tick(state, tmp_path, dead)

    assert state == ReaderState()
    records = [json.loads(line) for line in dead.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["file"] == gone
    assert records[0]["text"] == "user: unsent"
    assert records[0]["attempts"] == 3
    assert records[0]["reason"] == "transcript_deleted"
    assert records[0]["workspace"] is None


@pytest.mark.asyncio
async def test_tick_keeps_deleted_transcript_buffer_when_dead_letter_unwritable(
    tmp_path: Path,
) -> None:
    gone = str(tmp_path / "projects" / "p" / "gone.jsonl")
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    state = ReaderState(cursors={gone: 42}, buffers={gone: "user: unsent"})

    await _idle_tick(state, tmp_path, blocker / "dead.jsonl")

    assert state.buffers[gone] == "user: unsent"


@pytest.mark.asyncio
async def test_tick_does_not_prune_on_transient_stat_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unreadable = str(tmp_path / "projects" / "locked" / "s.jsonl")
    state = ReaderState(cursors={unreadable: 42}, buffers={unreadable: "user: unsent"})
    real_stat = Path.stat

    def flaky_stat(self: Path, *args: object, **kwargs: object) -> object:
        if str(self) == unreadable:
            raise PermissionError(13, "denied")
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", flaky_stat)
    await _idle_tick(state, tmp_path, tmp_path / "dead.jsonl")

    assert state == ReaderState(cursors={unreadable: 42}, buffers={unreadable: "user: unsent"})
    assert not (tmp_path / "dead.jsonl").exists()


@pytest.mark.asyncio
async def test_tick_keeps_state_of_live_transcripts(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    f = _write_session(projects, "s1.jsonl", _transcript("/x", [("user", "hi")]))
    size = f.stat().st_size
    state = ReaderState(cursors={str(f): size}, buffers={str(f): "user: pending"})

    await _idle_tick(state, tmp_path, tmp_path / "dead.jsonl")

    assert state == ReaderState(cursors={str(f): size}, buffers={str(f): "user: pending"})


@pytest.mark.asyncio
async def test_tick_runs_blocking_file_io_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    _write_session(
        projects, "s1.jsonl", _transcript("/Users/dmitrydankov/BSW", [("user", "x" * 5000)])
    )
    offloaded: list[str] = []
    real_to_thread = asyncio.to_thread

    async def recording_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        offloaded.append(func.__name__)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(session_reader.asyncio, "to_thread", recording_to_thread)

    async def failing_ingest(slug: str, text: str, src: str) -> None:
        raise RuntimeError("server rejected")

    state = ReaderState()
    for _ in range(MAX_INGEST_ATTEMPTS):
        state.retries = {k: IngestRetry(r.attempts, 0.0) for k, r in state.retries.items()}
        await _tick(
            state,
            dead_letter_path=tmp_path / "dead.jsonl",
            projects_dir=projects,
            ingest_fn=failing_ingest,
            resolve_fn=_registered,
            min_delta_chars=4_000,
        )

    assert "_transcript_cwd" in offloaded
    assert "_dead_letter" in offloaded
    assert (tmp_path / "dead.jsonl").exists()
