from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastmcp.utilities.lifespan import combine_lifespans
from starlette.middleware.authentication import AuthenticationMiddleware

from .api import compile as compile_router
from .api import health, logs, pages, workspaces
from .auth.api_keys import ApiKeyService
from .auth.dev_shim import DevTokenAuthBackend
from .auth.middleware import on_auth_error
from .config import get_settings
from .db.session import engine_lifespan, get_db_session
from .fs.bootstrap import ensure_fs_root_layout
from .mcp.server import build_mcp, build_mcp_skeleton
from .observability.logging import configure_logging
from .observability.metrics import build_instrumentator
from .routers import api_keys as api_keys_router
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
                # APScheduler 3.x shutdown() is sync; D-11 prose "await" wording
                # is descriptive intent, not a literal API call — see RESEARCH §2.
                scheduler.shutdown(wait=True)
                set_coordinator(None)
                app.state.compile_coordinator = None
                app.state.scheduler = None

    app = FastAPI(
        title="KeenySpace",
        lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan),
    )

    app.state.settings = settings
    app.state.wal_locks = WorkspaceLockRegistry()
    app.state.api_key_service = ApiKeyService(
        pepper=settings.auth.api_key_pepper,
        db_factory=get_db_session,
        debounce_seconds=settings.auth.api_key_last_used_debounce_seconds,
    )

    # Wave 1 transitional middleware: dev_shim backend без dev_token (всегда отвергает).
    # Wave 2 (task 03-03-04) заменяет на CompositeAuthBackend с cookie/api_key/oidc paths.
    app.add_middleware(
        AuthenticationMiddleware,
        backend=DevTokenAuthBackend(dev_token=None),
        on_error=on_auth_error,
    )

    app.include_router(health.router)
    app.include_router(workspaces.router, prefix="/v1/api/workspaces")
    app.include_router(pages.router, prefix="/v1/api/workspaces")
    app.include_router(logs.router, prefix="/v1/api/workspaces")
    app.include_router(compile_router.router, prefix="/v1/api/workspaces")
    app.include_router(api_keys_router.router, prefix="/v1/api/auth/api-keys")

    app.mount("/v1/mcp", mcp_app)

    build_instrumentator().instrument(app).expose(app)

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
