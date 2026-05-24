"""Claude Code transcript tail-reader for the post-compact daemon flow.

Per Phase 5 D-09: hook envelope carries `transcript_path` (a JSONL file),
not an inline excerpt. The daemon does the heavy I/O on its own event
loop via `asyncio.to_thread` so the hook process stays under 1s.

The tail heuristic keeps the last ~3000 tokens (12000 chars at the
4-chars/token rule of thumb). The first partial line is dropped so the
downstream agent prompt never sees a half JSON object.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

MAX_EXCERPT_CHARS = 12_000


async def read_transcript_excerpt(
    transcript_path: str | Path,
    max_chars: int = MAX_EXCERPT_CHARS,
) -> str | None:
    path = Path(transcript_path)
    try:
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    if not content:
        return ""
    if len(content) <= max_chars:
        return content
    tail = content[-max_chars:]
    nl = tail.find("\n")
    if nl >= 0:
        return tail[nl + 1 :]
    return tail
