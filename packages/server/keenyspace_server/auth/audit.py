"""Audit log writer: appends to `audit_log`, never with plaintext credentials.

Events: auth.login.success/failure, auth.logout, auth.token.refresh,
auth.api_key.minted/revoked. Key USE is deliberately not audited — one event
per request would swamp the log.

The user-supplied `name` payload field is clipped to 128 characters, matching
the `api_keys.name` column, to bound what an attacker can push into the log.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.db.models import AuditLog

_NAME_MAX = 128


def _clip_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(payload)
    name = out.get("name")
    if isinstance(name, str) and len(name) > _NAME_MAX:
        out["name"] = name[:_NAME_MAX]
    return out


async def write_audit(
    session: AsyncSession,
    *,
    actor_sub: str,
    action: str,
    workspace_uuid: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    row = AuditLog(
        id=uuid4(),
        actor_sub=actor_sub,
        action=action,
        workspace_uuid=workspace_uuid,
        payload=_clip_payload(payload or {}),
        ts=datetime.now(UTC),
    )
    session.add(row)
