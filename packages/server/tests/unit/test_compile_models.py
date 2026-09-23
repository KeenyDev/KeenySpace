from __future__ import annotations

import pytest
from keenyspace_server.compile.models import (
    CompilePlan,
    CompileStatusResponse,
    CompileTriggerResponse,
    PageOp,
)
from pydantic import ValidationError


def test_pageop_happy_path() -> None:
    op = PageOp(action="create", path="notes/x.md", body="hello")
    assert op.model_dump()["path"] == "notes/x.md"


@pytest.mark.parametrize("bad_path", ["../etc/passwd", "/abs.md", "notes/../escape.md"])
def test_pageop_rejects_traversal(bad_path: str) -> None:
    with pytest.raises(ValidationError, match="non-traversing"):
        PageOp(action="create", path=bad_path, body="x")


def test_pageop_requires_md_extension() -> None:
    with pytest.raises(ValidationError, match=r"\.md"):
        PageOp(action="create", path="notes/no_extension", body="x")


def test_pageop_action_literal() -> None:
    with pytest.raises(ValidationError):
        PageOp(action="archive", path="x.md", body="y")  # type: ignore[arg-type]


def test_pageop_body_min_length() -> None:
    with pytest.raises(ValidationError):
        PageOp(action="create", path="x.md", body="")


def test_compileplan_rejects_duplicate_paths() -> None:
    with pytest.raises(ValidationError, match="Duplicate path"):
        CompilePlan(ops=[
            PageOp(action="create", path="x.md", body="a"),
            PageOp(action="update", path="x.md", body="b"),
        ])


def test_compileplan_empty_is_valid() -> None:
    plan = CompilePlan(ops=[])
    assert plan.ops == []
    assert plan.notes == ""


def test_compile_trigger_response_status_literal() -> None:
    CompileTriggerResponse(job_id="abc", status="queued")
    with pytest.raises(ValidationError):
        CompileTriggerResponse(job_id="abc", status="weird")  # type: ignore[arg-type]


def test_compile_status_response_defaults() -> None:
    r = CompileStatusResponse(state="idle")
    assert r.last_wal_id is None
    assert r.paused_reason is None
