"""Background reader: poll Claude Code transcripts and ingest deltas into the WAL.

Independent of hooks. On a timer the daemon scans ``~/.claude/projects/*/*.jsonl``,
maps each session's recorded ``cwd`` to a workspace slug (registered directories
ONLY -- the workspace-map / slug-marker, never the ``default`` fallback), and
ingests the bytes appended since the last cursor via the server-driven ``ingest``
flow. Per-file byte cursors, pending text and retry state persist together in
``ingest-state.json`` so a delta is never ingested twice. Distillation and the
actual ``append_log`` happen server-side inside
the ingest agent; compile then materialises pages on its own debounce/backstop.

This is the implicit-capture write path. The hooks cover only post-compact
re-injection (the read path); capture does not depend on them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from keenyspace import paths
from keenyspace.fs.atomic import write_atomic_secret
from keenyspace.workspace_inference import resolve_workspace_slug

log = structlog.get_logger(__name__)

DEFAULT_INTERVAL_SECONDS = 600
# Below this many extracted chars (~1k tokens at 4 chars/token) we wait for the
# session to accumulate more rather than spend an ingest call on a tiny delta.
MIN_DELTA_CHARS = 4_000
# Wall-clock cap on a single ingest. Without it, one hung LLM/HTTP call would
# block the whole poll loop indefinitely (a stuck ingest can wedge the reader
# for hours). On timeout the buffer is kept and retried on the next tick.
INGEST_TIMEOUT_SECONDS = 180
# Hard cap on raw bytes consumed per file per tick. Bounds a single ingest's
# input (cost + provider context budget) and drains a large backlog -- a long
# session or an empty first-run cursor over a multi-MB transcript -- in bounded
# chunks across ticks instead of one oversized, overflow-prone call.
MAX_DELTA_BYTES = 120_000
# Cap on buffered extracted text per file. While ingest keeps failing every tick
# appends another window; without a cap the buffer (and every retry's payload)
# grows without bound. Oldest text is dropped first.
MAX_BUFFER_CHARS = 2 * MAX_DELTA_BYTES

# resolve_workspace_slug sources that mean "this cwd maps to a registered
# workspace". "default" (config.yaml fallback) and "unresolved" are NOT captured.
_REGISTERED_SOURCES = frozenset({"explicit", "env", "slug-marker", "workspace-map"})

# (slug, extracted_text, source_path) -> None; raise IngestSkippedError to keep the buffer.
IngestFn = Callable[[str, str, str], Awaitable[None]]
# cwd -> (slug, source)
ResolveFn = Callable[[str], tuple[str | None, str]]


# Real ingest failures (timeout, server/LLM error) cost tokens and may have
# partially appended before failing, so they are retried with exponential backoff
# and dead-lettered after MAX_INGEST_ATTEMPTS instead of every tick forever.
RETRY_BASE_SECONDS = DEFAULT_INTERVAL_SECONDS
RETRY_MAX_SECONDS = 6 * 3600
MAX_INGEST_ATTEMPTS = 5

_LEGACY_CURSORS_NAME = "ingest-cursors.json"
_LEGACY_BUFFERS_NAME = "ingest-buffers.json"
_DEAD_LETTER_NAME = "ingest-dead-letter.jsonl"
# Once the dead-letter file passes this size it is rotated to ``<name>.1`` (one
# rotation kept, the previous ``.1`` is replaced) so it cannot grow forever.
DEAD_LETTER_MAX_BYTES = 5 * 1024 * 1024


class IngestSkippedError(Exception):
    """Ingest could not run (no credential / LLM key); the buffer must be kept.

    Skips cost nothing, so they are retried every tick without backoff.
    """


@dataclass
class IngestRetry:
    attempts: int
    next_retry_at: float


@dataclass
class ReaderState:
    """Per-transcript byte cursors, pending extracted text, and failure backoff."""

    cursors: dict[str, int] = field(default_factory=dict)
    buffers: dict[str, str] = field(default_factory=dict)
    retries: dict[str, IngestRetry] = field(default_factory=dict)


def _claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError, OSError, json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _int_map(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {k: int(v) for k, v in raw.items() if isinstance(v, (int, float))}


def _str_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, str) and v}


def load_state(path: Path) -> ReaderState:
    """Load reader state, falling back to the legacy split cursor/buffer files."""
    data = _read_json_object(path)
    if not data:
        return ReaderState(
            cursors=_int_map(_read_json_object(path.with_name(_LEGACY_CURSORS_NAME))),
            buffers=_str_map(_read_json_object(path.with_name(_LEGACY_BUFFERS_NAME))),
        )
    retries_raw = data.get("retries")
    retries: dict[str, IngestRetry] = {}
    if isinstance(retries_raw, dict):
        for key, entry in retries_raw.items():
            if not isinstance(entry, dict):
                continue
            attempts = entry.get("attempts")
            next_retry_at = entry.get("next_retry_at")
            if isinstance(attempts, int) and isinstance(next_retry_at, (int, float)):
                retries[key] = IngestRetry(attempts, float(next_retry_at))
    return ReaderState(
        cursors=_int_map(data.get("cursors")),
        buffers=_str_map(data.get("buffers")),
        retries=retries,
    )


def save_state(path: Path, state: ReaderState) -> None:
    """Persist cursors, buffers and retry state in one atomic owner-only write.

    One file means a crash can never leave cursors advanced past text whose
    buffer was not saved (or vice versa). Buffers hold transcript text -> 0600.
    """
    doc = {
        "version": 1,
        "cursors": state.cursors,
        "buffers": {k: v for k, v in state.buffers.items() if v},
        "retries": {
            k: {"attempts": r.attempts, "next_retry_at": r.next_retry_at}
            for k, r in state.retries.items()
        },
    }
    try:
        write_atomic_secret(path, json.dumps(doc).encode("utf-8"))
        for legacy in (_LEGACY_CURSORS_NAME, _LEGACY_BUFFERS_NAME):
            path.with_name(legacy).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("session_reader.state_persist_failed", error=str(exc))


def _rotate_dead_letter(path: Path, max_bytes: int) -> None:
    try:
        if path.stat().st_size <= max_bytes:
            return
        os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        return
    except OSError as exc:
        # Parking the text matters more than bounding the file; append anyway.
        log.warning("session_reader.dead_letter_rotate_failed", error=str(exc))


def _dead_letter(
    path: Path,
    *,
    key: str,
    slug: str | None,
    text: str,
    attempts: int,
    now: float,
    reason: str | None = None,
    max_bytes: int = DEAD_LETTER_MAX_BYTES,
) -> bool:
    """Append one record to the owner-only dead-letter file; False if it failed.

    Blocking file I/O: call it through ``asyncio.to_thread`` from the event loop.
    """
    record: dict[str, Any] = {
        "file": key,
        "workspace": slug,
        "attempts": attempts,
        "failed_at": now,
        "text": text,
    }
    if reason is not None:
        record["reason"] = reason
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_dead_letter(path, max_bytes)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(record) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        log.error("session_reader.dead_letter_failed", file=key, error=str(exc))
        return False
    log.warning(
        "session_reader.dead_lettered",
        file=key,
        workspace=slug,
        attempts=attempts,
        reason=reason,
        dead_letter=str(path),
    )
    return True


def _confirmed_missing(keys: Iterable[str]) -> list[str]:
    """Keys whose transcript is definitely gone; transient stat errors are not proof."""
    missing: list[str] = []
    for key in keys:
        try:
            Path(key).stat()
        except FileNotFoundError:
            missing.append(key)
        except OSError:
            continue
    return missing


async def _prune_deleted_transcripts(
    state: ReaderState,
    *,
    seen: set[str],
    dead_letter_path: Path,
    clock: Callable[[], float],
) -> None:
    """Drop state for deleted transcripts, parking any unsent buffer first."""
    tracked = (state.cursors.keys() | state.buffers.keys() | state.retries.keys()) - seen
    if not tracked:
        return
    for key in await asyncio.to_thread(_confirmed_missing, sorted(tracked)):
        text = state.buffers.get(key, "")
        if text:
            retry = state.retries.get(key)
            parked = await asyncio.to_thread(
                _dead_letter,
                dead_letter_path,
                key=key,
                slug=None,
                text=text,
                attempts=retry.attempts if retry is not None else 0,
                now=clock(),
                reason="transcript_deleted",
            )
            if not parked:
                continue
        state.cursors.pop(key, None)
        state.buffers.pop(key, None)
        state.retries.pop(key, None)
        log.info("session_reader.pruned_deleted_transcript", file=key)


def _backoff_seconds(attempts: int) -> float:
    return float(min(RETRY_BASE_SECONDS * 2 ** (attempts - 1), RETRY_MAX_SECONDS))


def _append_capped(existing: str, extracted: str, max_chars: int) -> tuple[str, bool]:
    combined = (existing + "\n" + extracted).strip()
    if len(combined) <= max_chars:
        return combined, False
    tail = combined[-max_chars:]
    nl = tail.find("\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1 :]
    return tail, True


def _transcript_cwd(path: Path) -> str | None:
    """The session cwd is recorded on the transcript's JSONL records."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get("cwd") if isinstance(obj, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunks)
    return ""


def _extract_text(raw: str) -> str:
    """Reduce a JSONL slice to readable user/assistant turns for distillation."""
    parts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") if isinstance(obj, dict) else None
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        text = _content_text(msg.get("content"))
        if role in ("user", "assistant") and text.strip():
            parts.append(f"{role}: {text}")
    return "\n".join(parts)


def _read_delta(path: Path, offset: int, max_bytes: int = MAX_DELTA_BYTES) -> tuple[str, int]:
    """Return (complete-line text appended since ``offset``, new byte offset).

    At most ``max_bytes`` are consumed per call so a large backlog drains in
    bounded chunks. A partial trailing line (session still writing, or the
    max-bytes cut landing mid-line) is left for the next tick by advancing the
    offset only to the last newline.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return "", offset
    if size <= offset:
        return "", offset
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read(max_bytes)
    except OSError:
        return "", offset
    nl = data.rfind(b"\n")
    if nl < 0:
        return "", offset
    complete = data[: nl + 1]
    return complete.decode("utf-8", errors="replace"), offset + len(complete)


async def _default_ingest(slug: str, text: str, source_path: str) -> None:
    # Deferred imports: keep the daemon cold-start cheap; pydantic-ai / httpx are
    # only touched once a delta actually needs ingesting.
    from keenyspace.cli.login import ensure_token
    from keenyspace.clients.llm import run_server_driven_command
    from keenyspace.clients.mcp import get_instructions
    from keenyspace.config import get_client_settings

    settings = get_client_settings()
    # Headless: never fall back to the interactive device flow (it would block the
    # poll loop). No durable/refreshable credential -> skip this tick's ingest.
    api_key = await ensure_token(interactive=False)
    if not api_key:
        log.warning("session_reader.no_token", workspace=slug)
        raise IngestSkippedError("no_token")
    if not os.environ.get(settings.llm.api_key_env):
        log.warning("session_reader.no_llm_key", env=settings.llm.api_key_env)
        raise IngestSkippedError("no_llm_key")
    instructions = await get_instructions(
        settings.server_url,
        api_key,
        workspace=slug,
        command="ingest",
        context={"source_path": source_path, "source_content": text},
    )
    await run_server_driven_command(
        server_url=settings.server_url,
        api_key=api_key,
        instructions=instructions,
        user_prompt=text,
        llm_model=f"{settings.llm.provider}:{settings.llm.model}",
    )


async def _tick(
    state: ReaderState,
    *,
    projects_dir: Path,
    ingest_fn: IngestFn,
    resolve_fn: ResolveFn,
    min_delta_chars: int,
    dead_letter_path: Path,
    max_delta_bytes: int = MAX_DELTA_BYTES,
    max_buffer_chars: int = MAX_BUFFER_CHARS,
    ingest_timeout: float = INGEST_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.time,
) -> None:
    if not projects_dir.is_dir():
        return
    cursors, buffers, retries = state.cursors, state.buffers, state.retries
    seen: set[str] = set()
    for proj in sorted(projects_dir.iterdir()):
        if not proj.is_dir():
            continue
        for transcript in sorted(proj.glob("*.jsonl")):
            key = str(transcript)
            seen.add(key)
            offset = cursors.get(key, 0)
            try:
                size = transcript.stat().st_size
            except OSError:
                continue
            has_new_bytes = size > offset
            # A full buffer is retried even when the session is idle; otherwise
            # text whose ingest was skipped/failed would be stranded until new bytes.
            if not has_new_bytes and len(buffers.get(key, "")) < min_delta_chars:
                continue
            cwd = await asyncio.to_thread(_transcript_cwd, transcript)
            if not cwd:
                continue
            slug, source = resolve_fn(cwd)
            if slug is None or source not in _REGISTERED_SOURCES:
                # Unregistered cwd: skip forward so we never reprocess it.
                cursors[key] = size
                buffers.pop(key, None)
                retries.pop(key, None)
                continue

            if has_new_bytes:
                raw, new_offset = await asyncio.to_thread(
                    _read_delta, transcript, offset, max_delta_bytes
                )
                if new_offset > offset:
                    # Advance past the consumed window regardless of text density, so
                    # a window dominated by non-text records never wedges the cursor.
                    # Human/assistant text is buffered across windows until it is
                    # worth an ingest, so low-text windows don't drop signal.
                    cursors[key] = new_offset
                    extracted = _extract_text(raw)
                    if extracted:
                        buffers[key], trimmed = _append_capped(
                            buffers.get(key, ""), extracted, max_buffer_chars
                        )
                        if trimmed:
                            log.warning(
                                "session_reader.buffer_trimmed",
                                file=key,
                                max_chars=max_buffer_chars,
                            )
                elif size - offset > max_delta_bytes:
                    # No complete line within the cap. A single record larger than
                    # the cap (huge tool_result / snapshot) would otherwise wedge the
                    # file forever -- skip past it so the reader keeps draining.
                    cursors[key] = offset + max_delta_bytes
                    log.warning("session_reader.oversized_record_skipped", file=key)
            if len(buffers.get(key, "")) < min_delta_chars:
                continue
            retry = retries.get(key)
            if retry is not None and clock() < retry.next_retry_at:
                continue

            text = buffers[key]
            try:
                await asyncio.wait_for(ingest_fn(slug, text, key), timeout=ingest_timeout)
            except IngestSkippedError as exc:
                log.info("session_reader.ingest_skipped", file=key, reason=str(exc))
                continue
            except Exception as exc:  # one bad session must not stall the loop
                reason = "timeout" if isinstance(exc, TimeoutError) else str(exc)
                attempts = (retry.attempts if retry is not None else 0) + 1
                now = clock()
                if attempts >= MAX_INGEST_ATTEMPTS:
                    if await asyncio.to_thread(
                        _dead_letter,
                        dead_letter_path,
                        key=key,
                        slug=slug,
                        text=text,
                        attempts=attempts,
                        now=now,
                    ):
                        buffers[key] = ""
                        retries.pop(key, None)
                    else:
                        # Never drop text we could not park: keep it, retry rarely.
                        retries[key] = IngestRetry(attempts, now + RETRY_MAX_SECONDS)
                    continue
                delay = _backoff_seconds(attempts)
                retries[key] = IngestRetry(attempts, now + delay)
                log.warning(
                    "session_reader.ingest_failed",
                    file=key,
                    error=reason,
                    attempts=attempts,
                    retry_in=delay,
                )
                continue
            buffers[key] = ""
            retries.pop(key, None)
            log.info("session_reader.ingested", workspace=slug, file=key, chars=len(text))
    await _prune_deleted_transcripts(
        state, seen=seen, dead_letter_path=dead_letter_path, clock=clock
    )


async def run_transcript_reader(
    stop_event: asyncio.Event,
    *,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    min_delta_chars: int = MIN_DELTA_CHARS,
    ingest_fn: IngestFn | None = None,
    resolve_fn: ResolveFn | None = None,
    projects_dir: Path | None = None,
    state_path: Path | None = None,
) -> None:
    """Poll loop: ingest transcript deltas until ``stop_event`` is set."""
    fn = ingest_fn or _default_ingest
    resolver: ResolveFn = resolve_fn or (lambda cwd: resolve_workspace_slug(cwd=cwd))
    pdir = projects_dir or _claude_projects_dir()
    spath = state_path or paths.INGEST_STATE
    dead_letter_path = spath.with_name(_DEAD_LETTER_NAME)
    state = load_state(spath)
    log.info("session_reader.started", projects_dir=str(pdir), interval=interval_seconds)
    while not stop_event.is_set():
        try:
            await _tick(
                state,
                projects_dir=pdir,
                ingest_fn=fn,
                resolve_fn=resolver,
                min_delta_chars=min_delta_chars,
                dead_letter_path=dead_letter_path,
            )
            save_state(spath, state)
        except Exception as exc:  # the loop must survive any single-tick failure
            log.warning("session_reader.tick_failed", error=str(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
