"""OIDC HTTP endpoints — D-01..D-07 + D-16.

GET  /v1/api/auth/discovery — public; advertises the IdP issuer to the CLI
GET  /v1/api/auth/login    — public; Authlib authorize_redirect → IdP
GET  /v1/api/auth/callback — public; code exchange; user-upsert; set ks_at+ks_rt+ks_idt
POST /v1/api/auth/refresh  — authed (via ks_rt cookie); rotate cookies
POST /v1/api/auth/logout   — authed; create_logout_url; clear cookies
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from authlib.integrations.base_client.errors import OAuthError  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from keenyspace_server.auth.audit import write_audit
from keenyspace_server.auth.oidc import OidcClient
from keenyspace_server.db.session import get_db

log = structlog.get_logger(__name__)
router = APIRouter()


def _settings(request: Request) -> Any:
    return request.app.state.settings


def _oauth(request: Request) -> Any:
    return request.app.state.oauth


def _oidc(request: Request) -> OidcClient:
    client: OidcClient = request.app.state.oidc_client
    return client


def _set_auth_cookies(
    response: Response,
    *,
    settings: Any,
    access_token: str,
    refresh_token: str | None,
    id_token: str | None,
    expires_in: int,
    refresh_expires_in: int | None = None,
) -> None:
    secure = settings.auth.cookie_secure
    response.set_cookie(
        "ks_at",
        access_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        path=settings.auth.cookie_path_ks_at,
        max_age=expires_in or 3600,
    )
    if refresh_token:
        if refresh_expires_in is not None:
            rt_max_age = int(refresh_expires_in)
        else:
            log.warning("auth.cookie.no_refresh_expires_in_falling_back_14d")
            rt_max_age = 86400 * 14
        response.set_cookie(
            "ks_rt",
            refresh_token,
            httponly=True,
            secure=secure,
            samesite="strict",
            path=settings.auth.cookie_path_ks_rt,
            max_age=rt_max_age,
        )
    if id_token:
        response.set_cookie(
            "ks_idt",
            id_token,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/v1/api/auth/logout",
            max_age=86400 * 14,
        )


def _clear_auth_cookies(response: Response, settings: Any) -> None:
    response.delete_cookie("ks_at", path=settings.auth.cookie_path_ks_at)
    response.delete_cookie("ks_rt", path=settings.auth.cookie_path_ks_rt)
    response.delete_cookie("ks_idt", path="/v1/api/auth/logout")


@router.get("/discovery")
async def discovery(request: Request) -> JSONResponse:
    # Public IdP-discovery shim. `keenyspace login` hits this first to learn the
    # Authentik issuer; without it the operator must export
    # KEENYSPACE_AUTHENTIK_ISSUER by hand. The CLI runs the device-code flow
    # directly against the returned issuer — the server is not in that path.
    settings = _settings(request)
    return JSONResponse({"issuer": settings.auth.oidc_issuer_url.rstrip("/")})


@router.get("/login")
async def login(request: Request) -> Response:
    settings = _settings(request)
    oauth = _oauth(request)
    redirect_uri = settings.auth.oidc_redirect_uri
    try:
        return await oauth.authentik.authorize_redirect(request, redirect_uri)  # type: ignore[no-any-return]
    except httpx.HTTPError:
        log.warning("auth.login.idp_unavailable")
        return JSONResponse(
            {"error": "idp_unavailable", "retry_after": 30},
            status_code=503,
            headers={"Retry-After": "30"},
        )


@router.get("/callback")
async def callback(
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    settings = _settings(request)
    oauth = _oauth(request)
    try:
        token = await oauth.authentik.authorize_access_token(request)
    except OAuthError as exc:
        log.warning("auth.login.failure", reason=str(exc))
        await write_audit(
            session,
            actor_sub="anonymous",
            action="auth.login.failure",
            payload={"reason": str(exc)[:200]},
        )
        await session.commit()
        raise HTTPException(401, "OIDC callback failed") from exc
    except Exception as exc:
        log.warning("auth.login.failure", reason=str(exc))
        await write_audit(
            session,
            actor_sub="anonymous",
            action="auth.login.failure",
            payload={"reason": str(exc)[:200]},
        )
        await session.commit()
        raise HTTPException(401, "OIDC callback failed") from exc

    userinfo = token.get("userinfo") or {}
    sub = userinfo.get("sub")
    if not sub:
        raise HTTPException(401, "missing sub claim")
    display_name = userinfo.get("preferred_username") or userinfo.get("name") or sub
    email = userinfo.get("email")

    await session.execute(
        text(
            "INSERT INTO users (sub, display_name, email, source, created_at) "
            "VALUES (:sub, :dn, :em, 'oidc', :now) "
            "ON CONFLICT (sub) DO UPDATE "
            "SET display_name = EXCLUDED.display_name, "
            "    email = EXCLUDED.email"
        ),
        {"sub": sub, "dn": display_name, "em": email, "now": datetime.now(UTC)},
    )

    await write_audit(
        session,
        actor_sub=sub,
        action="auth.login.success",
        payload={"source": "oidc"},
    )
    await session.commit()

    response = RedirectResponse("/", status_code=302)
    _set_auth_cookies(
        response,
        settings=settings,
        access_token=token["access_token"],
        refresh_token=token.get("refresh_token"),
        id_token=token.get("id_token"),
        expires_in=int(token.get("expires_in") or 3600),
        refresh_expires_in=(
            int(token["refresh_expires_in"])
            if token.get("refresh_expires_in") is not None
            else None
        ),
    )
    return response


@router.post("/refresh")
async def refresh(
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    settings = _settings(request)
    rt = request.cookies.get("ks_rt")
    if not rt:
        return JSONResponse({"error": "missing_refresh_token"}, status_code=401)
    oidc = _oidc(request)
    new_token = await oidc.refresh(rt)
    if new_token is None:
        resp = JSONResponse({"error": "refresh_failed"}, status_code=401)
        _clear_auth_cookies(resp, settings)
        return resp
    user_sub = (
        request.user.identity
        if hasattr(request, "user") and request.user.is_authenticated
        else "unknown"
    )
    await write_audit(
        session,
        actor_sub=user_sub,
        action="auth.token.refresh",
        payload={"source": "oidc"},
    )
    await session.commit()
    resp = JSONResponse({"ok": True})
    _set_auth_cookies(
        resp,
        settings=settings,
        access_token=new_token["access_token"],
        refresh_token=new_token.get("refresh_token"),
        id_token=new_token.get("id_token"),
        expires_in=int(new_token.get("expires_in") or 3600),
        refresh_expires_in=(
            int(new_token["refresh_expires_in"])
            if new_token.get("refresh_expires_in") is not None
            else None
        ),
    )
    return resp


@router.post("/logout")
async def logout(
    request: Request,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    settings = _settings(request)
    oidc = _oidc(request)
    id_token = request.cookies.get("ks_idt")
    target = await oidc.logout_url(id_token)
    user_sub = (
        request.user.identity
        if hasattr(request, "user") and request.user.is_authenticated
        else "anonymous"
    )
    if user_sub != "anonymous":
        await write_audit(session, actor_sub=user_sub, action="auth.logout")
        await session.commit()
    resp = RedirectResponse(target, status_code=302)
    _clear_auth_cookies(resp, settings)
    return resp
