from __future__ import annotations

from keenyspace_server.compile.hashing import hash_plan
from keenyspace_server.compile.models import CompilePlan, PageOp


def _plan_with_frontmatter(fm: dict[str, object]) -> CompilePlan:
    return CompilePlan(ops=[PageOp(action="update", path="notes/x.md", body="b", frontmatter=fm)])


def test_hash_is_64_char_hex() -> None:
    h = hash_plan("01HX0000000000000000000001", "01HX0000000000000000000002", CompilePlan(ops=[]))
    assert len(h) == 64
    int(h, 16)


def test_hash_stable_across_frontmatter_key_order() -> None:
    fm1 = {"title": "X", "status": "wip", "owner": "alice"}
    fm2 = {"status": "wip", "owner": "alice", "title": "X"}
    h1 = hash_plan("A", "B", _plan_with_frontmatter(fm1))
    h2 = hash_plan("A", "B", _plan_with_frontmatter(fm2))
    assert h1 == h2


def test_hash_changes_with_body() -> None:
    plan_a = CompilePlan(ops=[PageOp(action="create", path="x.md", body="aaa")])
    plan_b = CompilePlan(ops=[PageOp(action="create", path="x.md", body="bbb")])
    assert hash_plan("A", "B", plan_a) != hash_plan("A", "B", plan_b)


def test_hash_changes_with_path() -> None:
    plan_a = CompilePlan(ops=[PageOp(action="create", path="x.md", body="b")])
    plan_b = CompilePlan(ops=[PageOp(action="create", path="y.md", body="b")])
    assert hash_plan("A", "B", plan_a) != hash_plan("A", "B", plan_b)


def test_hash_changes_with_frontmatter_value() -> None:
    h1 = hash_plan("A", "B", _plan_with_frontmatter({"status": "wip"}))
    h2 = hash_plan("A", "B", _plan_with_frontmatter({"status": "done"}))
    assert h1 != h2


def test_hash_changes_with_wal_ids() -> None:
    plan = CompilePlan(ops=[PageOp(action="create", path="x.md", body="b")])
    assert hash_plan("A1", "B", plan) != hash_plan("A2", "B", plan)
    assert hash_plan("A", "B1", plan) != hash_plan("A", "B2", plan)
