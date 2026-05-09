from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine | None:
    return _engine


@asynccontextmanager
async def engine_lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _engine, _session_factory
    settings = app.state.settings
    _engine = create_async_engine(
        str(settings.db.url),
        pool_size=settings.db.pool_size,
        pool_pre_ping=settings.db.pool_pre_ping,
        echo=False,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    try:
        yield
    finally:
        await _engine.dispose()
        _engine = None
        _session_factory = None


async def get_db() -> AsyncIterator[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("engine_lifespan didn't run; combine_lifespans missing?")
    async with _session_factory() as session:
        yield session
