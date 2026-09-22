"""POST/GET/DELETE /v1/api/auth/api-keys — D-06/D-09.

Plaintext key shown ровно один раз через POST response (T-3-10).
List response НЕ возвращает plaintext.
Revoke = soft (UPDATE revoked_at); cross-user revoke → 404 (T-3-12 existence-leak hide).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status

from keenyspace_server.auth.api_keys import ApiKeyService
from keenyspace_server.auth.schemas import (
    ApiKeyListItem,
    ApiKeyMintRequest,
    ApiKeyMintResponse,
)

log = structlog.get_logger(__name__)
router = APIRouter()


def _get_service(request: Request) -> ApiKeyService:
    return request.app.state.api_key_service  # type: ignore[no-any-return]


@router.post(
    "",
    response_model=ApiKeyMintResponse,
    status_code=status.HTTP_201_CREATED,
)
async def mint_api_key(
    body: ApiKeyMintRequest,
    request: Request,
    service: ApiKeyService = Depends(_get_service),  # noqa: B008
) -> ApiKeyMintResponse:
    expires_at = (
        datetime.now(UTC) + timedelta(days=body.expires_in_days)
        if body.expires_in_days is not None
        else None
    )
    result = await service.mint(
        user_sub=request.user.identity, name=body.name, expires_at=expires_at
    )
    return ApiKeyMintResponse(**result)


@router.get("", response_model=list[ApiKeyListItem])
async def list_api_keys(
    request: Request,
    service: ApiKeyService = Depends(_get_service),  # noqa: B008
) -> list[ApiKeyListItem]:
    user_sub = request.user.identity
    rows = await service.list_for_user(user_sub)
    return [ApiKeyListItem(**r) for r in rows]


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: UUID,
    request: Request,
    service: ApiKeyService = Depends(_get_service),  # noqa: B008
) -> None:
    if not await service.revoke(key_id, request.user.identity):
        raise HTTPException(status_code=404, detail="not found")
