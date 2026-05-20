"""D-03 inline auto-refresh: FastAPI dependency.

Wire-up в main.py::build_app через router-level `dependencies=[Depends(refresh_if_needed)]`.
CompositeAuthBackend._try_cookie -> OidcClient.validate_access_token ставит
`request.state.ks_at_expiring_soon = True` если exp - now < refresh_threshold_seconds.
Эта dependency читает флаг + ks_rt cookie, делает refresh через OidcClient,
и rotate'ает cookies в текущий response.
"""

from __future__ import annotations

import structlog
from fastapi import Request, Response

log = structlog.get_logger(__name__)


async def refresh_if_needed(request: Request, response: Response) -> None:
    if not getattr(request.state, "ks_at_expiring_soon", False):
        return
    rt = request.cookies.get("ks_rt")
    if not rt:
        return
    settings = request.app.state.settings
    oidc = request.app.state.oidc_client
    new_token = await oidc.refresh(rt)
    if new_token is None:
        log.info("auth.token.refresh.silent_failed")
        return
    secure = settings.auth.cookie_secure
    response.set_cookie(
        "ks_at",
        new_token["access_token"],
        httponly=True,
        secure=secure,
        samesite="lax",
        path=settings.auth.cookie_path_ks_at,
        max_age=int(new_token.get("expires_in") or 3600),
    )
    if new_token.get("refresh_token"):
        rt_max_age_raw = new_token.get("refresh_expires_in")
        if rt_max_age_raw is None:
            log.warning("auth.token.refresh.no_refresh_expires_in_falling_back_14d")
            rt_max_age = 86400 * 14
        else:
            rt_max_age = int(rt_max_age_raw)
        response.set_cookie(
            "ks_rt",
            new_token["refresh_token"],
            httponly=True,
            secure=secure,
            samesite="strict",
            path=settings.auth.cookie_path_ks_rt,
            max_age=rt_max_age,
        )
    if new_token.get("id_token"):
        response.set_cookie(
            "ks_idt",
            new_token["id_token"],
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/v1/api/auth/logout",
            max_age=86400 * 14,
        )
    log.info("auth.token.refresh.silent_ok")
