"""JSONL dispatch for daemon socket events.

session-start with source=compact invokes the pydantic-ai post-compact
orchestrator and writes a response payload back on the same connection. Every
other kind, post-compact included, is pure fire-and-forget: the daemon only
logs the event.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

log = structlog.get_logger(__name__)


async def dispatch(envelope: dict[str, Any], writer: asyncio.StreamWriter) -> None:
    kind = envelope.get("kind")
    source = envelope.get("source")
    log.info(
        "daemon.event",
        kind=kind,
        source=source,
        workspace_slug=envelope.get("workspace_slug"),
    )
    if kind == "session-start" and source == "compact":
        # Deferred import: keeps daemon cold-start cheap; pydantic-ai is only
        # touched once a compact event actually arrives.
        from keenyspace.daemon.post_compact import assemble_context

        response = await assemble_context(envelope)
        writer.write(json.dumps(response).encode() + b"\n")
        try:  # noqa: SIM105 — await cannot live inside contextlib.suppress
            await writer.drain()
        except (OSError, ConnectionResetError):
            pass
        return
    # All other kinds, post-compact included: fire-and-forget — just logged.
