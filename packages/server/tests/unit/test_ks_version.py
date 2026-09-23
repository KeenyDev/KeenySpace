"""KS_VERSION is written into backup manifests and gates restore.

It is spelled as semver while pyproject uses PEP 440 ("0.2.0-alpha.1" vs
"0.2.0a1"), so the two can drift silently: a stale KS_VERSION would accept
backups from a schema the running code no longer matches.
"""

from __future__ import annotations

from importlib.metadata import version

import semver
from keenyspace_server.api.admin import KS_VERSION


def test_ks_version_is_semver_parseable() -> None:
    # A restore parses it; an unparseable value would fail every restore with 422.
    parsed = semver.VersionInfo.parse(KS_VERSION)

    assert (parsed.major, parsed.minor) >= (0, 2)


def test_ks_version_tracks_the_distribution_version() -> None:
    distribution = version("keenyspace-server")
    major, minor, *_ = distribution.replace("a", ".").replace("rc", ".").split(".")
    parsed = semver.VersionInfo.parse(KS_VERSION)

    assert (parsed.major, parsed.minor) == (int(major), int(minor)), (
        f"KS_VERSION {KS_VERSION} and package version {distribution} disagree; "
        "bump both when releasing"
    )
