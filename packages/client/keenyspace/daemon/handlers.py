"""JSONL dispatch for daemon socket events.

Wave 6 (D-09): session-start source=compact now invokes the pydantic-ai
post-compact orchestrator and writes a response payload back on the same
connection. Every other kind (incl. post-compact per F-09) is pure
fire-and-forget — daemon just logged the event.
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
    # All other kinds (incl. post-compact per F-09): fire-and-forget — just logged.
