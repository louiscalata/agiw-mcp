# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Configured syntax-interpreter validation, probing, and Nisi integration."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from support import FakeModelServer, IsolatedTestCase, node_available, write_fake_node

from agiw_mcp import nisi, readiness, tickets, tools
from agiw_mcp.config import Config, ConfigError, display_path, load_config
from agiw_mcp.errors import ToolError

ARGS = {"task": "Add an answer.", "new_files": ["answer.py"], "repair_budget": 0}
GOOD_NODE = ("/fake/node", (22, 13, 0))


class CheckPythonConfigTests(IsolatedTestCase):
    def test_unset_uses_none(self):
        self.assertIsNone(load_config().check_python)
        self.assertIsNone(load_config().public()["check_python"])

    def test_valid_absolute_paths_are_loaded_without_existence_checks(self):
        root = os.path.abspath(os.sep)
        for value in (str(self.tmp / "missing python"), root + "a" * (1024 - len(root))):
            with self.subTest(value=value):
                self.write_config(check_python=value)
                self.assertEqual(load_config().check_python, value)

    def test_invalid_values_are_refused_without_echoing_them(self):
        values = [None, True, 3, [], {}, "", "python3", "../python", os.sep + "a" * 1024]
        values.extend(str(self.tmp / f"python{chr(code)}x") for code in (0, 1, 9, 10, 31, 127, 128, 159))
        for value in values:
            with self.subTest(value=repr(value)):
                self.write_config(check_python=value)
                with self.assertRaises(ConfigError) as ctx:
                    load_config()
                self.assertEqual(ctx.exception.code, "CONFIG_INVALID")
                self.assertIn("check_python", ctx.exception.message)
                self.assertNotIn(str(self.tmp), ctx.exception.message)

    def test_environment_cannot_set_or_override_check_python(self):
        configured = str(self.tmp / "configured-python")
        with mock.patch.dict(os.environ, {"AGIW_MCP_CHECK_PYTHON": str(self.tmp / "environment-python")}):
            self.assertIsNone(load_config().check_python)
            self.write_config(check_python=configured)
            self.assertEqual(load_config().check_python, configured)


class CheckPythonProbeTests(IsolatedTestCase):
    def test_real_python_version(self):
        self.assertEqual(nisi.probe_check_python(sys.executable), sys.version.split()[0])

    def test_probe_uses_fixed_isolated_command_and_minimal_environment(self):
        path = str(self.tmp / "private-python")
        done = subprocess.CompletedProcess([], 0, b"3.12.13\n", b"ignored stderr")
        with (
            mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "secret-value", "HTTPS_PROXY": "secret-value"}),
            mock.patch.object(nisi.subprocess, "run", return_value=done) as run,
        ):
            self.assertEqual(nisi.probe_check_python(path), "3.12.13")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:5], [path, "-I", "-S", "-B", "-c"])
        self.assertEqual(len(argv), 6)
        self.assertIn("sys.version_info", argv[5])
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 3)
        self.assertIs(kwargs["check"], False)
        self.assertIs(kwargs["capture_output"], True)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["env"], nisi._probe_env())
        self.assertFalse({"TYPESAFE_API_KEY", "HTTPS_PROXY"} & set(kwargs["env"]))

    def test_only_complete_ascii_version_output_is_accepted(self):
        for stdout in (b"3.12.13", b"3.12.13\n", b"3.12.13\r\n"):
            with self.subTest(stdout=stdout):
                done = subprocess.CompletedProcess([], 0, stdout, b"")
                with mock.patch.object(nisi.subprocess, "run", return_value=done):
                    self.assertEqual(nisi.probe_check_python("/private/python"), "3.12.13")
        for stdout in (b"", b"Python 3.12.13\n", b"3.12\n", b"3.12.13rc1\n", b"3.12.13\nextra", b"garbage", b"\xff"):
            with self.subTest(stdout=stdout):
                done = subprocess.CompletedProcess([], 0, stdout, b"private interpreter output")
                with mock.patch.object(nisi.subprocess, "run", return_value=done):
                    self.assert_probe_refusal("/private/python")

    def assert_probe_refusal(self, path):
        with self.assertRaises(ToolError) as ctx:
            nisi.probe_check_python(path)
        self.assertEqual(ctx.exception.code, "CHECK_PYTHON_UNAVAILABLE")
        self.assertIn("check_python", ctx.exception.message)
        self.assertNotIn(path, ctx.exception.message)
        self.assertNotIn("private interpreter output", ctx.exception.message)
        self.assertEqual(ctx.exception.details, {})

    def test_nonzero_exit_os_error_and_timeout_are_redacted_refusals(self):
        path = str(self.tmp / "private-python")
        done = subprocess.CompletedProcess([], 1, b"3.12.13\n", b"private interpreter output")
        with mock.patch.object(nisi.subprocess, "run", return_value=done):
            self.assert_probe_refusal(path)
        for error in (
            OSError(path),
            subprocess.TimeoutExpired([path], 3, b"private interpreter output"),
            UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed"),
        ):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(nisi.subprocess, "run", side_effect=error):
                    self.assert_probe_refusal(path)

    @unittest.skipUnless(os.name == "posix", "fake interpreter uses a shebang script")
    def test_fake_interpreter_version_garbage_nonzero_and_hang(self):
        cases = [
            ("good", "print('3.9.23')\n", "3.9.23"),
            ("garbage", "print('not-python')\n", None),
            ("nonzero", "import sys\nprint('3.9.23')\nsys.exit(1)\n", None),
            ("hang", "import time\ntime.sleep(20)\n", None),
        ]
        for name, body, expected in cases:
            with self.subTest(case=name):
                path = write_fake_node(self.tmp / name, body)
                started = time.monotonic()
                if expected:
                    self.assertEqual(nisi.probe_check_python(str(path)), expected)
                else:
                    self.assert_probe_refusal(str(path))
                self.assertLess(time.monotonic() - started, 7, "the probe exceeded its bounded deadline")


class CheckPythonRunTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.path = str(self.tmp / "private-python")
        self.write_config(author_model="author-model", reviewer_model="reviewer-model", check_python=self.path)
        self.config = load_config()
        self.parent = self.tmp / "runs"
        self.parent.mkdir()

    def test_bad_checker_is_refused_before_temporary_directory_or_runner(self):
        with (
            mock.patch.object(nisi, "find_node", return_value=GOOD_NODE),
            mock.patch.object(nisi, "permission_base", return_value=["--permission"]),
            mock.patch.object(
                nisi.tempfile, "mkdtemp", side_effect=AssertionError("temporary directory created")
            ) as temp,
            mock.patch.object(nisi, "_bounded_run", side_effect=AssertionError("runner started")) as runner,
        ):
            result, is_error = tools.call_tool("agiw_nisi_run", ARGS)
        self.assertTrue(is_error, result)
        self.assertEqual(result["error"]["code"], "CHECK_PYTHON_UNAVAILABLE")
        self.assertNotIn(self.path, json.dumps(result))
        temp.assert_not_called()
        runner.assert_not_called()
        self.assertEqual(list(self.parent.iterdir()), [])

    @unittest.skipIf(sys.platform == "win32", "ticket admission remains unsupported on Windows")
    def test_bad_checker_releases_admitted_ticket_and_leaves_no_locks(self):
        os.chmod(self.config_dir, 0o700)
        ticket = {"ticketId": "CHECK-PYTHON", "claimState": "Granted", "writeScope": ["answer.py"], "owner": "writer"}
        registry = tickets.registry()
        with (
            mock.patch.object(nisi, "find_node", return_value=GOOD_NODE),
            mock.patch.object(nisi, "permission_base", return_value=["--permission"]),
            mock.patch.object(
                nisi.tempfile, "mkdtemp", side_effect=AssertionError("temporary directory created")
            ) as temp,
        ):
            with self.assertRaises(ToolError) as ctx:
                nisi.run({**ARGS, "ticket": ticket}, self.config, temp_parent=str(self.parent))
        self.assertEqual(ctx.exception.code, "CHECK_PYTHON_UNAVAILABLE")
        self.assertEqual(registry.admitted(), [])
        self.assertEqual(list((self.config_dir / "tickets").glob("*.lock")), [])
        self.assertEqual(list(self.parent.iterdir()), [])
        temp.assert_not_called()

    def test_refused_ticket_precedes_interpreter_probe(self):
        ticket = {"ticketId": "REFUSED", "claimState": "Requested", "writeScope": ["answer.py"], "owner": "writer"}
        with mock.patch.object(nisi, "probe_check_python", side_effect=AssertionError("checker probed")) as probe:
            with self.assertRaises(ToolError) as ctx:
                nisi.run({**ARGS, "ticket": ticket}, self.config)
        self.assertEqual(ctx.exception.code, "TICKET_NOT_ADMISSIBLE")
        probe.assert_not_called()

    def test_python_and_check_python_remain_unknown_tool_arguments(self):
        for key in ("python", "check_python"):
            with self.subTest(key=key):
                result, is_error = tools.call_tool("agiw_nisi_run", {**ARGS, key: self.path})
                self.assertTrue(is_error)
                self.assertEqual(result["error"]["code"], "INPUT_INVALID")
        schema = next(tool["inputSchema"] for tool in tools.TOOLS if tool["name"] == "agiw_nisi_run")
        self.assertIs(schema["additionalProperties"], False)
        self.assertNotIn("python", schema["properties"])
        self.assertNotIn("check_python", schema["properties"])


class CheckPythonReadinessTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        for name, value in (("find_node", GOOD_NODE), ("permission_base", ["--permission"])):
            patch = mock.patch.object(nisi, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(
            readiness, "check_models", return_value={"id": "models", "ok": True, "detail": "fake"}
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_unset_keeps_readiness_rows_and_reports_null_status(self):
        with mock.patch.object(nisi, "probe_check_python", side_effect=AssertionError("unexpected probe")) as probe:
            result = readiness.evaluate()
            self.assertEqual(
                [row["id"] for row in result["checks"][:5]], ["python", "config", "node", "nisi", "models"]
            )
            status = tools.status_tool({})
        self.assertNotIn("syntax_python", [row["id"] for row in result["checks"]])
        self.assertIsNone(status["checkPython"])
        probe.assert_not_called()

    def test_configured_checker_is_after_nisi_and_before_models(self):
        self.write_config(check_python=sys.executable)
        result = readiness.evaluate()
        self.assertEqual(result["status"], "READY", result)
        self.assertEqual(
            [row["id"] for row in result["checks"][:6]],
            ["python", "config", "node", "nisi", "syntax_python", "models"],
        )
        row = next(row for row in result["checks"] if row["id"] == "syntax_python")
        self.assertTrue(row["ok"], row)
        self.assertIn(sys.version.split()[0], row["detail"])
        status = tools.status_tool({})
        self.assertEqual(
            status["checkPython"], {"path": display_path(sys.executable), "version": sys.version.split()[0]}
        )
        self.assertEqual(status["config"]["check_python"], display_path(sys.executable))

    def test_unavailable_checker_is_first_missing_with_redacted_fix(self):
        path = str(self.tmp / "private-unavailable-python")
        self.write_config(check_python=path)
        result = readiness.evaluate()
        self.assertEqual(result["missing"]["id"], "syntax_python")
        self.assertIn("check_python", result["missing"]["fix"])
        self.assertNotIn(path, json.dumps(result))
        status = tools.status_tool({})
        self.assertEqual(status["checkPython"], {"path": display_path(path), "version": None})
        self.assertFalse(status["prerequisites"]["syntax_python"])


@unittest.skipUnless(node_available(), "Node.js 22+ required for the actual host runner")
class CheckPythonEndToEndTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.models = FakeModelServer(
            draft=lambda prompt: {
                "candidate": {"files": [{"path": "answer.py", "content": "answer = 42\n"}]},
                "note": "Add an answer.",
            }
        )
        self.addCleanup(self.models.close)
        self.config = Config(
            model_base_url=self.models.base + "/v1", author_model="author-model", reviewer_model="reviewer-model"
        )

    def run_config(self, config):
        result = nisi.run(ARGS, config)
        self.assertEqual(result["outcome"], "COMPLETED", result)
        self.assertEqual(result["syntax"], {"answer.py": "PASS"})
        return result

    def test_explicit_server_interpreter_preserves_unset_input_fingerprint(self):
        unset = self.run_config(self.config)
        selected = self.run_config(dataclasses.replace(self.config, check_python=sys.executable))
        self.assertEqual(selected["pythonCheckerVersion"], sys.version.split()[0])
        self.assertEqual(selected["inputFingerprint"], unset["inputFingerprint"])

    @unittest.skipUnless(os.name == "posix", "interpreter wrappers use shebang scripts")
    def test_selected_interpreter_runs_checker_and_fingerprint_excludes_its_path(self):
        seen = self.tmp / "checker-calls.jsonl"

        def wrapper(name, version):
            return write_fake_node(
                self.tmp / name,
                "import json, os, pathlib, sys\n"
                "if 'sys.stdin.buffer.read' not in sys.argv[-1]:\n"
                f"    print({version!r}); sys.exit(0)\n"
                f"with pathlib.Path({str(seen)!r}).open('a', encoding='utf-8') as handle:\n"
                "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n",
            )

        first = wrapper("first-python", "3.12.13")
        second = wrapper("second-python", "3.12.13")
        newer = wrapper("newer-python", "3.13.9")
        results = [
            self.run_config(dataclasses.replace(self.config, check_python=str(path))) for path in (first, second, newer)
        ]
        self.assertEqual([result["pythonCheckerVersion"] for result in results], ["3.12.13", "3.12.13", "3.13.9"])
        self.assertEqual(results[0]["inputFingerprint"], results[1]["inputFingerprint"])
        self.assertNotEqual(results[0]["inputFingerprint"], results[2]["inputFingerprint"])
        calls = [json.loads(line) for line in seen.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 3, "the configured checker must run for each Python candidate")
        self.assertTrue(all(argv[:4] == ["-I", "-S", "-B", "-c"] for argv in calls))
        self.assertTrue(all("compile(source" in argv[-1] for argv in calls))


if __name__ == "__main__":
    unittest.main()
