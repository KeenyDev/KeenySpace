from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime

from ulid import ULID

_ENTRY_RE = re.compile(
    r"<wal_entry ([^>]+)>(.+?)</wal_entry>",
    re.DOTALL,
)
# Invariant: framing.py escapes all attribute values with html.escape(x, quote=True),
# converting " → &quot;. No literal " can appear inside a value, so [^"]* terminates
# correctly. Breaking this invariant in framing.py would silently truncate attr values.
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


@dataclass
class WalEntry:
    id: ULID
    ts: datetime
    actor: str
    source: str
    client_version: str | None
    content_hash: str
    parent_id: ULID | None
    content: str


def parse_wal(text: str) -> list[WalEntry]:
    entries: list[WalEntry] = []
    for m in _ENTRY_RE.finditer(text):
        attrs_str = m.group(1)
        raw_content = m.group(2)

        attrs: dict[str, str] = {}
        for attr_m in _ATTR_RE.finditer(attrs_str):
            attrs[attr_m.group(1)] = html.unescape(attr_m.group(2))

        entry = WalEntry(
            id=ULID.from_str(attrs["id"]),
            ts=datetime.fromisoformat(attrs["ts"]),
            actor=attrs.get("actor", ""),
            source=attrs.get("source", ""),
            client_version=attrs.get("client_version"),
            content_hash=attrs.get("content_hash", ""),
            parent_id=ULID.from_str(attrs["parent_id"]) if "parent_id" in attrs else None,
            content=html.unescape(raw_content),
        )
        entries.append(entry)
    return entries
