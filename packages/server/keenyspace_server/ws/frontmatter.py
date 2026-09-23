"""YAML frontmatter splitting for markdown pages."""

from __future__ import annotations

from typing import Any

import yaml


def split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Split ``content`` into its YAML frontmatter mapping and the body below it.

    Returns ``({}, content)`` unchanged when there is no leading ``---`` block,
    when the block is unterminated, when the YAML fails to parse, or when it
    parses to something other than a mapping.
    """
    if not content.startswith("---\n"):
        return {}, content

    end = content.find("\n---\n", 4)
    if end == -1:
        return {}, content

    yaml_text = content[4:end]
    body = content[end + 5 :]

    try:
        fm = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return {}, content
    if not isinstance(fm, dict):
        return {}, content
    return fm, body
