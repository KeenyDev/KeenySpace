from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI
from fastmcp.utilities.lifespan import combine_lifespans
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .api import (
    admin,
    health,
    logs,
    pages,
    well_known,
    workspace_archive,
    workspace_export,
    workspace_import,
    workspace_list,
    workspace_manifest,
    workspaces,
)
from .api import api_keys as api_keys_router
from .api import auth as auth_router
from .api import compile as compile_router
from .auth.api_keys import ApiKeyService
from .auth.composite import CompositeAuthBackend
from .auth.group_snapshot import GroupSnapshotStore
from .auth.middleware import on_auth_error
from .auth.oidc import OidcClient, build_oauth
from .auth.refresh_dep import refresh_if_needed
from .config import get_settings
from .db.session import engine_lifespan, get_db_session
from .fs.bootstrap import ensure_fs_root_layout
from .mcp.server import build_mcp, build_mcp_skeleton
from .observability.logging import configure_logging
from .observability.metrics import build_instrumentator
from .observability.metrics_server import metrics_server_lifespan
from .wal.locks import WorkspaceLockRegistry


def build_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.server.log_level)

    mcp = build_mcp()
    mcp_app = mcp.http_app(path="/")

    @asynccontextmanager
    async def app_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with engine_lifespan(app):
            if settings.fs.blueprints_dir is not None:
                server_blueprints_dir = settings.fs.blueprints_dir
            else:
                server_blueprints_dir = Path(__file__).parent.parent.parent.parent / "blueprints"
            ensure_fs_root_layout(settings.fs.root, server_blueprints_dir)
            from .compile.coordinator import CompileCoordinator, set_coordinator
            from .compile.scheduler import build_scheduler

            coordinator = CompileCoordinator(settings=settings.compile)
            await coordinator.reconcile_interrupted()
            app.state.compile_coordinator = coordinator
            set_coordinator(coordinator)

            scheduler = build_scheduler()
            app.state.scheduler = scheduler
            scheduler.add_job(
                coordinator.backstop_all_workspaces,
                "interval",
                minutes=settings.compile.backstop_interval_minutes,
                id="compile_backstop",
                replace_existing=True,
            )
            scheduler.add_job(
                coordinator.reset_daily_ceiling,
                "cron",
                hour=0,
                minute=0,
                timezone="UTC",
                id="compile_daily_reset",
                replace_existing=True,
            )
            scheduler.start()

            try:
                yield
            finally:
                # APScheduler 3.x shutdown() is synchronous: nothing to await.
                scheduler.shutdown(wait=True)
                await coordinator.aclose()
                set_coordinator(None)
                app.state.compile_coordinator = None
                app.state.scheduler = None

    app = FastAPI(
        title="KeenySpace",
        lifespan=combine_lifespans(metrics_server_lifespan, app_lifespan, mcp_app.lifespan),
    )

    app.state.settings = settings
    app.state.wal_locks = WorkspaceLockRegistry()
    # FastMCP get_http_request() may return a synthetic request whose .app points
    # at the mounted mcp_app rather than the outer FastAPI app. MCP tools access
    # app.state.settings / wal_locks — mirror them onto mcp_app.state so the
    # mount-level request resolves the same attributes (out-of-scope baseline gap).
    mcp_app.state.settings = settings
    mcp_app.state.wal_locks = app.state.wal_locks
    snapshot_max_age_days = settings.auth.api_key_group_snapshot_max_age_days
    api_key_service = ApiKeyService(
        pepper=settings.auth.api_key_pepper.get_secret_value(),
        db_factory=get_db_session,
        debounce_seconds=settings.auth.api_key_last_used_debounce_seconds,
        group_snapshot_max_age=(
            timedelta(days=snapshot_max_age_days) if snapshot_max_age_days is not None else None
        ),
    )
    app.state.api_key_service = api_key_service
    group_snapshots = GroupSnapshotStore(
        db_factory=get_db_session,
        debounce_seconds=settings.auth.api_key_last_used_debounce_seconds,
    )
    app.state.group_snapshots = group_snapshots

    oauth = build_oauth(settings)
    app.state.oauth = oauth
    oidc_client = OidcClient(oauth, settings.auth)
    app.state.oidc_client = oidc_client

    composite_backend = CompositeAuthBackend(
        oidc_client=oidc_client,
        api_key_service=api_key_service,
        required_group=settings.auth.required_group,
        group_snapshots=group_snapshots,
    )
    # Middleware order — Starlette wraps in reverse-add order; LAST add_middleware
    # = OUTERMOST = runs first on inbound request. SessionMiddleware must run
    # BEFORE AuthenticationMiddleware so request.session is populated before
    # CompositeAuthBackend.authenticate (which delegates to OidcClient using
    # request session under /v1/api/auth). Path-scoped to /v1/api/auth so the
    # cookie does not leak onto /v1/mcp or other surfaces.
    app.add_middleware(
        AuthenticationMiddleware,
        backend=composite_backend,
        on_error=on_auth_error,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.auth.session_secret_key.get_secret_value(),
        session_cookie="ks_oidc_session",
        max_age=900,
        same_site="lax",
        https_only=settings.auth.cookie_secure,
        path="/v1/api/auth",
    )

    app.include_router(health.router)
    app.include_router(well_known.router)
    # Cookie-authenticated browser sessions get their access token rotated
    # inline when ks_at expires within refresh_threshold_seconds. NOT applied
    # to the auth router (login/callback are public; refresh/logout manage
    # their own cookies) nor to the MCP mount, where agents use API keys.
    protected_deps = [Depends(refresh_if_needed)]
    app.include_router(
        workspaces.router,
        prefix="/v1/api/workspaces",
        dependencies=protected_deps,
    )
    app.include_router(
        pages.router,
        prefix="/v1/api/workspaces",
        dependencies=protected_deps,
    )
    app.include_router(
        logs.router,
        prefix="/v1/api/workspaces",
        dependencies=protected_deps,
    )
    app.include_router(
        compile_router.router,
        prefix="/v1/api/workspaces",
        dependencies=protected_deps,
    )
    app.include_router(
        workspace_list.router,
        prefix="/v1/api/workspaces",
        dependencies=protected_deps,
    )
    app.include_router(
        workspace_archive.router,
        prefix="/v1/api/workspaces",
        dependencies=protected_deps,
    )
    app.include_router(
        workspace_export.router,
        prefix="/v1/api/workspaces",
        dependencies=protected_deps,
    )
    app.include_router(
        workspace_import.router,
        prefix="/v1/api/workspaces",
        dependencies=protected_deps,
    )
    app.include_router(
        workspace_manifest.router,
        prefix="/v1/api/workspaces",
        dependencies=protected_deps,
    )
    app.include_router(
        api_keys_router.router,
        prefix="/v1/api/auth/api-keys",
        dependencies=protected_deps,
    )
    # Admin routes (backup / restore) wipe and replace the entire
    # deployment. They are mounted only behind an explicit server-side opt-in
    # env flag, and every route additionally requires membership in
    # auth.admin_group (auth/admin_gate.py).
    if os.environ.get("KEENYSPACE_ADMIN_API_ENABLED") == "1":
        app.include_router(
            admin.router,
            prefix="/v1/admin",
            dependencies=protected_deps,
        )
    app.include_router(auth_router.router, prefix="/v1/api/auth")

    app.mount("/v1/mcp", mcp_app)

    build_instrumentator().instrument(app)

    return app


def build_app_skeleton() -> FastAPI:
    mcp = build_mcp_skeleton()
    mcp_app = mcp.http_app(path="/")

    @asynccontextmanager
    async def app_lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(
        title="KeenySpace-skeleton",
        lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan),
    )

    app.include_router(health.router)
    app.mount("/v1/mcp", mcp_app)

    return app


def cli_main() -> None:
    settings = get_settings()
    uvicorn.run(
        "keenyspace_server.main:app",
        host=settings.server.host,
        port=settings.server.port,
        workers=1,
        log_config=None,
    )


app = build_app() if os.environ.get("KEENYSPACE_DB__URL") else build_app_skeleton()
