"""Pydantic schemas для API-key mint/list endpoints (D-09).

Plaintext key (`key` field) присутствует ТОЛЬКО в ApiKeyMintResponse — нигде иначе
(list response не возвращает plaintext; T-3-10 mitigation).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ApiKeyMintRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)


class ApiKeyMintResponse(BaseModel):
    id: UUID
    name: str
    key: str
    key_prefix: str
    last4: str
    created_at: datetime


class ApiKeyListItem(BaseModel):
    id: UUID
    name: str
    key_prefix: str
    last4: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
