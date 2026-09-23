"""Assemble release notes from the branches merged since the previous tag.

Walks first-parent history, so each entry is one landed branch: a GitHub merge
commit ("Merge pull request #12 from KeenyDev/fix/wal-ids") or, when a branch was
merged fast-forward, the conventional-commit subject itself. Entries are grouped
by the branch prefix, falling back to the commit type when there is no branch.

    uv run python scripts/release_notes.py --to v0.2.0-alpha.1

With --intro, a hand-written file is placed above the generated sections.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SECTIONS: list[tuple[str, str]] = [
    ("feat", "Features"),
    ("fix", "Fixes"),
    ("perf", "Performance"),
    ("refactor", "Refactoring"),
    ("deploy", "Deployment"),
    ("ci", "CI"),
    ("docs", "Documentation"),
    ("test", "Tests"),
    ("style", "Style"),
    ("chore", "Chores"),
]
_OTHER = "Other changes"

# "Merge pull request #12 from KeenyDev/fix/wal-ids"; the PR title follows in the body.
_MERGE_PR = re.compile(r"^Merge pull request #(?P<number>\d+) from [^/]+/(?P<branch>\S+)")
# "Merge branch 'fix/wal-ids'" / "Merge remote-tracking branch 'origin/fix/wal-ids'"
_MERGE_BRANCH = re.compile(r"^Merge (?P<remote>remote-tracking )?branch '(?P<branch>[^']+)'")
_CONVENTIONAL = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?!?: (?P<subject>.+)$")

_ALIASES = {"build": "deploy", "infra": "deploy", "tests": "test", "doc": "docs"}


@dataclass(frozen=True)
class Entry:
    """One landed change: its section key, one-line title and PR number if any."""

    section: str
    title: str
    pr: str | None


def run_git(args: list[str], repo: Path) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def previous_tag(rev: str, repo: Path) -> str:
    """The most recent tag reachable from rev, excluding rev's own tag."""
    return run_git(["describe", "--tags", "--abbrev=0", f"{rev}^"], repo)


def normalize_section(raw: str) -> str:
    key = _ALIASES.get(raw.lower(), raw.lower())
    return key if any(key == section for section, _ in SECTIONS) else _OTHER


def parse_commit(message: str) -> Entry:
    """Derive one entry from a commit message (subject plus body)."""
    lines = [line.strip() for line in message.splitlines()]
    subject = lines[0] if lines else ""
    body = [line for line in lines[1:] if line]

    merge_pr = _MERGE_PR.match(subject)
    merge_branch = None if merge_pr else _MERGE_BRANCH.match(subject)
    if merge_pr or merge_branch:
        match = merge_pr or merge_branch
        assert match is not None  # one of the two matched to get here
        branch = match.group("branch")
        if merge_branch is not None and merge_branch.group("remote"):
            # A remote-tracking merge carries the remote name: origin/fix/x.
            _, _, branch = branch.partition("/")
        prefix, _, rest = branch.partition("/")
        # GitHub puts the pull request title in the merge commit body.
        title = body[0] if body else rest.replace("-", " ") or branch
        conventional = _CONVENTIONAL.match(title)
        if conventional:
            title = conventional.group("subject")
        return Entry(
            section=normalize_section(prefix),
            title=title,
            pr=merge_pr.group("number") if merge_pr else None,
        )

    conventional = _CONVENTIONAL.match(subject)
    if conventional:
        return Entry(
            section=normalize_section(conventional.group("type")),
            title=conventional.group("subject"),
            pr=None,
        )
    return Entry(section=_OTHER, title=subject, pr=None)


def _commit_messages(rev_range: list[str], repo: Path) -> list[str]:
    log = run_git(["log", "--format=%B%x1e", *rev_range], repo)
    return [record.strip() for record in log.split("\x1e") if record.strip()]


def branch_entries(sha: str, parents: list[str], pr: str | None, repo: Path) -> list[Entry]:
    """The branch's own commits, so one merged branch yields one entry per change."""
    if len(parents) != 2:
        return []
    messages = _commit_messages([f"{parents[0]}..{parents[1]}"], repo)
    entries = [parse_commit(message) for message in messages]
    named = [
        Entry(section=entry.section, title=entry.title, pr=pr)
        for entry in entries
        if entry.title and entry.section != _OTHER
    ]
    # A branch of unlabelled commits says more through its merge title.
    return named if named else []


def collect(from_rev: str, to_rev: str, repo: Path) -> list[Entry]:
    """Entries for every change that landed in from_rev..to_rev, newest first."""
    log = run_git(
        ["log", "--first-parent", "--format=%H%x1f%P%x1f%B%x1e", f"{from_rev}..{to_rev}"], repo
    )
    entries: list[Entry] = []
    for record in log.split("\x1e"):
        if not record.strip():
            continue
        sha, _, rest = record.strip().partition("\x1f")
        parent_field, _, message = rest.partition("\x1f")
        merged = parse_commit(message)
        expanded = branch_entries(sha, parent_field.split(), merged.pr, repo)
        if expanded:
            entries.extend(expanded)
        elif merged.title:
            entries.append(merged)
    return entries


def render(entries: list[Entry], version: str, compare_url: str | None) -> str:
    grouped: dict[str, list[Entry]] = {}
    for entry in entries:
        grouped.setdefault(entry.section, []).append(entry)

    out: list[str] = [f"# {version}", ""]
    ordered = [*(key for key, _ in SECTIONS), _OTHER]
    headings = dict(SECTIONS) | {_OTHER: _OTHER}
    for key in ordered:
        section_entries = grouped.get(key)
        if not section_entries:
            continue
        out += [f"## {headings[key]}", ""]
        for entry in section_entries:
            suffix = f" (#{entry.pr})" if entry.pr else ""
            out.append(f"- {entry.title}{suffix}")
        out.append("")

    if not entries:
        out += ["No changes recorded since the previous tag.", ""]
    if compare_url:
        out += [f"**Full changelog:** {compare_url}", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", default="HEAD", help="release revision or tag (default HEAD)")
    parser.add_argument(
        "--from", dest="from_rev", help="previous tag (default: the one before --to)"
    )
    parser.add_argument("--version", help="heading to use (default: the --to tag name)")
    parser.add_argument("--intro", type=Path, help="markdown file to place above the sections")
    parser.add_argument("--repo-url", help="repository URL, for a compare link")
    parser.add_argument("--output", type=Path, help="write here instead of stdout")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="repository checkout")
    args = parser.parse_args(argv)

    from_rev = args.from_rev or previous_tag(args.to, args.repo)
    version = args.version or (args.to if args.to != "HEAD" else "Unreleased")
    compare = f"{args.repo_url}/compare/{from_rev}...{args.to}" if args.repo_url else None

    body = render(collect(from_rev, args.to, args.repo), version, compare)
    if args.intro and args.intro.is_file():
        body = f"{args.intro.read_text().strip()}\n\n{body}"

    if args.output:
        args.output.write_text(body)
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
