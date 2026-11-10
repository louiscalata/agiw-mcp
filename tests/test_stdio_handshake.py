# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
import json
import subprocess
import sys

from support import IsolatedTestCase, ROOT, subprocess_env

from agiw_mcp import __version__


class StdioHandshakeTests(IsolatedTestCase):
    def test_module_entry_point_lists_tools(self):
        script = ROOT / "scripts" / "stdio_handshake.py"
        done = subprocess.run(
            [sys.executable, "-B", str(script), sys.executable, "-B", "-m", "agiw_mcp"],
            capture_output=True, cwd=str(ROOT), env=subprocess_env(self.config_dir), timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        summary = json.loads(done.stdout.decode())
        self.assertEqual(summary["protocolVersion"], "2025-03-26")
        self.assertEqual(summary["server"], {"name": "agiw-mcp", "version": __version__})
        self.assertEqual(summary["tools"], [
            "agiw_readiness", "agiw_status", "agiw_consent", "agiw_jev_pre", "agiw_jev_post", "agiw_nisi_run"])
        self.assertEqual(done.stderr, b"")

    def test_missing_command_fails(self):
        script = ROOT / "scripts" / "stdio_handshake.py"
        done = subprocess.run(
            [sys.executable, "-B", str(script), str(self.tmp / "missing-agiw-mcp")],
            capture_output=True, cwd=str(ROOT), env=subprocess_env(self.config_dir), timeout=20)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(b"entry point", done.stderr)


class SuiteCompatScriptTests(IsolatedTestCase):
    def test_rejects_a_directory_without_the_suite_bundle(self):
        script = ROOT / "scripts" / "suite_compat.py"
        done = subprocess.run(
            [sys.executable, "-B", str(script), str(self.tmp)],
            capture_output=True, cwd=str(ROOT), env=subprocess_env(self.config_dir), timeout=20)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(b"not an agiw-suite checkout", done.stderr)
