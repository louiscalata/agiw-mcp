# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""L5: the opt-in local record of Nisi runs (nisi-activity.jsonl).

The record is off unless config.json sets nisi_activity_log to true. It holds hashes, codes and counts only,
is written under the config directory with the discipline of the Jev log plus a symlink and file-type check,
rotates above 1 MiB, and a write failure never fails the tool.
"""

import json
import os
import stat
import unittest
from unittest import mock

from support import (
    ROOT,
    FakeModelServer,
    IsolatedTestCase,
    node_available,
    write_fake_node,
)

from agiw_mcp import activity, jev, nisi, tools
from agiw_mcp.config import Config, load_config
from agiw_mcp.errors import ToolError

BROKEN = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
HEX = "a" * 64
GOOD = {
    "outcome": "COMPLETED",
    "inputFingerprint": HEX,
    "candidateFingerprint": HEX,
    "repairAttempts": 0,
    "sandbox": "node-permission",
    "ticketed": False,
    "nisiVersion": "0.3.2",
    "elapsedMs": 12,
}
FIELDS = {"at", *GOOD}
POSIX = unittest.skipIf(os.name != "posix", "POSIX file modes and links")

# A stand-in for node whose help lists the permission flag, so a default config reaches the runner.
RUNNER = r"""
import json, sys
if "--version" in sys.argv:
    print("v22.0.0"); sys.exit(0)
if "--help" in sys.argv:
    print("  --permission  enable the permission model"); sys.exit(0)
request = json.loads(sys.stdin.read())
files = [{"path": f["path"], "content": f["content"].replace("a - b", "a + b")} for f in request["files"]]
report = {"outcome": "COMPLETED", "code": None, "candidate": {"files": files}, "repairAttempts": 0,
          "stages": [], "runId": "r", "taskFingerprint": "t", "candidateFingerprint": "c"}
print(json.dumps({"kind": "agiw-mcp.nisi-run.v1", "report": report, "receipts": {}, "syntax": {},
                  "inputFingerprint": "%s"}))
""" % ("b" * 64)

SECRET_TASK = "TASK-SECRET-7f3a make add actually add"
SECRET_BODY = "# BODY-SECRET-91bc\n" + BROKEN
SECRET_PATH = "private-area-5d2e/calc.py"
ARGS = {
    "task": SECRET_TASK,
    "files": [{"path": SECRET_PATH, "content": SECRET_BODY}],
    "acceptance_criteria": ["CRITERION-SECRET-44aa"],
}


class FakeNodeCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.fake = write_fake_node(self.tmp / "bin", RUNNER)
        self.log = self.config_dir / activity.NISI_LOG_FILE
        nisi._FLAG_CACHE.clear()
        self.addCleanup(nisi._FLAG_CACHE.clear)
        patcher = mock.patch.object(nisi, "find_node", return_value=(str(self.fake), (22, 0, 0)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, **values):
        self.write_config(author_model="author-model-ID-SECRET", reviewer_model="reviewer-model-ID-SECRET", **values)
        os.chmod(self.config_dir, 0o700)

    def run_tool(self, args=None):
        return tools.call_tool("agiw_nisi_run", dict(ARGS if args is None else args))

    def lines(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


@POSIX
class DefaultOffTests(FakeNodeCase):
    def test_default_config_is_off(self):
        self.assertIs(Config().nisi_activity_log, False)
        self.configure()
        self.assertIs(load_config().nisi_activity_log, False)

    def test_no_key_writes_nothing_and_creates_no_config_files(self):
        self.configure()
        before = sorted(os.listdir(self.config_dir))
        result, is_error = self.run_tool()
        self.assertFalse(is_error, result)
        self.assertEqual(result["outcome"], "COMPLETED")
        self.assertFalse(self.log.exists())
        self.assertFalse((self.config_dir / (activity.NISI_LOG_FILE + ".1")).exists())
        self.assertEqual(sorted(os.listdir(self.config_dir)), before)

    def test_explicit_false_writes_nothing(self):
        self.configure(nisi_activity_log=False)
        result, is_error = self.run_tool()
        self.assertFalse(is_error, result)
        self.assertFalse(self.log.exists())

    def test_refusals_write_nothing_when_off(self):
        self.write_config()  # no models configured: MODELS_NOT_CONFIGURED, after validation
        result, is_error = self.run_tool()
        self.assertTrue(is_error)
        self.assertEqual(result["error"]["code"], "MODELS_NOT_CONFIGURED")
        self.assertFalse(self.log.exists())

    def test_the_off_state_does_not_touch_the_log_helper(self):
        self.configure()
        with mock.patch.object(activity, "record_nisi", side_effect=AssertionError("log touched")) as record:
            result, is_error = self.run_tool()
        self.assertFalse(is_error, result)
        record.assert_not_called()

    def test_environment_cannot_turn_it_on(self):
        self.configure()
        for name in ("AGIW_MCP_NISI_ACTIVITY_LOG", "AGIW_MCP_NISI_ACTIVITY", "NISI_ACTIVITY_LOG"):
            os.environ[name] = "1"
        self.assertIs(load_config().nisi_activity_log, False)
        result, is_error = self.run_tool()
        self.assertFalse(is_error, result)
        self.assertFalse(self.log.exists())

    def test_tool_argument_cannot_turn_it_on(self):
        self.configure()
        result, is_error = self.run_tool({**ARGS, "nisi_activity_log": True})
        self.assertTrue(is_error)
        self.assertEqual(result["error"]["code"], "INPUT_INVALID")
        self.assertFalse(self.log.exists())
        schema = next(tool for tool in tools.TOOLS if tool["name"] == "agiw_nisi_run")["inputSchema"]
        self.assertNotIn("nisi_activity_log", schema["properties"])


class ConfigKeyTests(IsolatedTestCase):
    def test_key_is_accepted_as_a_boolean(self):
        self.write_config(nisi_activity_log=True)
        config = load_config()
        self.assertIs(config.nisi_activity_log, True)
        self.assertIs(config.public()["nisi_activity_log"], True)
        self.write_config(nisi_activity_log=False)
        self.assertIs(load_config().public()["nisi_activity_log"], False)

    def test_non_boolean_values_are_refused(self):
        for value in ("yes", "true", 1, 0, None, [], {}):
            with self.subTest(value=value):
                self.write_config(nisi_activity_log=value)
                with self.assertRaises(ToolError) as ctx:
                    load_config()
                self.assertEqual(ctx.exception.code, "CONFIG_INVALID")

    def test_status_reports_the_key(self):
        self.write_config(nisi_activity_log=True)
        result, is_error = tools.call_tool("agiw_status", {})
        self.assertFalse(is_error, result)
        self.assertIs(result["config"]["nisi_activity_log"], True)
        self.write_config()
        result, _ = tools.call_tool("agiw_status", {})
        self.assertIs(result["config"]["nisi_activity_log"], False)


class AllowlistTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.log = self.config_dir / activity.NISI_LOG_FILE

    def test_a_good_entry_is_written(self):
        self.assertTrue(activity.record_nisi(**GOOD))
        entry = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertEqual(set(entry), FIELDS)
        self.assertRegex(entry["at"], r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")

    def test_nulls_are_accepted_where_the_design_allows_them(self):
        refused = {**GOOD, "inputFingerprint": None, "candidateFingerprint": None, "repairAttempts": None}
        self.assertTrue(activity.record_nisi(**{**refused, "sandbox": None, "outcome": "NODE_UNAVAILABLE"}))

    def test_text_in_any_field_is_rejected_and_nothing_is_written(self):
        bad = {
            "outcome": ["completed", "COMPLETED ", "see /Users/x/file", "A" * 65, "", None, 7],
            "inputFingerprint": ["task text", HEX[:-1], HEX.upper(), HEX + "0", "", 0],
            "candidateFingerprint": ["task text", HEX[:-1], HEX.upper(), 1],
            "repairAttempts": [True, -1, 1.5, "0", 10**9],
            "sandbox": ["/usr/bin/node", "other", True],
            "ticketed": [0, 1, "yes", None],
            "nisiVersion": ["0.2", "v0.2.0", "0.2.0-x", "a.b.c", "0.2.0\n"],
            "elapsedMs": [-1, 1.5, True, "1", None],
        }
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertFalse(activity.record_nisi(**{**GOOD, key: value}))
        self.assertFalse(self.log.exists())

    def test_unknown_and_missing_fields_are_rejected(self):
        for name in ("task", "path", "ticketId", "owner", "model", "endpoint", "files", "note"):
            with self.subTest(name=name):
                self.assertFalse(activity.record_nisi(**{**GOOD, name: "secret text"}))
        for name in GOOD:
            with self.subTest(missing=name):
                self.assertFalse(activity.record_nisi(**{k: v for k, v in GOOD.items() if k != name}))
        self.assertFalse(self.log.exists())

    def test_every_line_is_short(self):
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertLess(len(self.log.read_bytes()), 512)

    def test_the_jev_log_keeps_its_shape_and_rejects_nisi_fields(self):
        jev_entry = {
            "tool": "agiw_jev_pre",
            "inputSha256": HEX,
            "requestSha256": None,
            "inputBytes": 1,
            "sentBytes": 1,
            "truncated": False,
            "outcome": "ANSWERED",
            "httpStatus": 200,
            "flaggedCount": 0,
            "elapsedMs": 1,
        }
        self.assertTrue(activity.record(**jev_entry))
        line = json.loads((self.config_dir / activity.LOG_FILE).read_text(encoding="utf-8"))
        self.assertEqual(set(line), {"at", *jev_entry})
        self.assertFalse(self.log.exists())
        self.assertFalse(activity.record(**jev_entry, sandbox="none"))
        self.assertFalse(activity.record(ticketed=False))
        self.assertIs(jev.activity, activity)


@POSIX
class LoggedRunTests(FakeNodeCase):
    def test_a_completed_run_leaves_one_line_with_the_designed_fields(self):
        self.configure(nisi_activity_log=True)
        result, is_error = self.run_tool()
        self.assertFalse(is_error, result)
        self.assertEqual(result["outcome"], "COMPLETED")
        entries = self.lines()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(set(entry), FIELDS)
        self.assertEqual(entry["outcome"], "COMPLETED")
        self.assertEqual(entry["inputFingerprint"], "b" * 64)
        self.assertIsNone(entry["candidateFingerprint"])  # the stand-in's "c" is not a SHA-256: dropped, not stored
        self.assertEqual(entry["repairAttempts"], 0)
        self.assertEqual(entry["sandbox"], "node-permission")
        self.assertIs(entry["ticketed"], False)
        self.assertEqual(entry["nisiVersion"], nisi.NISI_VERSION)
        self.assertIsInstance(entry["elapsedMs"], int)
        self.assertGreaterEqual(entry["elapsedMs"], 0)

    def test_the_file_is_private_and_alone_in_the_directory(self):
        self.configure(nisi_activity_log=True)
        self.run_tool()
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config_dir.stat().st_mode), 0o700)
        names = sorted(os.listdir(self.config_dir))
        lock = [activity.NISI_LOG_FILE + ".lock"] if activity.fcntl is not None else []
        self.assertEqual(names, ["config.json", activity.NISI_LOG_FILE, *lock])
        # The persistent flock file is empty and private; it never holds run data.
        for name in lock:
            self.assertEqual((self.config_dir / name).read_bytes(), b"")
            self.assertEqual(stat.S_IMODE((self.config_dir / name).stat().st_mode), 0o600)

    def test_the_log_has_no_request_text_paths_models_or_secrets(self):
        self.configure(nisi_activity_log=True, model_base_url="http://127.0.0.1:45871/v1")
        self.run_tool()
        self.run_tool({**ARGS, "files": [{"path": "other-6c1d/x.py", "content": BROKEN}]})
        text = self.log.read_text(encoding="utf-8")
        for secret in (
            "SECRET",
            "private-area",
            "calc.py",
            "def add",
            "45871",
            "127.0.0.1",
            "author-model",
            "reviewer-model",
            "ticketId",
            "owner",
            str(self.tmp),
            "agiw-mcp-nisi-",
            os.sep,
        ):
            self.assertNotIn(secret, text, secret)
        for entry in self.lines():
            for key, value in entry.items():
                if isinstance(value, str):
                    self.assertRegex(value, r"[A-Za-z0-9_.:-]+", key)

    def test_one_line_per_call_that_passes_validation(self):
        self.configure(nisi_activity_log=True)
        for _ in range(3):
            self.run_tool()
        self.assertEqual(len(self.lines()), 3)

    def test_calls_refused_during_validation_write_nothing(self):
        self.configure(nisi_activity_log=True)
        for args in (
            {"task": ""},
            {"task": "x", "files": [{"path": "../up.py", "content": ""}]},
            {"task": "x", "unknown": 1},
            {},
        ):
            with self.subTest(args=args):
                result, is_error = self.run_tool(args)
                self.assertTrue(is_error)
                self.assertEqual(result["error"]["code"], "INPUT_INVALID")
        self.assertFalse(self.log.exists())

    def test_require_ticket_refusal_precedes_validation_and_writes_nothing(self):
        self.configure(nisi_activity_log=True, require_ticket=True)
        result, is_error = self.run_tool()
        self.assertTrue(is_error)
        self.assertEqual(result["error"]["code"], "TICKET_REQUIRED")
        self.assertFalse(self.log.exists())

    def test_a_refusal_after_validation_is_one_line_with_its_code(self):
        # Flagless Node, default config: NODE_SANDBOX_UNAVAILABLE, after validation.
        flagless = write_fake_node(
            self.tmp / "flagless",
            "import sys\nprint('v22.0.0' if '--version' in sys.argv else '  --check')\n",
        )
        self.configure(nisi_activity_log=True)
        with mock.patch.object(nisi, "find_node", return_value=(str(flagless), (22, 0, 0))):
            result, is_error = self.run_tool()
        self.assertTrue(is_error)
        self.assertEqual(result["error"]["code"], "NODE_SANDBOX_UNAVAILABLE")
        (entry,) = self.lines()
        self.assertEqual(entry["outcome"], "NODE_SANDBOX_UNAVAILABLE")
        self.assertIsNone(entry["inputFingerprint"])
        self.assertIsNone(entry["candidateFingerprint"])
        self.assertIsNone(entry["repairAttempts"])
        self.assertIsNone(entry["sandbox"])
        self.assertIs(entry["ticketed"], False)

    def test_models_not_configured_is_recorded(self):
        self.write_config(nisi_activity_log=True)
        result, is_error = self.run_tool()
        self.assertTrue(is_error)
        (entry,) = self.lines()
        self.assertEqual(entry["outcome"], "MODELS_NOT_CONFIGURED")

    def test_a_non_completed_report_outcome_is_recorded_as_is(self):
        self.configure(nisi_activity_log=True)
        summary = {
            "outcome": "REPAIR_LIMIT",
            "inputFingerprint": HEX,
            "candidateFingerprint": HEX,
            "repairAttempts": 2,
            "sandbox": "none",
        }
        with mock.patch.object(nisi, "_run_validated", return_value=summary):
            result, is_error = self.run_tool()
        self.assertFalse(is_error)
        (entry,) = self.lines()
        self.assertEqual(entry["outcome"], "REPAIR_LIMIT")
        self.assertEqual(entry["repairAttempts"], 2)
        self.assertEqual(entry["sandbox"], "none")
        self.assertEqual(entry["candidateFingerprint"], HEX)

    def test_a_ticketed_refusal_records_ticketed_but_not_the_ticket(self):
        self.configure(nisi_activity_log=True)
        ticket = {
            "ticketId": "T-SECRET-id",
            "claimState": "Granted",
            "writeScope": ["elsewhere"],
            "owner": "OWNER-SECRET",
        }
        result, is_error = self.run_tool({**ARGS, "ticket": ticket})
        self.assertTrue(is_error)
        self.assertEqual(result["error"]["code"], "TICKET_SCOPE_MISMATCH")
        (entry,) = self.lines()
        self.assertEqual(entry["outcome"], "TICKET_SCOPE_MISMATCH")
        self.assertIs(entry["ticketed"], True)
        text = self.log.read_text(encoding="utf-8")
        self.assertNotIn("SECRET", text)
        self.assertNotIn("elsewhere", text)

    def test_an_unexpected_exception_is_recorded_and_still_propagates(self):
        self.configure(nisi_activity_log=True)
        with mock.patch.object(nisi, "_run_validated", side_effect=RuntimeError("boom " + SECRET_TASK)):
            result, is_error = self.run_tool()
        self.assertTrue(is_error)
        self.assertEqual(result["error"]["code"], "INTERNAL_ERROR")
        (entry,) = self.lines()
        self.assertEqual(entry["outcome"], "INTERNAL_ERROR")
        self.assertNotIn("boom", self.log.read_text(encoding="utf-8"))

    def test_an_error_code_that_is_not_a_plain_code_is_stored_as_refused(self):
        self.configure(nisi_activity_log=True)
        with mock.patch.object(nisi, "_run_validated", side_effect=ToolError("not a code /x", "m")):
            result, is_error = self.run_tool()
        self.assertTrue(is_error)
        (entry,) = self.lines()
        self.assertEqual(entry["outcome"], "REFUSED")

    def test_the_result_returned_to_the_client_is_unchanged_by_logging(self):
        self.configure()
        off, _ = self.run_tool()
        self.configure(nisi_activity_log=True)
        on, _ = self.run_tool()
        self.assertEqual(off, on)


@POSIX
class WriteFailureTests(FakeNodeCase):
    def test_an_unwritable_log_does_not_fail_the_run(self):
        self.configure(nisi_activity_log=True)
        self.log.mkdir()  # the log path is a directory: opening it for append fails
        result, is_error = self.run_tool()
        self.assertFalse(is_error, result)
        self.assertEqual(result["outcome"], "COMPLETED")

    def test_a_failing_helper_never_fails_the_tool(self):
        self.configure(nisi_activity_log=True)
        for effect in (OSError("disk"), ValueError("bad"), PermissionError("no")):
            with self.subTest(effect=type(effect).__name__):
                with mock.patch.object(activity, "_prepare_nisi_directory", side_effect=effect):
                    result, is_error = self.run_tool()
                self.assertFalse(is_error, result)
                self.assertEqual(result["outcome"], "COMPLETED")

    def test_an_unexpected_failure_inside_the_helper_is_swallowed(self):
        self.configure(nisi_activity_log=True)
        with mock.patch.object(activity, "record_nisi", side_effect=RuntimeError("boom " + SECRET_TASK)):
            result, is_error = self.run_tool()
        self.assertFalse(is_error, result)
        self.assertEqual(result["outcome"], "COMPLETED")
        with mock.patch.object(activity, "record_nisi", side_effect=RuntimeError("boom")):
            result, is_error = self.run_tool({"task": "x", "files": [{"path": "a.py", "content": ""}], "ticket": 1})
        self.assertTrue(is_error)
        self.assertEqual(result["error"]["code"], "TICKET_INVALID")

    def test_the_helper_reports_false_instead_of_raising(self):
        for effect in (OSError("disk"), PermissionError("no"), ValueError("bad")):
            with self.subTest(effect=type(effect).__name__):
                with mock.patch.object(activity, "_prepare_nisi_directory", side_effect=effect):
                    self.assertFalse(activity.record_nisi(**GOOD))

    def test_a_failed_write_on_a_refusal_keeps_the_original_refusal(self):
        self.write_config(nisi_activity_log=True)
        os.chmod(self.config_dir, 0o700)
        self.log.mkdir()
        result, is_error = self.run_tool()
        self.assertTrue(is_error)
        self.assertEqual(result["error"]["code"], "MODELS_NOT_CONFIGURED")


@POSIX
class FileDisciplineTests(FakeNodeCase):
    def test_a_symlinked_log_is_not_followed(self):
        self.configure(nisi_activity_log=True)
        victim = self.tmp / "victim.txt"
        victim.write_text("keep me\n", encoding="utf-8")
        os.symlink(victim, self.log)
        result, is_error = self.run_tool()
        self.assertFalse(is_error, result)
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep me\n")
        self.assertTrue(self.log.is_symlink())

    def test_a_dangling_symlinked_log_is_not_created_through(self):
        self.configure(nisi_activity_log=True)
        target = self.tmp / "created-elsewhere.txt"
        os.symlink(target, self.log)
        self.assertFalse(activity.record_nisi(**GOOD))
        self.assertFalse(target.exists())

    def test_a_named_pipe_log_does_not_block(self):
        self.configure(nisi_activity_log=True)
        os.mkfifo(self.log)
        self.assertFalse(activity.record_nisi(**GOOD))
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.log).st_mode))

    def test_a_symlinked_rotation_target_is_replaced_not_followed(self):
        self.config_dir.mkdir(mode=0o700)
        victim = self.tmp / "victim.txt"
        victim.write_text("keep me\n", encoding="utf-8")
        previous = self.config_dir / (activity.NISI_LOG_FILE + ".1")
        os.symlink(victim, previous)
        self.log.write_bytes(b"x" * (activity.MAX_LOG_BYTES + 1))
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep me\n")
        self.assertFalse(previous.is_symlink())

    def test_the_log_is_created_0600_whatever_the_umask(self):
        old = os.umask(0)
        self.addCleanup(os.umask, old)
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config_dir.stat().st_mode), 0o700)

    def test_an_existing_log_keeps_its_mode(self):
        self.config_dir.mkdir(mode=0o700)
        self.log.write_text("", encoding="utf-8")
        os.chmod(self.log, 0o640)
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o640)


class RotationTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.config_dir.mkdir(parents=True)
        self.log = self.config_dir / activity.NISI_LOG_FILE
        self.previous = self.config_dir / (activity.NISI_LOG_FILE + ".1")

    def fill(self, marker: bytes, size: int) -> bytes:
        data = marker + b"x" * (size - len(marker))
        self.log.write_bytes(data)
        return data

    def test_the_cap_is_one_mebibyte(self):
        self.assertEqual(activity.MAX_LOG_BYTES, 1024 * 1024)

    def test_a_log_at_the_limit_is_not_rotated(self):
        self.fill(b"first", activity.MAX_LOG_BYTES)
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertFalse(self.previous.exists())
        self.assertGreater(self.log.stat().st_size, activity.MAX_LOG_BYTES)

    def test_a_log_over_the_limit_moves_to_dot_one_and_starts_fresh(self):
        old = self.fill(b"first", activity.MAX_LOG_BYTES + 1)
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(self.previous.read_bytes(), old)
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["outcome"], "COMPLETED")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)

    def test_a_second_rotation_replaces_the_previous_file(self):
        self.fill(b"first", activity.MAX_LOG_BYTES + 1)
        self.assertTrue(activity.record_nisi(**GOOD))
        second = self.fill(b"second", activity.MAX_LOG_BYTES + 1)
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(self.previous.read_bytes(), second)
        self.assertEqual(
            sorted(path.name for path in self.config_dir.iterdir()),
            [activity.NISI_LOG_FILE, activity.NISI_LOG_FILE + ".1"]
            + ([activity.NISI_LOG_FILE + ".lock"] if activity.fcntl is not None else []),
        )

    def test_total_size_stays_bounded_over_many_runs(self):
        line = len(json.dumps({"at": "2026-11-16T00:00:00Z", **GOOD}, separators=(",", ":"))) + 1
        count = (2 * activity.MAX_LOG_BYTES) // line + 50
        for _ in range(count):
            self.assertTrue(activity.record_nisi(**GOOD))
        total = sum(path.stat().st_size for path in self.config_dir.iterdir())
        self.assertLessEqual(total, 2 * activity.MAX_LOG_BYTES + 512)
        self.assertTrue(self.previous.exists())

    def test_the_jev_log_and_the_nisi_log_rotate_independently(self):
        (self.config_dir / activity.LOG_FILE).write_bytes(b"x" * (activity.MAX_LOG_BYTES + 1))
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertFalse(self.previous.exists())
        self.assertFalse((self.config_dir / (activity.LOG_FILE + ".1")).exists())

    def test_rotation_failure_is_reported_false_not_raised(self):
        self.fill(b"first", activity.MAX_LOG_BYTES + 1)
        self.previous.mkdir()
        (self.previous / "keep").write_text("x", encoding="utf-8")
        self.assertFalse(activity.record_nisi(**GOOD))


@unittest.skipUnless(node_available(), "Node.js 22+ not available; the fake-runner tests cover the Python side")
class RealNodeTests(IsolatedTestCase):
    def test_a_real_run_records_hex_fingerprints(self):
        models = FakeModelServer()
        self.addCleanup(models.close)
        models.draft = lambda prompt: {
            "candidate": {
                "files": [
                    {
                        "path": "calc.py",
                        "content": {item["path"]: item["content"] for item in prompt["task"]["currentFiles"]}[
                            "calc.py"
                        ].replace("a - b", "a + b"),
                    }
                ]
            },
            "note": "Use addition.",
        }
        self.write_config(
            model_base_url=models.base + "/v1",
            author_model="author-model",
            reviewer_model="reviewer-model",
            nisi_activity_log=True,
        )
        result, is_error = tools.call_tool(
            "agiw_nisi_run",
            {"task": SECRET_TASK, "files": [{"path": "calc.py", "content": BROKEN}], "repair_budget": 0},
        )
        self.assertFalse(is_error, result)
        self.assertEqual(result["outcome"], "COMPLETED", result)
        lines = (self.config_dir / activity.NISI_LOG_FILE).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["outcome"], "COMPLETED")
        self.assertEqual(entry["inputFingerprint"], result["inputFingerprint"])
        self.assertEqual(entry["candidateFingerprint"], result["candidateFingerprint"])
        self.assertRegex(entry["inputFingerprint"], r"[0-9a-f]{64}\Z")
        self.assertRegex(entry["candidateFingerprint"], r"[0-9a-f]{64}\Z")
        self.assertEqual(entry["sandbox"], result["sandbox"])
        self.assertNotIn("SECRET", lines[0])


class DocumentationTests(unittest.TestCase):
    def text(self, name):
        path = ROOT / name
        if not path.is_file():
            self.skipTest(f"{name} is not in an sdist")
        return " ".join(path.read_text(encoding="utf-8").split())

    def test_security_md_documents_the_opt_in_record(self):
        text = self.text("SECURITY.md")
        self.assertIn("nisi-activity.jsonl", text)
        self.assertIn("nisi_activity_log", text)
        self.assertIn("off by default", text)
        self.assertIn("inputFingerprint", text)
        self.assertIn("nisi-activity.jsonl.1", text)
        self.assertIn(f"rotated when it exceeds {activity.MAX_LOG_BYTES // (1024 * 1024)} MiB", text)
        self.assertNotIn("Nisi runs have no per-call log", text)

    def test_readme_documents_the_key_and_its_default(self):
        text = self.text("README.md")
        self.assertIn("`nisi_activity_log` (default false)", text)
        self.assertIn("nisi-activity.jsonl", text)

    def test_changelog_has_an_unreleased_line(self):
        text = self.text("CHANGELOG.md")
        unreleased = text.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
        self.assertIn("nisi_activity_log", unreleased)
        self.assertIn("nisi-activity.jsonl", unreleased)

    def test_the_docs_do_not_claim_a_way_to_read_it(self):
        # The design adds no tool and no CLI command for reading the record.
        self.assertEqual(len(tools.TOOLS), 6)
        for tool in tools.TOOLS:
            self.assertNotIn("nisi-activity", json.dumps(tool))


if __name__ == "__main__":
    unittest.main()
