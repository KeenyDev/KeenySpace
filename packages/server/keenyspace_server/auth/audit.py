"""Audit log writer — пишет в `audit_log` без plaintext credentials (D-18, T-3-10).

Events: auth.login.success/failure, auth.logout, auth.token.refresh,
auth.api_key.minted/revoked. NO auth.api_key.used (high cardinality, Pitfall F).

`name` clipping (N-5): user-supplied free-form поле клипается до 128 chars matching
DB column constraint `api_keys.name Mapped[String(128)]` — PII / log-injection защита.
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
