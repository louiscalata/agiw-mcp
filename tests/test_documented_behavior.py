# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Client-facing text and SECURITY.md match what the server does.

The agiw_readiness description names the permission-flag requirement that readiness.check_node enforces
and lists the checks in the order readiness.evaluate runs them, and jev-activity.jsonl rotates at
MAX_LOG_BYTES keeping one previous file, as SECURITY.md says.
"""

import json
import os
import subprocess
import sys
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

    def test_readiness_description_lists_checks_in_evaluation_order(self):
        description = next(tool for tool in tools.TOOLS if tool["name"] == "agiw_readiness")["description"]
        order = description.split("in this order:", 1)[1]
        # readiness.evaluate runs python, config, node, nisi, syntax_python, then models.
        phrases = ["Python", "configuration", "Node.js 22+", "Nisi hashes", "syntax Python", "model server"]
        positions = [order.find(phrase) for phrase in phrases]
        self.assertNotIn(-1, positions, dict(zip(phrases, positions, strict=True)))
        self.assertEqual(positions, sorted(positions))


class ActivityRotationTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.config_dir.mkdir(parents=True)
        self.log = self.config_dir / activity.LOG_FILE
        self.previous = self.config_dir / (activity.LOG_FILE + ".1")
        self.lock = self.config_dir / (activity.LOG_FILE + ".lock")

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
        expected = [activity.LOG_FILE, activity.LOG_FILE + ".1"]
        if activity.fcntl is not None and os.name == "posix":
            expected.append(activity.LOG_FILE + ".lock")
        self.assertEqual(sorted(path.name for path in self.config_dir.iterdir()), expected)

    @unittest.skipIf(activity.fcntl is None or os.name != "posix", "flock is POSIX only")
    def test_cross_process_lock_serializes_rotation_and_open(self):
        old = self.fill(b"first", activity.MAX_LOG_BYTES + 1)
        script = (
            "import fcntl, os, sys\n"
            "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "sys.stdin.read()\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", script, str(self.lock)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertEqual(process.stdout.readline(), b"held\n")
            self.assertFalse(activity.record(**ENTRY))
            self.assertEqual(self.log.read_bytes(), old)
            self.assertFalse(self.previous.exists())
        finally:
            process.stdin.close()
            process.wait(timeout=15)
            process.stdout.close()
            process.stderr.close()
        self.assertTrue(activity.record(**ENTRY))
        self.assertEqual(self.previous.read_bytes(), old)
        self.assertEqual(len(self.lines()), 1)

    @unittest.skipUnless((ROOT / "SECURITY.md").is_file(), "SECURITY.md is not in an sdist")
    def test_security_md_states_the_rotation(self):
        text = " ".join((ROOT / "SECURITY.md").read_text(encoding="utf-8").split())
        self.assertIn(f"rotated when it exceeds {activity.MAX_LOG_BYTES // (1024 * 1024)} MiB", text)
        self.assertIn(activity.LOG_FILE + ".1", text)


if __name__ == "__main__":
    unittest.main()
