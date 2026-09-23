"""RED tests for api/workspace_archive.py HTTP endpoints."""
from __future__ import annotations


def test_archive_router_importable() -> None:
    from keenyspace_server.api.workspace_archive import router

    assert router is not None


def test_archive_router_has_two_routes() -> None:
    from keenyspace_server.api.workspace_archive import router

    assert len(router.routes) == 2


def test_archive_router_has_archive_route() -> None:
    from keenyspace_server.api.workspace_archive import router

    paths = [r.path for r in router.routes]  # type: ignore[attr-defined]
    assert any("archive" in p and "unarchive" not in p for p in paths)


def test_archive_router_has_unarchive_route() -> None:
    from keenyspace_server.api.workspace_archive import router

    paths = [r.path for r in router.routes]  # type: ignore[attr-defined]
    assert any("unarchive" in p for p in paths)


def test_archive_response_model_importable() -> None:
    from keenyspace_server.api.workspace_archive import ArchiveResponse

    obj = ArchiveResponse(slug="test", status="archived", archived_at="2026-01-01T00:00:00+00:00")
    assert obj.slug == "test"
    assert obj.status == "archived"


def test_archive_response_model_archived_at_optional() -> None:
    from keenyspace_server.api.workspace_archive import ArchiveResponse

    obj = ArchiveResponse(slug="test", status="active")
    assert obj.archived_at is None
