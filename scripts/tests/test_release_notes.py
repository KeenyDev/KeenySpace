"""Tests for the release-notes assembler."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release_notes import Entry, collect, parse_commit, render


def test_github_merge_commit_takes_its_section_from_the_branch() -> None:
    entry = parse_commit(
        "Merge pull request #12 from KeenyDev/fix/wal-ids\n\nfix(wal): monotonic entry ids"
    )

    assert entry == Entry(section="fix", title="monotonic entry ids", pr="12")


def test_merge_commit_without_a_body_falls_back_to_the_branch_name() -> None:
    entry = parse_commit("Merge pull request #7 from KeenyDev/docs/install-guide")

    assert entry == Entry(section="docs", title="install guide", pr="7")


def test_plain_merge_branch_commit_is_recognised() -> None:
    entry = parse_commit("Merge branch 'deploy/pin-images'\n\ndeploy: pin every image")

    assert entry == Entry(section="deploy", title="pin every image", pr=None)


def test_direct_commit_is_grouped_by_its_conventional_type() -> None:
    entry = parse_commit("perf(server): keyset search\n\nbody text")

    assert entry == Entry(section="perf", title="keyset search", pr=None)


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("build: bump uv", "deploy"),
        ("doc: fix a typo", "docs"),
        ("wip something", "Other changes"),
        ("nonsense(scope) no colon", "Other changes"),
    ],
)
def test_sections_are_normalised(subject: str, expected: str) -> None:
    assert parse_commit(subject).section == expected


def test_breaking_change_marker_does_not_hide_the_section() -> None:
    assert parse_commit("feat(auth)!: require an admin group").section == "feat"


def test_render_orders_sections_and_links_pull_requests() -> None:
    body = render(
        [
            Entry(section="docs", title="write an upgrade guide", pr=None),
            Entry(section="fix", title="stop losing entries", pr="12"),
        ],
        version="v0.2.0",
        compare_url="https://example.invalid/compare/v0.1.0...v0.2.0",
    )

    assert body.index("## Fixes") < body.index("## Documentation")
    assert "- stop losing entries (#12)" in body
    assert body.startswith("# v0.2.0")
    assert "https://example.invalid/compare/v0.1.0...v0.2.0" in body


def test_render_says_so_when_nothing_landed() -> None:
    assert "No changes recorded" in render([], version="v0.2.0", compare_url=None)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository tagged v0.1.0, ready for branches to be merged into main."""

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / "f").write_text("one")
    git("add", "f")
    git("commit", "-qm", "chore: first")
    git("tag", "v0.1.0")
    return tmp_path


def merge_branch(repo: Path, branch: str, commits: list[str], merge_subject: str) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("checkout", "-qb", branch)
    for index, message in enumerate(commits):
        (repo / branch.replace("/", "-")).write_text(f"{index}")
        git("add", "-A")
        git("commit", "-qm", message)
    git("checkout", "-q", "main")
    git("merge", "--no-ff", "-m", merge_subject, branch)


def test_collect_expands_a_merged_branch_into_its_own_commits(repo: Path) -> None:
    merge_branch(
        repo,
        "fix/thing",
        ["fix: the thing", "test: cover the thing"],
        "Merge pull request #3 from org/fix/thing\n\nfix: the thing",
    )

    entries = collect("v0.1.0", "HEAD", repo)

    # One branch, two changes: each keeps its own type and carries the PR number.
    assert set(entries) == {
        Entry(section="fix", title="the thing", pr="3"),
        Entry(section="test", title="cover the thing", pr="3"),
    }


def test_collect_falls_back_to_the_merge_title_for_unlabelled_branches(repo: Path) -> None:
    merge_branch(
        repo,
        "docs/guide",
        ["wip", "more wip"],
        "Merge pull request #4 from org/docs/guide\n\ndocs: write the guide",
    )

    entries = collect("v0.1.0", "HEAD", repo)

    assert entries == [Entry(section="docs", title="write the guide", pr="4")]


def test_collect_keeps_direct_commits_on_the_release_branch(repo: Path) -> None:
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "fix: straight onto main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    entries = collect("v0.1.0", "HEAD", repo)

    assert entries == [Entry(section="fix", title="straight onto main", pr=None)]
