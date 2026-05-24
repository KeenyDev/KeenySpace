"""JSONL dispatch for daemon socket events.

Wave 5 ships the transport. The session-start source=compact branch returns
an explicit not-implemented payload so hook integration tests can verify the
round-trip without depending on an LLM. Wave 6 (Plan 06) replaces the stub
with the pydantic-ai post-compact orchestrator per D-09.
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
        # Wave 6 will implement the LLM orchestration; Wave 5 ships a stub
        # so hook integration tests can verify the request-response transport.
        response = {
            "ok": False,
            "content": None,
            "error": "not_implemented_in_wave_5",
        }
        writer.write(json.dumps(response).encode() + b"\n")
        try:  # noqa: SIM105 — await cannot live inside contextlib.suppress
            await writer.drain()
        except (OSError, ConnectionResetError):
            pass
        return
    # All other kinds (incl. post-compact per F-09): fire-and-forget — just logged.
