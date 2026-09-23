"""POST / GET / DELETE /v1/api/auth/api-keys.

The plaintext key is shown exactly once, in the POST response; the listing
never returns it. Revocation is soft (it stamps revoked_at). Revoking a key
that belongs to somebody else answers 404 rather than 403, so the endpoint
does not confirm that the key id exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status

from keenyspace_server.auth.api_keys import ApiKeyService, StaleCredentialError
from keenyspace_server.auth.schemas import (
    ApiKeyListItem,
    ApiKeyMintRequest,
    ApiKeyMintResponse,
)
from keenyspace_server.auth.user import User

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
    user = request.user
    # A key must not be able to extend itself: an expiring or leaked key could
    # otherwise mint a non-expiring successor.
    if not isinstance(user, User) or user.source != "oidc" or user.issued_at is None:
        log.warning("auth.api_key.mint_refused", reason="not_oidc", sub=user.identity)
        raise HTTPException(
            status_code=403,
            detail="API keys are minted with an OIDC login (keenyspace login), not with a key",
        )
    expires_at = (
        datetime.now(UTC) + timedelta(days=body.expires_in_days)
        if body.expires_in_days is not None
        else None
    )
    try:
        result = await service.mint(
            user_sub=user.sub,
            name=body.name,
            credential_issued_at=user.issued_at,
            expires_at=expires_at,
        )
    except StaleCredentialError as exc:
        raise HTTPException(
            status_code=403,
            detail="token predates your latest group change; log in again to mint keys",
        ) from exc
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
