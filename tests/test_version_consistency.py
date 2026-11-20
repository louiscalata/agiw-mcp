# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Active documented versions agree with the package version.

README.md, roadmap.md, CHANGELOG.md and the current release notes state the
version that current main reports. Tag numbers in release history, the vendored
Nisi version and MCP protocol versions are not the package version.
"""

from __future__ import annotations

import re
import unittest

from support import ROOT

from agiw_mcp import __version__

SEMVER = r"(?P<version>\d+\.\d+\.\d+)"

# Only passages that present a version as the one current main reports. A tag
# reference (`v0.1.0`), a changelog heading (`## [0.1.0]`) and a dependency
# version (`0.2.0` Nisi, `2025-11-25` protocol) deliberately do not match.
ACTIVE_VERSION_PATTERNS = (
    re.compile(rf"\breport(?:s|ed)?(?: version)? `?v?{SEMVER}`?"),
    re.compile(rf"\bprints? `agiw-mcp v?{SEMVER}`"),
    re.compile(rf"\bserver `agiw-mcp` `?v?{SEMVER}`?"),
    re.compile(rf"\bstay(?:s|ed)?(?: on)? version `?v?{SEMVER}`?"),
    re.compile(rf"\bversion stays? `?v?{SEMVER}`?"),
    re.compile(rf"\bversion remains? `?v?{SEMVER}`?"),
    re.compile(rf"(?m)^Version: `?v?{SEMVER}`?"),
)

DOCUMENTS = (
    "README.md",
    "roadmap.md",
    "CHANGELOG.md",
    "docs/release-notes/next.md",
    "docs/release-notes/v0.2.0.md",
)


def current_version_claims(text: str) -> list[tuple[int, str]]:
    """Return (line, version) for each passage that states current main's version."""
    claims = set()
    for pattern in ACTIVE_VERSION_PATTERNS:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start("version")) + 1
            claims.add((line, match.group("version")))
    return sorted(claims)


def version_mismatches(name: str, text: str, expected: str) -> list[str]:
    """Return one message per active claim that differs from expected."""
    return [
        f"{name}:{line}: documents version {claim}, package is {expected}"
        for line, claim in current_version_claims(text)
        if claim != expected
    ]


class VersionClaimTests(unittest.TestCase):
    def test_historical_versions_are_ignored(self):
        text = (
            "## [0.1.0] - 2026-10-24\n"
            "The only published tag is `v0.1.0`.\n"
            "uv tool install git+https://github.com/example/agiw-mcp@v0.1.0\n"
            "The vendored Nisi is `0.2.0`; the legacy protocol is `2025-11-25`.\n"
        )
        self.assertEqual(current_version_claims(text), [])
        self.assertEqual(version_mismatches("CHANGELOG.md", text, "9.9.9"), [])

    def test_one_active_mismatch_is_rejected_beside_matching_claims(self):
        text = (
            "The tag and current main both report version `0.1.0`.\n"
            "`agiw-mcp --version` prints `agiw-mcp 0.1.0`.\n"
            "Current main still reports version `0.1.1`.\n"
        )
        self.assertEqual(
            version_mismatches("README.md", text, "0.1.0"),
            ["README.md:3: documents version 0.1.1, package is 0.1.0"],
        )

    def test_each_active_claim_form_is_recognized(self):
        text = "\n".join(
            (
                "The tag and current main both report version `0.1.0`.",
                "`agiw-mcp --version` prints `agiw-mcp 0.1.0`.",
                "It returned server `agiw-mcp` `0.1.0` and these tools.",
                "Later commits on main stay version `0.1.0`.",
                "The version stays `0.1.0`.",
                "The package version remains `0.1.0`.",
                "Version: 0.1.0",
            )
        )
        self.assertEqual(
            current_version_claims(text),
            [(line, "0.1.0") for line in range(1, 8)],
        )


class DocumentedVersionTests(unittest.TestCase):
    @unittest.skipUnless(
        all((ROOT / name).is_file() for name in DOCUMENTS),
        "documentation is not all present (an sdist omits some files)",
    )
    def test_every_active_version_claim_matches_the_package(self):
        for name in DOCUMENTS:
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(document=name):
                self.assertTrue(current_version_claims(text), f"{name} must state the package version")
                self.assertEqual(
                    version_mismatches(name, text, __version__),
                    [],
                    f"{name} must agree with __version__ {__version__}",
                )

    @unittest.skipUnless(
        (ROOT / "docs/release-notes/v0.2.0.md").is_file(),
        "the v0.2.0 release notes are not in an sdist",
    )
    def test_v020_release_notes_name_the_assigned_version_and_commit_boundary(self):
        text = (ROOT / "docs/release-notes/v0.2.0.md").read_text(encoding="utf-8")
        self.assertIn("Version: 0.2.0", text)
        self.assertNotIn("Version: TBD", text)
        self.assertIn("f69630a", text)
        self.assertIn("c14a0dd", text)
        self.assertIn("2c3bfd3", text)


if __name__ == "__main__":
    unittest.main()
