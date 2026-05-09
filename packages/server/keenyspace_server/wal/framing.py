from __future__ import annotations

import html
from datetime import datetime

from ulid import ULID


def format_entry(
    entry_id: ULID,
    ts: datetime,
    actor: str,
    source: str,
    client_version: str | None,
    content_hash: str,
    parent_id: ULID | None,
    content: str,
) -> bytes:
    attrs = [
        f'id="{entry_id}"',
        f'ts="{ts.isoformat()}"',
        f'actor="{html.escape(actor, quote=True)}"',
        f'source="{html.escape(source, quote=True)}"',
    ]
    if client_version is not None:
        attrs.append(f'client_version="{html.escape(client_version, quote=True)}"')
    attrs.append(f'content_hash="{content_hash}"')
    if parent_id is not None:
        attrs.append(f'parent_id="{parent_id}"')

    safe_content = html.escape(content)
    line = f"<wal_entry {' '.join(attrs)}>{safe_content}</wal_entry>\n\n"
    return line.encode()
