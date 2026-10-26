# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
import io
import json
import subprocess
import sys
from unittest import mock

from support import IsolatedTestCase, ROOT, subprocess_env

from agiw_mcp import __main__ as cli, __version__, consent, readiness, tools


class CliTests(IsolatedTestCase):
    def invoke(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "agiw_mcp", *args], input=b"", capture_output=True,
                              cwd=str(ROOT), env=subprocess_env(self.config_dir), timeout=10)

    def test_version(self):
        done = self.invoke("--version")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.decode().strip(), f"agiw-mcp {__version__}")
        self.assertEqual(done.stderr, b"")

    def test_readiness_exit_codes(self):
        for status, expected in (("READY", 0), ("NOT_READY", 1)):
            with self.subTest(status=status), \
                    mock.patch.object(readiness, "evaluate", return_value={"status": status}) as probe, \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                code = cli.main(["readiness"])
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(output.getvalue()), {"status": status})
                probe.assert_called_once_with(stop_at_first=True)

    def test_status(self):
        with mock.patch.object(tools, "status_tool",
                               return_value={"config": None, "configError": "broken"}) as status, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(["status"]), 0)
        status.assert_called_once_with({})
        self.assertEqual(json.loads(output.getvalue()), {"config": None, "configError": "broken"})

    def test_consent_show_and_revoke(self):
        self.grant_consent()
        shown = self.invoke("consent", "show")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        # Windows text-mode stdout writes CRLF; the disclosure itself is LF.
        self.assertIn(consent.disclosure_text(), shown.stdout.decode().replace("\r\n", "\n"))
        self.assertIn("Consent recorded: yes", shown.stdout.decode())
        revoked = self.invoke("consent", "revoke")
        self.assertEqual(revoked.returncode, 0, revoked.stderr)
        self.assertFalse(json.loads(revoked.stdout)["granted"])
        self.assertFalse(consent.consent_recorded())
        self.assertIn(b"Consent recorded: no", self.invoke("consent", "show").stdout)

    def test_non_tty_grant_is_refused(self):
        done = self.invoke("consent", "grant")
        self.assertEqual(done.returncode, 2)
        self.assertIn(b"interactive terminal", done.stderr)
        self.assertFalse(consent.consent_recorded())

    def test_unknown_subcommand_prints_usage(self):
        for args in (("unknown",), ("consent", "unknown"), ("status", "extra")):
            with self.subTest(args=args):
                done = self.invoke(*args)
                self.assertEqual(done.returncode, 2)
                self.assertIn(cli.USAGE, done.stderr.decode())
                self.assertEqual(done.stdout, b"")
