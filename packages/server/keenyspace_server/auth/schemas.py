"""Pydantic schemas for the API-key mint / list endpoints.

The plaintext key (`key`) exists ONLY on ApiKeyMintResponse: the mint response
is the single moment a caller ever sees it. The list schema has no such field,
so a listing cannot leak keys.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ApiKeyMintRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyMintResponse(BaseModel):
    id: UUID
    name: str
    key: str
    key_prefix: str
    last4: str
    created_at: datetime
    expires_at: datetime | None


class ApiKeyListItem(BaseModel):
    id: UUID
    name: str
    key_prefix: str
    last4: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    expires_at: datetime | None


class ApiKeyRevokeAllRequest(BaseModel):
    sub: str = Field(..., min_length=1, max_length=256)


class ApiKeyRevokeAllResponse(BaseModel):
    sub: str
    revoked: int
