"""Background reader: poll Claude Code transcripts and ingest deltas into the WAL.

Independent of hooks. On a timer the daemon scans ``~/.claude/projects/*/*.jsonl``,
maps each session's recorded ``cwd`` to a workspace slug (registered directories
ONLY -- the workspace-map / slug-marker, never the ``default`` fallback), and
ingests the bytes appended since the last cursor via the server-driven ``ingest``
flow. Per-file byte cursors persist in ``ingest-cursors.json`` so a delta is never
ingested twice. Distillation + the actual ``append_log`` happen server-side inside
the ingest agent; compile then materialises pages on its own debounce/backstop.

This is the implicit-capture write path. The hooks remain only for post-compact
re-injection (the read path); capture no longer depends on them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog

from keenyspace import paths
from keenyspace.workspace_inference import resolve_workspace_slug

log = structlog.get_logger(__name__)

DEFAULT_INTERVAL_SECONDS = 600
# Below this many extracted chars (~1k tokens at 4 chars/token) we wait for the
# session to accumulate more rather than spend an ingest call on a tiny delta.
MIN_DELTA_CHARS = 4_000
# Wall-clock cap on a single ingest. Without it, one hung LLM/HTTP call would
# block the whole poll loop indefinitely (a stuck ingest once wedged the reader
# for hours). On timeout the buffer is kept and retried on the next tick.
INGEST_TIMEOUT_SECONDS = 180
# Hard cap on raw bytes consumed per file per tick. Bounds a single ingest's
# input (cost + provider context budget) and drains a large backlog -- a long
# session or an empty first-run cursor over a multi-MB transcript -- in bounded
# chunks across ticks instead of one oversized, overflow-prone call.
MAX_DELTA_BYTES = 120_000

# resolve_workspace_slug sources that mean "this cwd maps to a registered
# workspace". "default" (config.yaml fallback) and "unresolved" are NOT captured.
_REGISTERED_SOURCES = frozenset({"explicit", "env", "slug-marker", "workspace-map"})

# (slug, extracted_text, source_path) -> None
IngestFn = Callable[[str, str, str], Awaitable[None]]
# cwd -> (slug, source)
ResolveFn = Callable[[str], tuple[str | None, str]]


def _claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _load_cursors(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}


def _save_cursors(path: Path, cursors: dict[str, int]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(cursors), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("session_reader.cursor_persist_failed", error=str(exc))


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
        return
    if not os.environ.get(settings.llm.api_key_env):
        log.warning("session_reader.no_llm_key", env=settings.llm.api_key_env)
        return
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
    cursors: dict[str, int],
    buffers: dict[str, str],
    *,
    projects_dir: Path,
    ingest_fn: IngestFn,
    resolve_fn: ResolveFn,
    min_delta_chars: int,
    max_delta_bytes: int = MAX_DELTA_BYTES,
    ingest_timeout: float = INGEST_TIMEOUT_SECONDS,
) -> None:
    if not projects_dir.is_dir():
        return
    for proj in sorted(projects_dir.iterdir()):
        if not proj.is_dir():
            continue
        for transcript in sorted(proj.glob("*.jsonl")):
            key = str(transcript)
            offset = cursors.get(key, 0)
            try:
                size = transcript.stat().st_size
            except OSError:
                continue
            if size <= offset:
                continue
            cwd = _transcript_cwd(transcript)
            if not cwd:
                continue
            slug, source = resolve_fn(cwd)
            if slug is None or source not in _REGISTERED_SOURCES:
                # Unregistered cwd: skip forward so we never reprocess it.
                cursors[key] = size
                continue

            raw, new_offset = await asyncio.to_thread(
                _read_delta, transcript, offset, max_delta_bytes
            )
            if new_offset <= offset:
                # No complete line within the cap. A single record larger than the
                # cap (huge tool_result / snapshot) would otherwise wedge the file
                # forever -- skip past it so the reader keeps draining.
                if size - offset > max_delta_bytes:
                    cursors[key] = offset + max_delta_bytes
                    log.warning("session_reader.oversized_record_skipped", file=key)
                continue

            # Advance past the consumed window regardless of text density, so a
            # window dominated by non-text records never wedges the cursor. Human/
            # assistant text is buffered across windows until it is worth an ingest,
            # so low-text windows don't drop signal.
            extracted = _extract_text(raw)
            cursors[key] = new_offset
            if extracted:
                buffers[key] = (buffers.get(key, "") + "\n" + extracted).strip()
            if len(buffers.get(key, "")) < min_delta_chars:
                continue

            text = buffers[key]
            try:
                await asyncio.wait_for(ingest_fn(slug, text, key), timeout=ingest_timeout)
            except Exception as exc:  # one bad session must not stall the loop
                # Keep the buffer (cursor already advanced) so the text is retried,
                # not lost, on the next tick.
                reason = "timeout" if isinstance(exc, TimeoutError) else str(exc)
                log.warning("session_reader.ingest_failed", file=key, error=reason)
                continue
            buffers[key] = ""
            log.info("session_reader.ingested", workspace=slug, file=key, chars=len(text))


async def run_transcript_reader(
    stop_event: asyncio.Event,
    *,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    min_delta_chars: int = MIN_DELTA_CHARS,
    ingest_fn: IngestFn | None = None,
    resolve_fn: ResolveFn | None = None,
    projects_dir: Path | None = None,
    cursors_path: Path | None = None,
) -> None:
    """Poll loop: ingest transcript deltas until ``stop_event`` is set."""
    fn = ingest_fn or _default_ingest
    resolver: ResolveFn = resolve_fn or (lambda cwd: resolve_workspace_slug(cwd=cwd))
    pdir = projects_dir or _claude_projects_dir()
    cpath = cursors_path or paths.INGEST_CURSORS
    cursors = _load_cursors(cpath)
    # Per-file extracted-text buffers, in-memory: accumulate low-text windows
    # across ticks so signal isn't dropped while the cursor keeps advancing.
    buffers: dict[str, str] = {}
    log.info("session_reader.started", projects_dir=str(pdir), interval=interval_seconds)
    while not stop_event.is_set():
        try:
            await _tick(
                cursors,
                buffers,
                projects_dir=pdir,
                ingest_fn=fn,
                resolve_fn=resolver,
                min_delta_chars=min_delta_chars,
            )
            _save_cursors(cpath, cursors)
        except Exception as exc:  # the loop must survive any single-tick failure
            log.warning("session_reader.tick_failed", error=str(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
