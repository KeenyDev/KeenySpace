from __future__ import annotations

from pathlib import Path

import pytest
from keenyspace_server.compile.models import CompilePlan, PageOp
from keenyspace_server.compile.page_writer import (
    CompilePlanSafetyError,
    _serialize_page,
    apply_plan,
)


def test_apply_plan_writes_valid_pageop(tmp_path: Path) -> None:
    plan = CompilePlan(
        ops=[
            PageOp(action="create", path="notes/test.md", body="hello", frontmatter={"title": "T"}),
        ]
    )
    n = apply_plan(tmp_path, plan)
    assert n == 1
    page = tmp_path / "notes" / "test.md"
    assert page.is_file()
    text = page.read_text(encoding="utf-8")
    assert "title: T" in text
    assert "hello" in text


def test_apply_plan_denylist_zero_files_written_on_first_violation(tmp_path: Path) -> None:
    plan = CompilePlan(
        ops=[
            PageOp(action="create", path="notes/ok.md", body="ok", frontmatter={}),
            PageOp(action="create", path="logs/forbidden.md", body="x", frontmatter={}),
        ]
    )
    with pytest.raises(CompilePlanSafetyError):
        apply_plan(tmp_path, plan)
    assert not (tmp_path / "notes" / "ok.md").exists()
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files == []


def test_apply_plan_denylist_first_op_violation(tmp_path: Path) -> None:
    plan = CompilePlan(
        ops=[
            PageOp(action="create", path="CLAUDE.md", body="x", frontmatter={}),
        ]
    )
    with pytest.raises(CompilePlanSafetyError) as exc_info:
        apply_plan(tmp_path, plan)
    assert exc_info.value.path == "CLAUDE.md"


def test_serialize_page_preserves_dict_insertion_order(tmp_path: Path) -> None:
    fm: dict[str, object] = {}
    fm["zeta"] = 1
    fm["alpha"] = 2
    fm["middle"] = 3
    out = _serialize_page(fm, "body").decode("utf-8")
    i_zeta = out.index("zeta:")
    i_alpha = out.index("alpha:")
    i_middle = out.index("middle:")
    assert i_zeta < i_alpha < i_middle, f"sort_keys=False violated; got:\n{out}"


def test_serialize_page_round_trip_no_frontmatter(tmp_path: Path) -> None:
    out = _serialize_page({}, "body-only").decode("utf-8")
    assert out == "body-only"


def test_apply_plan_writes_to_canonical_md_path(tmp_path: Path) -> None:
    unvalidated = PageOp.model_construct(
        action="create", path="notes/raw-name", body="b", frontmatter={}
    )
    apply_plan(tmp_path, CompilePlan.model_construct(ops=[unvalidated], notes=""))
    assert (tmp_path / "notes" / "raw-name.md").is_file()
    assert not (tmp_path / "notes" / "raw-name").exists()
