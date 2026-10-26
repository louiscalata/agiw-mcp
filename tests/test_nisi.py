# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from support import (
    FakeModelServer, IsolatedTestCase, node_availability, skip_without_node, tree_snapshot, write_fake_node,
)

from agiw_mcp import nisi, tools
from agiw_mcp.config import load_config
from agiw_mcp.errors import ToolError

BROKEN = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"


def current(prompt, path):
    return {item["path"]: item["content"] for item in prompt["task"]["currentFiles"]}[path]


class NisiInputTests(IsolatedTestCase):
    def test_validation(self):
        good = {"task": "Fix add", "files": [{"path": "calc.py", "content": BROKEN}]}
        self.assertEqual(nisi.validate_request(good)["allowed"], ["calc.py"])
        self.assertEqual(nisi.validate_request(good)["language"], "python")
        bad_cases = [
            {"task": ""},
            {"task": "x", "files": [{"path": "/etc/passwd", "content": ""}]},
            {"task": "x", "files": [{"path": "../up.py", "content": ""}]},
            {"task": "x", "files": [{"path": "a\\b.py", "content": ""}]},
            {"task": "x", "files": [{"path": "a.py", "content": "x"}, {"path": "a.py", "content": "y"}]},
            {"task": "x", "files": [{"path": "a.py", "content": "x" * (nisi.MAX_FILE_BYTES + 1)}]},
            {"task": "x", "files": [{"path": "a.py"}]},
            {"task": "x"},
            {"task": "x", "new_files": ["a.py"], "repair_budget": 9},
            {"task": "x", "new_files": ["a.py"], "assertions": [{"path": "b.py", "contains": "y"}]},
            {"task": "x", "new_files": ["a.py"], "unknown": 1},
        ]
        for case in bad_cases:
            with self.assertRaises(ToolError, msg=str(case)[:80]):
                nisi.validate_request(case)

    def test_requires_two_distinct_models(self):
        args = {"task": "Fix add", "files": [{"path": "calc.py", "content": BROKEN}]}
        with self.assertRaises(ToolError) as ctx:
            nisi.run(args, load_config())
        self.assertEqual(ctx.exception.code, "MODELS_NOT_CONFIGURED")
        self.write_config(author_model="m", reviewer_model="m")
        with self.assertRaises(ToolError) as ctx:
            nisi.run(args, load_config())
        self.assertEqual(ctx.exception.code, "MODELS_NOT_DISTINCT")

    def test_files_and_new_files_share_the_path_limit(self):
        files = [{"path": f"f{index}.py", "content": "x"} for index in range(nisi.MAX_FILES)]
        new_files = [f"n{index}.py" for index in range(nisi.MAX_FILES)]
        self.assertEqual(len(nisi.validate_request({"task": "edit", "files": files})["allowed"]), nisi.MAX_FILES)
        only_new = nisi.validate_request({"task": "edit", "new_files": new_files})
        self.assertEqual(len(only_new["allowed"]), nisi.MAX_FILES)
        half = nisi.MAX_FILES // 2
        combined = nisi.validate_request({"task": "edit", "files": files[:half], "new_files": new_files[:half]})
        self.assertEqual(len(combined["allowed"]), nisi.MAX_FILES)
        for case in ({"files": files, "new_files": new_files},
                     {"files": files[:nisi.MAX_FILES - 1], "new_files": new_files[:2]}):
            with self.subTest(files=len(case["files"]), new_files=len(case["new_files"])):
                with self.assertRaises(ToolError) as ctx:
                    nisi.validate_request({"task": "edit", **case})
                self.assertEqual(ctx.exception.code, "INPUT_INVALID")
                self.assertIn(str(nisi.MAX_FILES), ctx.exception.message)


class ProbeEnvTests(IsolatedTestCase):
    """Node 24 on Windows exits 134 (CSPRNG assertion) during the probes when SystemRoot is dropped."""

    SECRETS = {"TYPESAFE_API_KEY": "secret-value-123", "ANTHROPIC_API_KEY": "secret-value-123",
               "GITHUB_TOKEN": "secret-value-123", "HTTP_PROXY": "secret-value-123",
               "HTTPS_PROXY": "secret-value-123", "https_proxy": "secret-value-123",
               "NO_PROXY": "secret-value-123", "ALL_PROXY": "secret-value-123", "HOME": "/home/someone"}
    WINDOWS = {"PATH": r"C:\node", "systemroot": r"C:\Windows", "SystemDrive": "C:", "windir": r"C:\Windows"}

    def probe_env(self, os_name, environ):
        with mock.patch.dict(os.environ, environ, clear=True), mock.patch.object(nisi.os, "name", os_name):
            return nisi._probe_env()

    def test_windows_keeps_system_vars_case_insensitively(self):
        env = self.probe_env("nt", self.WINDOWS)
        self.assertEqual(env, {"PATH": r"C:\node", "LANG": "C.UTF-8", "NODE_NO_WARNINGS": "1",
                               "SystemRoot": r"C:\Windows", "SYSTEMDRIVE": "C:", "WINDIR": r"C:\Windows"})

    def test_windows_still_strips_keys_and_proxies(self):
        env = self.probe_env("nt", {**self.WINDOWS, **self.SECRETS})
        self.assertEqual(set(env), {"PATH", "LANG", "NODE_NO_WARNINGS", "SystemRoot", "SYSTEMDRIVE", "WINDIR"})
        self.assertNotIn("secret-value-123", env.values())

    def test_windows_omits_unset_system_vars(self):
        self.assertEqual(self.probe_env("nt", {"PATH": r"C:\node", "WINDIR": ""}),
                         {"PATH": r"C:\node", "LANG": "C.UTF-8", "NODE_NO_WARNINGS": "1"})

    def test_posix_is_unchanged(self):
        env = self.probe_env("posix", {**self.WINDOWS, **self.SECRETS, "PATH": "/usr/bin"})
        self.assertEqual(env, {"PATH": "/usr/bin", "LANG": "C.UTF-8", "NODE_NO_WARNINGS": "1"})

    def test_windows_failed_help_probe_still_refuses(self):
        nisi._FLAG_CACHE.clear()
        self.addCleanup(nisi._FLAG_CACHE.clear)
        crashed = subprocess.CompletedProcess(["node", "--help"], 134, b"", b"")
        with mock.patch.dict(os.environ, self.WINDOWS, clear=True), mock.patch.object(nisi.os, "name", "nt"), \
                mock.patch.object(nisi.subprocess, "run", return_value=crashed) as run:
            with self.assertRaises(ToolError) as ctx:
                nisi.permission_flags(r"C:\node\node.exe", r"C:\work")
        self.assertEqual(ctx.exception.code, "NODE_UNAVAILABLE")
        self.assertNotIn(r"C:\node\node.exe", nisi._FLAG_CACHE)
        self.assertEqual(run.call_args.kwargs["env"]["SystemRoot"], r"C:\Windows")


@unittest.skipIf(os.name != "posix", "fake runner uses a shebang script")
class FakeRunnerTests(IsolatedTestCase):
    """Exercises the Python side with a stand-in for node (used where Node is unavailable)."""

    RUNNER = r'''
import json, sys
if "--version" in sys.argv:
    print("v22.0.0"); sys.exit(0)
if "--help" in sys.argv:
    print("  --check  syntax check"); sys.exit(0)
request = json.loads(sys.stdin.read())
MODE = %r
if MODE == "garbage":
    print("not json"); sys.exit(0)
files = [{"path": f["path"], "content": f["content"].replace("a - b", "a + b")} for f in request["files"]]
if MODE == "escape":
    files = [{"path": "../../etc/hosts", "content": "x"}]
report = {"outcome": "COMPLETED", "code": None, "candidate": {"files": files}, "repairAttempts": 0,
          "stages": [{"stage": "review", "status": "PASS", "code": None,
                      "evidence": {"attempt": 0, "findings": [], "summary": "ok", "reviewerId": "agiw.reviewer"}}],
          "runId": "r", "taskFingerprint": "t", "candidateFingerprint": "c"}
print(json.dumps({"kind": "agiw-mcp.nisi-run.v1", "report": report, "receipts": {"author": [], "reviewer": []},
                  "syntax": {"calc.py": "PASS"}, "seenRequest": sorted(request)}))
'''

    def run_with(self, mode):
        fake = write_fake_node(self.tmp / "bin", self.RUNNER % mode)
        self.write_config(author_model="author-model", reviewer_model="reviewer-model")
        nisi._FLAG_CACHE.clear()
        self.addCleanup(nisi._FLAG_CACHE.clear)
        parent = self.tmp / "tmpparent"
        parent.mkdir()
        with mock.patch.object(nisi, "find_node", return_value=(str(fake), (22, 0, 0))):
            try:
                return nisi.run({"task": "Fix add", "files": [{"path": "calc.py", "content": BROKEN}]},
                                load_config(), temp_parent=str(parent))
            finally:
                self.assertEqual(os.listdir(parent), [], "temporary directory was not removed")

    def test_fake_runner_round_trip(self):
        result = self.run_with("ok")
        self.assertEqual(result["outcome"], "COMPLETED")
        self.assertEqual(result["proposedFiles"], [{"path": "calc.py", "status": "modified", "content": FIXED}])
        self.assertEqual(result["sandbox"], "none")

    def test_garbage_output_is_refused(self):
        with self.assertRaises(ToolError) as ctx:
            self.run_with("garbage")
        self.assertEqual(ctx.exception.code, "NISI_RUNNER_FAILED")

    def test_out_of_scope_file_is_refused(self):
        with self.assertRaises(ToolError) as ctx:
            self.run_with("escape")
        self.assertEqual(ctx.exception.code, "NISI_RUNNER_FAILED")

    def test_bounded_run_kills_on_timeout(self):
        fake = write_fake_node(self.tmp / "slow", "import time\ntime.sleep(30)\n")
        code, out, state = nisi._bounded_run([str(fake)], data=b"", cwd=str(self.tmp), env=dict(os.environ),
                                             timeout=0.5)
        self.assertEqual(state, "timeout")


def flags_or_skip(test, node, workdir):
    """Return Node's permission flags, or skip when the probe cannot run."""
    try:
        return nisi.permission_flags(node, workdir)
    except ToolError as exc:
        if exc.code == "NODE_UNAVAILABLE":
            test.skipTest("Node.js is not available: " + exc.message)
        raise


class NodeUnavailableSkipTests(IsolatedTestCase):
    def test_unusable_node_skips_with_a_clear_reason(self):
        saved = dict(nisi._FLAG_CACHE)
        self.addCleanup(nisi._FLAG_CACHE.update, saved)
        self.addCleanup(nisi._FLAG_CACHE.clear)
        nisi._FLAG_CACHE.clear()
        crashed = subprocess.CompletedProcess(["node", "--help"], 1, b"", b"")
        with mock.patch.object(nisi, "find_node", return_value=("/usr/bin/node", (22, 0, 0))), \
                mock.patch.object(nisi.subprocess, "run", return_value=crashed):
            available, reason = node_availability()
            self.assertFalse(available)
            self.assertIn("Node.js is not available", reason)
            self.assertIn("node --help failed", reason)
            with self.assertRaises(unittest.SkipTest) as ctx:
                flags_or_skip(self, "/usr/bin/node", "/work")
        self.assertIn("Node.js is not available", str(ctx.exception))
        self.assertIn("node --help failed", str(ctx.exception))


@skip_without_node
class NisiEndToEndTests(IsolatedTestCase):
    """Real Node and the vendored Nisi against a fake loopback model server."""

    def setUp(self):
        super().setUp()
        self.models = FakeModelServer()
        self.addCleanup(self.models.close)
        self.write_config(model_base_url=self.models.base + "/v1", author_model="author-model",
                          reviewer_model="reviewer-model")

    def run_tool(self, args):
        result, is_error = tools.call_tool("agiw_nisi_run", args)
        self.assertFalse(is_error, result)
        return result

    def chat_bodies(self):
        return [json.loads(record["body"]) for record in self.models.requests
                if record["path"] == "/v1/chat/completions"]

    def test_reasoning_effort_is_sent_only_when_configured(self):
        # Reasoning models on LM Studio return empty content under JSON-schema output unless this is "none".
        self.models.draft = lambda prompt: {"candidate": {"files": [
            {"path": "lib.mjs", "content": "export const answer = 42;\n"}]}, "note": "New module."}
        args = {"task": "Add a module.", "new_files": ["lib.mjs"], "repair_budget": 0}
        result = self.run_tool(args)
        self.assertTrue(self.chat_bodies())
        self.assertFalse([body for body in self.chat_bodies() if "reasoning_effort" in body])
        self.assertIsNone(result["reasoningEffort"])
        self.models.requests.clear()
        os.environ["AGIW_MCP_REASONING_EFFORT"] = "none"
        result = self.run_tool(args)
        bodies = self.chat_bodies()
        self.assertEqual({op for op, _ in self.models.calls}, {"draft", "review"})
        self.assertTrue(bodies)
        self.assertEqual({body.get("reasoning_effort") for body in bodies}, {"none"})
        self.assertEqual(result["reasoningEffort"], "none")
        self.assertEqual(result["outcome"], "COMPLETED", result)

    def test_completed_run_with_no_side_effects(self):
        self.models.draft = lambda prompt: {"candidate": {"files": [
            {"path": "calc.py", "content": current(prompt, "calc.py").replace("a - b", "a + b")}]},
            "note": "Use addition."}
        project = self.tmp / "project"
        project.mkdir()
        (project / "calc.py").write_text(BROKEN, encoding="utf-8")
        home = self.tmp / "home"
        (home / ".config").mkdir(parents=True)
        temp_parent = self.tmp / "tmpparent"
        temp_parent.mkdir()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        watched = [project, home, self.config_dir]
        before = [tree_snapshot(path) for path in watched]
        old_cwd = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, old_cwd)
        with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(tempfile, "tempdir",
                                                                                str(temp_parent)):
            result = self.run_tool({"task": "Fix add so it adds.",
                                    "files": [{"path": "calc.py", "content": BROKEN}],
                                    "assertions": [{"path": "calc.py", "contains": "a + b"}]})
        self.assertEqual(result["outcome"], "COMPLETED", result)
        self.assertEqual(result["proposedFiles"], [{"path": "calc.py", "status": "modified", "content": FIXED}])
        self.assertEqual(result["syntax"], {"calc.py": "PASS"})
        self.assertEqual(result["sandbox"], "node-permission")
        self.assertEqual(self.models.calls, [("draft", "author-model"), ("review", "reviewer-model")])
        # Nothing outside the private temporary directory changed, and it was removed.
        self.assertEqual([tree_snapshot(path) for path in watched], before)
        self.assertEqual(os.listdir(temp_parent), [])
        self.assertEqual(os.getcwd(), str(project))

    def test_reviewer_uses_separate_server_when_configured(self):
        reviewer_server = FakeModelServer(models=("reviewer-model",))
        self.addCleanup(reviewer_server.close)
        self.write_config(model_base_url=self.models.base + "/v1", reviewer_base_url=reviewer_server.base + "/v1",
                          author_model="author-model", reviewer_model="reviewer-model")
        self.models.draft = lambda prompt: {"candidate": {"files": [
            {"path": "lib.mjs", "content": "export const answer = 42;\n"}]}, "note": "New module."}
        result = self.run_tool({"task": "Add a module.", "new_files": ["lib.mjs"], "repair_budget": 0})
        self.assertEqual(result["outcome"], "COMPLETED", result)
        self.assertEqual(self.models.calls, [("draft", "author-model")])
        self.assertEqual(reviewer_server.calls, [("review", "reviewer-model")])

    @unittest.skipIf(os.name != "posix", "fake runner uses a shebang script")
    def test_failed_probe_refuses_and_is_not_cached(self):
        marker = self.tmp / "started"
        fake = write_fake_node(self.tmp / "badbin", "import sys, pathlib\n"
                               "if '--help' in sys.argv: sys.exit(1)\n"
                               f"pathlib.Path({str(marker)!r}).write_text('started')\n")
        nisi._FLAG_CACHE.clear()
        self.addCleanup(nisi._FLAG_CACHE.clear)
        with self.assertRaises(ToolError) as ctx:
            nisi.permission_flags(str(fake), "/work")
        self.assertEqual(ctx.exception.code, "NODE_UNAVAILABLE")
        self.assertNotIn(str(fake), nisi._FLAG_CACHE)
        self.write_config(author_model="author-model", reviewer_model="reviewer-model")
        parent = self.tmp / "tmpparent-probe"
        parent.mkdir()
        with mock.patch.object(nisi, "find_node", return_value=(str(fake), (22, 0, 0))):
            with self.assertRaises(ToolError) as ctx:
                nisi.run({"task": "Fix add", "files": [{"path": "calc.py", "content": BROKEN}]}, load_config(),
                         temp_parent=str(parent))
        self.assertEqual(ctx.exception.code, "NODE_UNAVAILABLE")
        self.assertFalse(marker.exists(), "Node was started without a sandbox")
        self.assertEqual(os.listdir(parent), [])

    @unittest.skipIf(os.name != "posix", "fake runner uses a shebang script")
    def test_child_env_is_exactly_the_documented_set(self):
        # Grok's delta review: nothing pinned the runner environment, so a key could have leaked into it.
        for name in ("TYPESAFE_API_KEY", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "SYSTEMROOT", "WINDIR"):
            os.environ[name] = "secret-value-123"
        env = nisi._child_env("/opt/node/bin/node", "/tmp/work")
        expected = {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "NODE_NO_WARNINGS"}
        if os.name == "nt":
            expected |= {"SYSTEMROOT", "WINDIR"}
        self.assertEqual(set(env), expected)
        self.assertNotIn("secret-value-123", {value for key, value in env.items() if key not in ("SYSTEMROOT", "WINDIR")})
        self.assertEqual((env["HOME"], env["TMPDIR"], env["TMP"], env["TEMP"]), ("/tmp/work",) * 4)
        self.assertEqual((env["LANG"], env["NODE_NO_WARNINGS"]), ("C.UTF-8", "1"))

    def test_probes_get_no_keys_or_proxies(self):
        seen = self.tmp / "probe-env.json"
        fake = write_fake_node(self.tmp / "envbin", "import json, os, pathlib, sys\n"
                               f"pathlib.Path({str(seen)!r}).write_text(json.dumps(sorted(os.environ)))\n"
                               "sys.stdout.write('v22.13.0\\n' if '--version' in sys.argv else '  --permission\\n')\n")
        for name in ("TYPESAFE_API_KEY", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
            os.environ[name] = "secret-value-123"
        nisi._FLAG_CACHE.clear()
        self.addCleanup(nisi._FLAG_CACHE.clear)
        def check():
            names = json.loads(seen.read_text())
            self.assertIn("PATH", names)
            self.assertFalse({"TYPESAFE_API_KEY", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"} & set(names), names)
            seen.unlink()

        flags_or_skip(self, str(fake), "/work")
        check()
        with mock.patch.object(nisi.shutil, "which", return_value=str(fake)):
            self.assertEqual(nisi.find_node(), (str(fake), (22, 13, 0)))
        check()

    @unittest.skipIf(os.name != "posix", "fake runner uses a shebang script")
    def test_comma_in_temp_path_is_refused(self):
        fake = write_fake_node(self.tmp / "bin", FakeRunnerTests.RUNNER % "ok")
        self.write_config(author_model="author-model", reviewer_model="reviewer-model")
        parent = self.tmp / "a,b"
        parent.mkdir()
        nisi._FLAG_CACHE.clear()
        self.addCleanup(nisi._FLAG_CACHE.clear)
        with mock.patch.object(nisi, "find_node", return_value=(str(fake), (22, 0, 0))):
            with self.assertRaises(ToolError) as ctx:
                nisi.run({"task": "Fix add", "files": [{"path": "calc.py", "content": BROKEN}]}, load_config(),
                         temp_parent=str(parent))
        self.assertEqual(ctx.exception.code, "TEMP_DIR_UNSAFE")
        self.assertEqual(os.listdir(parent), [])

    @unittest.skipIf(os.name != "posix", "fake runner uses a shebang script")
    def test_permission_flag_detection(self):
        # Node 22.13+ prints both names on one line; Node 24 prints only --permission; early Node 22 only the
        # experimental name. Picking the experimental name on 22.13+ breaks the checker child.
        helps = {"  --experimental-permission, --permission\n": "--permission",
                 "  --permission                  enable the permission model\n": "--permission",
                 "  --experimental-permission     enable the permission model\n": "--experimental-permission",
                 "  --no-experimental-permission-x\n": None}
        for index, (text, expected) in enumerate(helps.items()):
            fake = write_fake_node(self.tmp / f"bin{index}", f"import sys\nsys.stdout.write({text!r})\n")
            nisi._FLAG_CACHE.clear()
            self.addCleanup(nisi._FLAG_CACHE.clear)
            flags, sandbox = flags_or_skip(self, str(fake), "/work")
            self.assertEqual(flags[0] if flags else None, expected, text)
            self.assertEqual(sandbox, "node-permission" if expected else "none")

    def test_permission_flags_block_writes_outside_the_work_dir(self):
        node, _ = nisi.find_node()
        work = self.tmp / "work"
        work.mkdir()
        outside = self.tmp / "outside.txt"
        script = work / "probe.mjs"
        script.write_text(
            "import fs from 'node:fs';\n"
            f"try {{ fs.writeFileSync({json.dumps(str(outside))}, 'x'); console.log('WROTE'); }}\n"
            "catch (error) { console.log(error.code); }\n"
            f"fs.writeFileSync({json.dumps(str(work / 'inside.txt'))}, 'ok'); console.log('INSIDE');\n",
            encoding="utf-8")
        flags, sandbox = flags_or_skip(self, node, str(work))
        if sandbox != "node-permission":
            self.skipTest("this Node has no permission model")
        done = subprocess.run([node, *flags, str(script)], capture_output=True, timeout=30,
                              env=nisi._child_env(node, str(work)), cwd=str(work))
        self.assertEqual(done.stdout.decode().split(), ["ERR_ACCESS_DENIED", "INSIDE"])
        self.assertFalse(outside.exists())

    def test_syntax_error_is_repaired(self):
        self.models.draft = lambda prompt: {"candidate": {"files": [
            {"path": "calc.py", "content": "def add(a, b)\n    return a + b\n"}]}, "note": "Draft."}
        seen = {}

        def repair(prompt):
            seen["stages"] = prompt["stages"]
            return {"status": "REPAIRED", "candidate": {"files": [{"path": "calc.py", "content": FIXED}]},
                    "note": "Add the missing colon."}
        self.models.repair = repair
        result = self.run_tool({"task": "Fix add.", "files": [{"path": "calc.py", "content": BROKEN}]})
        self.assertEqual(result["outcome"], "COMPLETED", result)
        self.assertEqual(result["repairAttempts"], 1)
        failed = [stage for stage in seen["stages"] if stage["stage"] == "staticChecks"][0]
        self.assertEqual(failed["status"], "FAIL")
        self.assertIn("calc.py", failed["evidence"]["findings"][0]["message"])

    def test_unchanged_draft_ends_without_progress(self):
        self.models.draft = lambda prompt: {"candidate": {"files": [
            {"path": "calc.py", "content": current(prompt, "calc.py")}]}, "note": "No change."}
        result = self.run_tool({"task": "Fix add.", "files": [{"path": "calc.py", "content": BROKEN}]})
        self.assertEqual(result["outcome"], "NO_PROGRESS")
        self.assertFalse(result["completed"])
        tests_stage = [stage for stage in result["stages"] if stage["stage"] == "tests"][0]
        self.assertEqual(tests_stage["failures"][0]["code"], "NO_CHANGE")

    def test_reviewer_findings_fail_the_run(self):
        self.models.draft = lambda prompt: {"candidate": {"files": [
            {"path": "lib.mjs", "content": "export const answer = 42;\n"}]}, "note": "New module."}
        self.models.review = lambda prompt: {"findings": [{"code": "NAMING", "message": "Name is unclear."}],
                                             "summary": "One naming issue."}
        result = self.run_tool({"task": "Add a module.", "new_files": ["lib.mjs"], "repair_budget": 0})
        self.assertEqual(result["outcome"], "FAILED")
        self.assertEqual(result["syntax"], {"lib.mjs": "PASS"}, result)
        self.assertEqual(result["proposedFiles"][0]["status"], "new")

    def test_javascript_syntax_checks_by_module_kind(self):
        # node --check runs on stdin under the permission model; given a path it would resolve parent
        # folders the model refuses (seen on Node 22 in CI), and every file would report FAIL.
        files = {"ok.mjs": "export const answer = 42;\n", "ok.cjs": "module.exports = { answer: 42 };\n",
                 "esm.js": "export default function f() { return 1; }\n", "bad.mjs": "export const a = 1;\nconst = ;\n"}
        self.models.draft = lambda prompt: {"candidate": {"files": [
            {"path": path, "content": content} for path, content in files.items()]}, "note": "Four modules."}
        result = self.run_tool({"task": "Add modules.", "new_files": sorted(files), "repair_budget": 0})
        self.assertEqual(result["syntax"], {"ok.mjs": "PASS", "ok.cjs": "PASS", "esm.js": "PASS", "bad.mjs": "FAIL"},
                         result)
        static = [stage for stage in result["stages"] if stage["stage"] == "staticChecks"][0]
        messages = " ".join(finding["message"] for finding in static["findings"])
        self.assertIn("bad.mjs:2", messages)
        self.assertIn("SyntaxError", messages)
        self.assertNotIn("[stdin]", messages)

    def test_long_empty_file_list_stays_within_the_finding_limit(self):
        # A joined path list longer than 2048 used to become TESTS_FINDING_INVALID and block repair.
        names = [f"{'e' * 96}{index:02d}.txt" for index in range(24)]
        self.assertGreater(len(f"Empty proposed files: {', '.join(names)}."), 2048)
        self.models.draft = lambda prompt: {"candidate": {"files": [
            {"path": name, "content": ""} for name in names]}, "note": "Empty draft."}
        self.models.repair = lambda prompt: {"status": "REPAIRED", "candidate": {"files": [
            {"path": name, "content": "filled\n"} for name in names]}, "note": "Filled the files."}
        result = self.run_tool({"task": "Fill the new files.", "new_files": names, "repair_budget": 1})
        self.assertEqual(result["outcome"], "COMPLETED", result)
        self.assertEqual(result["repairAttempts"], 1)
        failed = [stage for stage in result["stages"]
                  if stage.get("stage") == "tests" and stage.get("status") == "FAIL"]
        self.assertEqual(len(failed), 1, result)
        failure = failed[0]["failures"][0]
        self.assertEqual(failure["code"], "EMPTY_FILE")
        self.assertLessEqual(len(failure["message"]), 2048)
        self.assertTrue(failure["message"].startswith("Empty proposed files:"))


if __name__ == "__main__":
    unittest.main()
