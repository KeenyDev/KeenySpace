"""The tool descriptions MCP clients actually receive.

FastMCP derives a tool's description from its function docstring and cuts it at
the first `Args:` section, routing the per-argument text into the input schema.
A docstring that puts its contract prose below `Args:` therefore ships a
truncated description to every client without any visible error.
"""

from __future__ import annotations

import re

import pytest
from fastmcp.tools import Tool

from keenyspace_server.mcp.server import _TIER1_TOOLS

_PLANNING_ID = re.compile(r"\b[A-Z]{1,6}-[0-9]+\b")
_TOOL_CASES = [pytest.param(fn, name, id=name) for fn, name in _TIER1_TOOLS]


@pytest.mark.parametrize(("fn", "wire_name"), _TOOL_CASES)
def test_tool_ships_a_usable_description(fn: object, wire_name: str) -> None:
    description = Tool.from_function(fn, name=wire_name).description  # type: ignore[arg-type]

    assert description, f"{wire_name} would reach clients with no description"
    assert len(description) > 60, (
        f"{wire_name} description is {len(description)} chars: "
        "contract prose below an Args: section is dropped by FastMCP"
    )


@pytest.mark.parametrize(("fn", "wire_name"), _TOOL_CASES)
def test_tool_description_carries_no_internal_ids(fn: object, wire_name: str) -> None:
    description = Tool.from_function(fn, name=wire_name).description or ""

    leaked = _PLANNING_ID.findall(description)
    assert not leaked, f"{wire_name} leaks internal planning ids to clients: {leaked}"


def test_append_log_states_that_pages_need_a_compile() -> None:
    append_log = next(fn for fn, name in _TIER1_TOOLS if name == "append_log")

    description = Tool.from_function(append_log, name="append_log").description or ""

    assert "compile" in description.lower()
