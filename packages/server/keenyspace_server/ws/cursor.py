from __future__ import annotations

import base64
import binascii
import json
from typing import Any


def encode_cursor(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data, sort_keys=True).encode()).rstrip(b"=").decode()


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("malformed cursor (base64 decode failed)") from exc
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("malformed cursor (json decode failed)") from exc
    if not isinstance(data, dict):
        raise ValueError("malformed cursor (not a dict)")
    return data


def encode_mtime_cursor(mtime_ns: int, path: str) -> str:
    return encode_cursor({"mtime_ns": mtime_ns, "path": path})


def decode_mtime_cursor(cursor: str) -> tuple[int, str]:
    data = decode_cursor(cursor)
    mtime_ns = data.get("mtime_ns")
    path = data.get("path")
    if not isinstance(mtime_ns, int) or not isinstance(path, str):
        raise ValueError("malformed mtime cursor (missing mtime_ns or path)")
    return mtime_ns, path
