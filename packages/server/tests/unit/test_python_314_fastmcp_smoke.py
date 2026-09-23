def test_fastmcp_imports() -> None:
    from fastmcp import FastMCP
    from fastmcp.server.dependencies import get_http_request
    from fastmcp.utilities.lifespan import combine_lifespans

    assert FastMCP is not None
    assert combine_lifespans is not None
    assert get_http_request is not None


def test_fastmcp_factory_creates_asgi_app() -> None:
    from fastmcp import FastMCP

    mcp = FastMCP("smoke")

    @mcp.tool
    async def dummy_tool(x: str) -> str:
        return x

    asgi_app = mcp.http_app(path="/")
    assert callable(asgi_app)


def test_pydantic_imports() -> None:
    from pydantic import BaseModel
    from pydantic_settings import BaseSettings

    class M(BaseModel):
        x: int

    m = M(x=1)
    assert m.x == 1
    assert BaseSettings is not None


def test_sqlalchemy_async_import() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    assert create_async_engine is not None


def test_ulid_import() -> None:
    from ulid import ULID

    u = ULID()
    assert len(str(u)) == 26


def test_structlog_import() -> None:
    import structlog

    logger = structlog.get_logger()
    assert logger is not None


def test_prometheus_import() -> None:
    from prometheus_fastapi_instrumentator import Instrumentator

    assert Instrumentator is not None
