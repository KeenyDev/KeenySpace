"""UDS client used by hook entry points.

Hard timeouts:
  - fire-and-forget: 50ms connect cap
  - session-start source=compact: 200ms connect + 800ms readline

On any socket failure or timeout the call increments the dropped counter
and returns (fail-open). Hooks MUST exit 0 — never raise out of this
module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from typing import Any

from keenyspace.hooks.dropped import increment
from keenyspace.paths import DAEMON_SOCK

FIRE_AND_FORGET_CONNECT_S = 0.05
REQ_RESP_CONNECT_S = 0.2
REQ_RESP_READ_S = 0.8


async def fire_and_forget(envelope: dict[str, Any]) -> None:
    counter_key = str(envelope.get("kind", "unknown"))
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(DAEMON_SOCK)),
            timeout=FIRE_AND_FORGET_CONNECT_S,
        )
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError):
        increment(counter_key)
        print(
            "WARN: keenyspace daemon socket unreachable; event dropped",
            file=sys.stderr,
        )
        return
    try:
        writer.write(json.dumps(envelope).encode() + b"\n")
        await writer.drain()
    except OSError:
        increment(counter_key)
    finally:
        writer.close()
        try:  # noqa: SIM105 — await cannot live inside contextlib.suppress
            await writer.wait_closed()
        except OSError:
            pass


async def request_response(envelope: dict[str, Any], *, counter_key: str) -> str:
    """Return assembled context text or empty string on fail-open."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(DAEMON_SOCK)),
            timeout=REQ_RESP_CONNECT_S,
        )
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError):
        increment(counter_key)
        return ""
    try:
        writer.write(json.dumps(envelope).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=REQ_RESP_READ_S)
        if not line:
            increment(counter_key)
            return ""
        payload = json.loads(line.decode())
        if not isinstance(payload, dict):
            increment(counter_key)
            return ""
        if payload.get("ok"):
            content = payload.get("content")
            return content if isinstance(content, str) else ""
        return ""
    except (TimeoutError, json.JSONDecodeError, OSError):
        increment(counter_key)
        return ""
    finally:
        writer.close()
        with contextlib.suppress(OSError, AttributeError):
            await_close = writer.wait_closed()
            if asyncio.iscoroutine(await_close):
                try:  # noqa: SIM105 — await cannot live inside contextlib.suppress
                    await await_close
                except OSError:
                    pass
