# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Pin the documented syntax-interpreter grammar and refusal boundary."""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from support import ROOT, IsolatedTestCase

from agiw_mcp import nisi, tools
from agiw_mcp.errors import ToolError

REFUSAL = {
    "error": {
        "code": "CHECK_PYTHON_UNAVAILABLE",
        "message": (
            "The check_python interpreter could not report X.Y.Z within 3 seconds. "
            "Set check_python in config.json to a working Python interpreter, or remove the key "
            "to use the server's interpreter."
        ),
    }
}


class DocumentedCheckPythonTests(IsolatedTestCase):
    @unittest.skipUnless(
        all((ROOT / name).is_file() for name in ("README.md", "SECURITY.md")),
        "README.md and SECURITY.md are not both in an sdist",
    )
    def test_readme_and_security_match_the_fixed_probe_and_config_only_contract(self):
        specific = {
            "README.md": (
                "with a fixed message that does not include the path",
                "The path is not an environment override or a tool argument.",
            ),
            "SECURITY.md": (
                "with a fixed path-free message",
                "`check_python` is config-only, not an environment override or tool argument.",
            ),
        }
        common = ("`-I -S -B`", "3 second limit", "strict `X.Y.Z` version", "`CHECK_PYTHON_UNAVAILABLE`")
        for name, phrases in specific.items():
            text = " ".join((ROOT / name).read_text(encoding="utf-8").split())
            for phrase in common + phrases:
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, text)
        path = str(self.tmp / "configured interpreter")
        done = subprocess.CompletedProcess([], 0, b"3.12.13\n", b"")
        with mock.patch.object(nisi.subprocess, "run", return_value=done) as run:
            self.assertEqual(nisi.probe_check_python(path), "3.12.13")
        run.assert_called_once_with(
            [path, "-I", "-S", "-B", "-c", 'import sys; print("%d.%d.%d" % sys.version_info[:3])'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=3,
            check=False,
            env=mock.ANY,
        )

    def test_plain_versions_have_no_minimum_and_preserve_all_components(self):
        # The server's own Python minimum must not become a checker-version gate.
        for version in ("0.0.0", "2.7.18", "3.9.23", "123.456.789"):
            for ending in (b"", b"\n", b"\r\n"):
                with self.subTest(version=version, ending=ending):
                    done = subprocess.CompletedProcess([], 0, version.encode("ascii") + ending, b"")
                    with mock.patch.object(nisi.subprocess, "run", return_value=done):
                        self.assertEqual(nisi.probe_check_python(str(self.tmp / "checker")), version)

    def test_plain_version_rejects_whitespace_separators_and_extra_records(self):
        invalid = (
            b" 3.12.13",
            b"3.12.13 ",
            b"\t3.12.13",
            b"3.12.13\r",
            b"3.12.13\n\n",
            b"\n3.12.13",
            b"3x12x13",
            b"3.12.13.0",
            b"3.12.13+local",
            b"3.12.13\x00",
            b"3.12.13\n3.12.13",
            "３.１２.１３".encode(),
        )
        for output in invalid:
            with self.subTest(output=output):
                done = subprocess.CompletedProcess([], 0, output, b"private stderr")
                with mock.patch.object(nisi.subprocess, "run", return_value=done):
                    with self.assertRaises(ToolError) as caught:
                        nisi.probe_check_python(str(self.tmp / "checker"))
                self.assertEqual(caught.exception.payload(), REFUSAL)

    def test_all_probe_failures_have_one_exact_path_free_payload(self):
        path = str(self.tmp / "private interpreter")
        failures = (
            {"return_value": subprocess.CompletedProcess([], 1, b"3.12.13", path.encode())},
            {"return_value": subprocess.CompletedProcess([], 0, path.encode(), b"private stderr")},
            {"side_effect": OSError(path)},
            {"side_effect": subprocess.TimeoutExpired([path], 3, output=path.encode(), stderr=path.encode())},
            {"side_effect": subprocess.SubprocessError(path)},
            {"side_effect": ValueError(path)},
        )
        for failure in failures:
            with self.subTest(failure=failure), mock.patch.object(nisi.subprocess, "run", **failure):
                with self.assertRaises(ToolError) as caught:
                    nisi.probe_check_python(path)
                self.assertEqual(caught.exception.payload(), REFUSAL)
                self.assertEqual(caught.exception.details, {})
                self.assertNotIn(path, caught.exception.message)

    def test_no_tool_accepts_the_config_only_interpreter_key(self):
        arguments = {
            "agiw_readiness": {},
            "agiw_status": {},
            "agiw_consent": {"action": "show"},
            "agiw_jev_pre": {"task": "Inspect the change."},
            "agiw_jev_post": {"report": "The check completed."},
            "agiw_nisi_run": {"task": "Add an answer.", "new_files": ["answer.py"]},
        }
        with mock.patch.object(nisi, "find_node", return_value=(None, None)):
            for tool in tools.TOOLS:
                with self.subTest(tool=tool["name"]):
                    schema = tool["inputSchema"]
                    self.assertNotIn("check_python", schema["properties"])
                    self.assertIs(schema["additionalProperties"], False)
                    result, is_error = tools.call_tool(
                        tool["name"], {**arguments[tool["name"]], "check_python": str(self.tmp / "checker")}
                    )
                    self.assertTrue(is_error, result)
                    self.assertEqual(result["error"]["code"], "INPUT_INVALID", result)


if __name__ == "__main__":
    unittest.main()
