from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from keenyspace_server.compile.agent import compile_agent, run_compile_agent
from keenyspace_server.compile.models import CompileDeps, CompilePlan, PageOp
from keenyspace_server.compile.page_writer import apply_plan
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

DOMAIN_FIXTURES = Path(__file__).parent / "fixtures" / "compile" / "domain"

pytestmark = pytest.mark.eval


def _load_fixture(fixture_dir: Path) -> tuple[str, dict[str, Any], Path]:
    wal_text = (fixture_dir / "wal.md").read_text(encoding="utf-8")
    expect: dict[str, Any] = json.loads((fixture_dir / "expect.json").read_text(encoding="utf-8"))
    vault_path = fixture_dir / "vault"
    return wal_text, expect, vault_path


def _copy_vault(vault_path: Path, ws_root: Path) -> None:
    if vault_path.exists():
        for src in vault_path.rglob("*"):
            if src.is_file() and src.name != ".gitkeep":
                dest = ws_root / src.relative_to(vault_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)


def _extract_headings(text: str) -> list[tuple[int, str]]:
    headings: list[tuple[int, str]] = []
    for line in text.splitlines():
        m = re.match(r"^(#{1,6}) (.+)$", line)
        if m:
            headings.append((len(m.group(1)), m.group(2).strip()))
    return headings


def _extract_wikilinks(text: str) -> list[str]:
    return re.findall(r"\[\[([^\]]+)\]\]", text)


@pytest.mark.asyncio
async def test_domain_01_frontmatter_preservation(tmp_path: Path) -> None:
    fixture_dir = DOMAIN_FIXTURES / "01-frontmatter-preservation"
    wal_text, expect, vault_path = _load_fixture(fixture_dir)

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    _copy_vault(vault_path, ws_root)

    target_path = expect["expected_ops"][0]["path"]
    existing_page = ws_root / target_path
    existing_content = existing_page.read_text(encoding="utf-8")
    fm_end = existing_content.find("---", 3)
    fm_text = existing_content[3:fm_end].strip()
    existing_frontmatter: dict[str, Any] = yaml.safe_load(fm_text)
    existing_keys = set(existing_frontmatter.keys())

    preserved_fm = dict(existing_frontmatter)
    synth_plan = CompilePlan(
        ops=[
            PageOp(
                action="update",
                path=target_path,
                body=existing_content.split("---\n", 2)[-1].strip()
                + "\n\nInfrastructure migration completed on 2026-05-10.",
                frontmatter=preserved_fm,
            )
        ],
        notes="",
    )

    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=output_tool, args=synth_plan.model_dump())])

    deps = CompileDeps(ws_root=ws_root, wal_text=wal_text)
    with compile_agent.override(model=FunctionModel(_fake)):
        plan, _ = await run_compile_agent(deps)

    plan_fm_keys = set(plan.ops[0].frontmatter.keys())
    assert existing_keys <= plan_fm_keys, (
        f"Frontmatter keys lost: {existing_keys - plan_fm_keys}. "
        f"Expected all of {existing_keys!r} to survive in plan."
    )

    apply_plan(ws_root, plan)
    result_content = existing_page.read_text(encoding="utf-8")
    assert result_content.startswith("---"), "Written page must have frontmatter"
    result_fm_end = result_content.find("---", 3)
    result_fm: dict[str, Any] = yaml.safe_load(result_content[3:result_fm_end].strip())
    assert isinstance(result_fm, dict), "Result frontmatter must parse as dict"
    assert existing_keys <= set(result_fm.keys()), (
        f"Frontmatter keys lost on disk: {existing_keys - set(result_fm.keys())}"
    )


@pytest.mark.asyncio
async def test_domain_02_wikilink_hygiene(tmp_path: Path) -> None:
    fixture_dir = DOMAIN_FIXTURES / "02-wikilink-hygiene"
    wal_text, expect, vault_path = _load_fixture(fixture_dir)

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    _copy_vault(vault_path, ws_root)

    target_path = expect["expected_ops"][0]["path"]
    required_fragments: list[str] = expect.get("required_body_fragments", [])

    synth_body = (
        "# Index\n\n"
        "Main entry point.\n\n"
        "See [[auth]] for authentication documentation.\n"
    )
    synth_plan = CompilePlan(
        ops=[PageOp(action="update", path=target_path, body=synth_body, frontmatter={})],
        notes="",
    )

    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=output_tool, args=synth_plan.model_dump())])

    deps = CompileDeps(ws_root=ws_root, wal_text=wal_text)
    with compile_agent.override(model=FunctionModel(_fake)):
        plan, _ = await run_compile_agent(deps)

    op_body = plan.ops[0].body
    for fragment in required_fragments:
        assert fragment in op_body, f"Required fragment {fragment!r} not in op body"

    plan_paths_full = {op.path.removesuffix(".md") for op in plan.ops}
    plan_path_stems = {Path(p).name for p in plan_paths_full}
    vault_paths = {
        str(p.relative_to(ws_root)).removesuffix(".md")
        for p in ws_root.rglob("*.md")
    }
    vault_stems = {Path(vp).name for vp in vault_paths}

    for link in _extract_wikilinks(op_body):
        link_stem = Path(link).name
        resolvable = (
            link in vault_paths
            or link in plan_paths_full
            or link_stem in vault_stems
            or link_stem in plan_path_stems
        )
        assert resolvable, (
            f"Wikilink [[{link}]] is unresolvable: not in vault nor plan ops"
        )


@pytest.mark.asyncio
async def test_domain_03_heading_structure_preservation(tmp_path: Path) -> None:
    fixture_dir = DOMAIN_FIXTURES / "03-heading-structure"
    wal_text, expect, vault_path = _load_fixture(fixture_dir)

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    _copy_vault(vault_path, ws_root)

    target_path = expect["expected_ops"][0]["path"]
    required_fragments: list[str] = expect.get("required_body_fragments", [])

    prior_content = (ws_root / target_path).read_text(encoding="utf-8")
    prior_headings = set(_extract_headings(prior_content))

    new_body = (
        "# Architecture\n\n"
        "System architecture documentation.\n\n"
        "## Overview\n\n"
        "High-level overview of the system.\n\n"
        "## Components\n\n"
        "Key system components.\n\n"
        "### Database\n\n"
        "PostgreSQL 17 is used for persistent storage.\n\n"
        "### Cache\n\n"
        "Redis is used for caching sessions and rate limiting.\n\n"
        "### Redis (New)\n\n"
        "Redis also serves as the new cache component added in this WAL entry.\n"
    )
    synth_plan = CompilePlan(
        ops=[PageOp(action="update", path=target_path, body=new_body, frontmatter={})],
        notes="",
    )

    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=output_tool, args=synth_plan.model_dump())])

    deps = CompileDeps(ws_root=ws_root, wal_text=wal_text)
    with compile_agent.override(model=FunctionModel(_fake)):
        plan, _ = await run_compile_agent(deps)

    new_headings = set(_extract_headings(plan.ops[0].body))
    assert prior_headings <= new_headings, (
        f"Headings lost: {prior_headings - new_headings}. "
        f"All prior headings must survive at original levels."
    )

    for fragment in required_fragments:
        assert fragment in plan.ops[0].body, (
            f"Required heading fragment {fragment!r} not found in new body"
        )


@pytest.mark.asyncio
async def test_domain_04_page_targeting_deepest(tmp_path: Path) -> None:
    fixture_dir = DOMAIN_FIXTURES / "04-page-targeting"
    wal_text, expect, vault_path = _load_fixture(fixture_dir)

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    _copy_vault(vault_path, ws_root)

    expected_path = expect["expected_ops"][0]["path"]

    synth_plan = CompilePlan(
        ops=[
            PageOp(
                action="update",
                path=expected_path,
                body="# Google OAuth\n\nGoogle OAuth now requires PKCE for all new integrations as of 2026.\n",
                frontmatter={},
            )
        ],
        notes="",
    )

    async def _fake(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool = info.output_tools[0].name if info.output_tools else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=output_tool, args=synth_plan.model_dump())])

    deps = CompileDeps(ws_root=ws_root, wal_text=wal_text)
    with compile_agent.override(model=FunctionModel(_fake)):
        plan, _ = await run_compile_agent(deps)

    assert plan.ops[0].path == expected_path, (
        f"Agent must target the deepest specific match ({expected_path!r}), "
        f"not a shallower path like 'notes/auth.md'. Got: {plan.ops[0].path!r}"
    )
