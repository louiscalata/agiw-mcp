# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Client-facing text and SECURITY.md match what the server does.

The agiw_readiness description names the permission-flag requirement that readiness.check_node enforces,
and jev-activity.jsonl rotates at MAX_LOG_BYTES keeping one previous file, as SECURITY.md says.
"""

import json
import os
import unittest

from support import ROOT, IsolatedTestCase

from agiw_mcp import activity, tools

ENTRY = {
    "tool": "agiw_jev_pre",
    "inputSha256": "0" * 64,
    "requestSha256": None,
    "inputBytes": 1,
    "sentBytes": 1,
    "truncated": False,
    "outcome": "ANSWERED",
    "httpStatus": 200,
    "flaggedCount": 0,
    "elapsedMs": 1,
}


class ReadinessDescriptionTests(unittest.TestCase):
    def test_readiness_description_names_the_permission_flag_and_opt_out(self):
        description = next(tool for tool in tools.TOOLS if tool["name"] == "agiw_readiness")["description"]
        self.assertIn("permission flag", description)
        self.assertIn("allow_unsandboxed_nisi", description)


class ActivityRotationTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.config_dir.mkdir(parents=True)
        self.log = self.config_dir / activity.LOG_FILE
        self.previous = self.config_dir / (activity.LOG_FILE + ".1")

    def fill(self, marker: bytes, size: int) -> bytes:
        data = marker + b"x" * (size - len(marker))
        self.log.write_bytes(data)
        return data

    def lines(self):
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_log_at_the_limit_is_not_rotated(self):
        self.fill(b"first", activity.MAX_LOG_BYTES)
        self.assertTrue(activity.record(**ENTRY))
        self.assertFalse(self.previous.exists())
        self.assertGreater(self.log.stat().st_size, activity.MAX_LOG_BYTES)

    def test_log_over_the_limit_moves_to_dot_one_and_starts_fresh(self):
        old = self.fill(b"first", activity.MAX_LOG_BYTES + 1)
        self.assertTrue(activity.record(**ENTRY))
        self.assertEqual(self.previous.read_bytes(), old)
        lines = self.lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["outcome"], "ANSWERED")
        if os.name == "posix":
            self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)

    def test_second_rotation_replaces_the_previous_file(self):
        self.fill(b"first", activity.MAX_LOG_BYTES + 1)
        self.assertTrue(activity.record(**ENTRY))
        second = self.fill(b"second", activity.MAX_LOG_BYTES + 1)
        self.assertTrue(activity.record(**ENTRY))
        self.assertEqual(self.previous.read_bytes(), second)
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(
            sorted(path.name for path in self.config_dir.iterdir()), [activity.LOG_FILE, activity.LOG_FILE + ".1"]
        )

    @unittest.skipUnless((ROOT / "SECURITY.md").is_file(), "SECURITY.md is not in an sdist")
    def test_security_md_states_the_rotation(self):
        text = " ".join((ROOT / "SECURITY.md").read_text(encoding="utf-8").split())
        self.assertIn(f"rotated when it exceeds {activity.MAX_LOG_BYTES // (1024 * 1024)} MiB", text)
        self.assertIn(activity.LOG_FILE + ".1", text)


if __name__ == "__main__":
    unittest.main()
