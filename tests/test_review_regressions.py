# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Review regressions; no listening sockets or real model calls."""
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock

from support import IsolatedTestCase, node_available
from agiw_mcp import __main__ as cli, config, consent, jev, net, nisi, readiness, tickets, tools
from agiw_mcp.errors import ToolError


class ReviewRegressions(IsolatedTestCase):
    @unittest.skipIf(sys.platform == "win32", "this check uses a Unix socket pair")
    def test_http_deadline_closes_headers_and_body_sockets(self):
        import socket
        for headers in (False, True):
            with self.subTest(headers=headers):
                client, peer = socket.socketpair()
                closed = threading.Event()
                def respond(peer=peer, headers=headers, closed=closed):
                    request = b""
                    try:
                        while b"\r\n\r\n" not in request:
                            chunk = peer.recv(4096)
                            if not chunk:
                                return
                            request += chunk
                        if headers:
                            peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4096\r\n\r\n ")
                        if peer.recv(1) == b"":
                            closed.set()
                    except OSError:
                        pass
                worker = threading.Thread(target=respond, daemon=True)
                worker.start()
                try:
                    # Unix socket pairs have no TCP_NODELAY option; no listening service is needed.
                    with mock.patch.object(socket, "create_connection", return_value=client), \
                            mock.patch.object(socket.socket, "setsockopt"):
                        with self.assertRaises(net.HttpFailure):
                            net.request_json("http://127.0.0.1/test", timeout=0.05, max_bytes=8192)
                    self.assertTrue(closed.wait(0.3), "HTTP socket survived the deadline")
                finally:
                    try:
                        client.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    client.close()
                    peer.close()
                    worker.join(1)
                    for thread in threading.enumerate():
                        if thread.name == "agiw-mcp-http":
                            thread.join(1)

    def test_jev_failure_does_not_deny_possible_delivery(self):
        self.grant_consent()
        with mock.patch.object(jev.keys, "read_key", return_value="synthetic-test-key"), \
                mock.patch.object(jev, "request_json", side_effect=net.HttpFailure("timeout")):
            with self.assertRaises(ToolError) as caught:
                jev.analyse("pre", "task", config.Config())
        self.assertIn("may have been sent", caught.exception.message)

    def test_jev_busy_reports_nothing_sent(self):
        self.grant_consent()
        with mock.patch.object(jev.keys, "read_key", return_value="synthetic-test-key"), \
                mock.patch.object(jev, "request_json", side_effect=net.HttpFailure("busy")):
            with self.assertRaises(ToolError) as caught:
                jev.analyse("pre", "task", config.Config())
        self.assertEqual(caught.exception.code, "JEV_BUSY")
        self.assertIn("nothing was sent", caught.exception.message)
        self.assertNotIn("may have been sent", caught.exception.message)

    def test_readiness_busy_does_not_blame_endpoint(self):
        with mock.patch.object(readiness, "request_json", side_effect=net.HttpFailure("busy")):
            listed, failed = readiness._list_models("http://127.0.0.1:1234/v1/models", "model_base_url")
        self.assertIsNone(listed)
        self.assertIn("busy", failed["detail"])
        self.assertIn("nothing was sent", failed["detail"])
        self.assertNotIn("not reachable", failed["detail"])
        self.assertIn("Wait", failed["fix"])

    def test_nisi_rejects_paths_that_cannot_have_ticket_scopes(self):
        for part in ("calc.py.", "nul.txt", "AUX.txt", "CON", "CoM1.py", "lpt9.txt"):
            for path in (part, "src/" + part, part + "/calc.py"):
                for field in ("files", "new_files"):
                    args = {"task": "edit", "new_files": ["valid.py"]}
                    args[field] = ([{"path": path, "content": "x"}] if field == "files" else
                                   [path])
                    with self.subTest(path=path, field=field):
                        with self.assertRaises(ToolError) as caught:
                            nisi.validate_request(args)
                        self.assertEqual(caught.exception.code, "INPUT_INVALID")
        for path in (".env", "src/calc.py", "null.txt", "auxiliary.txt", "com10.py"):
            with self.subTest(valid=path):
                self.assertTrue(nisi._path_ok(path))

    def test_python_checker_version_is_reported(self):
        request = nisi.validate_request({"task": "edit", "new_files": ["a.py"]})
        result = nisi._summarize({"report": {"outcome": "FAILED"}}, request, "none")
        self.assertEqual(result.get("pythonCheckerVersion"), sys.version.split()[0])

    def test_ticket_scope_contains_existing_and_new_paths(self):
        cfg = config.Config(author_model="author", reviewer_model="reviewer")
        ticket = {"ticketId": "scope-test", "claimState": "Granted", "owner": "writer", "writeScope": ["src"]}
        for path, field in (("docs/a.py", "files"), ("srcx/a.py", "new_files")):
            args = {"task": "edit", "ticket": ticket, field: [{"path": path, "content": "x"}]
                    if field == "files" else [path]}
            with self.subTest(path=path), mock.patch.object(nisi, "_run_validated") as run, \
                    mock.patch.object(tickets, "registry") as registry:
                with self.assertRaises(ToolError) as caught:
                    nisi.run(args, cfg)
                self.assertEqual(caught.exception.code, "TICKET_SCOPE_MISMATCH")
                registry.assert_not_called()
                run.assert_not_called()
        with mock.patch.object(nisi, "_run_validated", return_value={}), mock.patch.object(tickets, "registry"):
            nisi.run({"task": "edit", "new_files": ["SRC/a.py"], "ticket": ticket}, cfg)

    def test_successful_child_is_not_killed_after_reaping(self):
        with mock.patch.object(nisi, "_kill_tree") as kill:
            result = nisi._bounded_run([sys.executable, "-I", "-S", "-B", "-c", "print('ok')"],
                                       data=b"", cwd=str(self.tmp), env={}, timeout=3)
        self.assertEqual(result[0], 0)
        self.assertEqual(result[2], "exited")
        self.assertEqual(result[1].replace(b"\r\n", b"\n"), b"ok\n")
        kill.assert_not_called()

    def test_consent_prompt_eof_and_interrupt_record_nothing(self):
        for error in (EOFError, KeyboardInterrupt):
            with self.subTest(error=error.__name__), mock.patch("sys.stdin.isatty", return_value=True), \
                    mock.patch("builtins.input", side_effect=error), mock.patch("sys.stdout", io.StringIO()):
                try:
                    result = cli.main(["consent", "grant"])
                except (EOFError, KeyboardInterrupt) as exc:
                    self.fail(f"Prompt leaked {type(exc).__name__}")
                self.assertEqual(result, 1)
                self.assertFalse(consent.consent_recorded())

    def test_relative_config_has_useful_status_and_readiness(self):
        with mock.patch.dict(os.environ, {"AGIW_MCP_CONFIG_DIR": "relative"}), \
                mock.patch.object(nisi, "find_node", return_value=(None, None)):
            result = readiness.evaluate()
            self.assertIn("absolute path", result["missing"]["detail"])
            result = tools.status_tool({})
            self.assertIn("absolute path", result["configError"])
            self.assertIsNone(result["configDir"])

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_new_config_ancestors_are_private(self):
        parent = self.tmp / "new-parent"
        old = os.umask(0o002)
        try:
            config.ensure_private_dir(parent / "config")
        finally:
            os.umask(old)
        self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((parent / "config").stat().st_mode & 0o777, 0o700)

    def test_nisi_annotation_reports_side_effects(self):
        annotation = next(t for t in tools.TOOLS if t["name"] == "agiw_nisi_run")["annotations"]
        self.assertFalse(annotation["readOnlyHint"])
        self.assertFalse(annotation["destructiveHint"])

    def test_local_readiness_does_not_require_jev(self):
        self.write_config(author_model="author", reviewer_model="reviewer")
        with mock.patch.object(readiness, "check_node", return_value={"id": "node", "ok": True}), \
                mock.patch.object(readiness, "check_models", return_value={"id": "models", "ok": True}), \
                mock.patch.object(readiness.keys, "key_presence") as key:
            result = readiness.evaluate()
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["jev"]["status"], "NOT_READY")
        self.assertNotIn("missing", result)
        key.assert_not_called()

    def test_http_timeout_workers_are_bounded(self):
        release = threading.Event()
        entered = []
        class Response(io.BytesIO):
            status = 200
            def read(self, size=-1):
                entered.append(1)
                release.wait(2)
                return b"{}"
        try:
            with mock.patch.object(net, "_opener") as opener:
                opener.return_value.open.side_effect = lambda *a, **kw: Response()
                for _ in range(6):
                    with self.assertRaises(net.HttpFailure):
                        net.request_json("http://127.0.0.1/test", timeout=0.01, max_bytes=100)
            self.assertLessEqual(len(entered), 2)
        finally:
            release.set()
            for thread in threading.enumerate():
                if thread.name == "agiw-mcp-http":
                    thread.join(3)


@unittest.skipUnless(node_available(), "Node 22+ required")
class RunnerRegressions(IsolatedTestCase):
    def runner(self, candidate="value = 1\n", **changes):
        work = self.tmp / ("runner-" + str(len(list(self.tmp.iterdir()))))
        work.mkdir()
        nisi._copy_verified(nisi.VENDOR_DIR, work)
        request = {"schema": 1, "taskText": "change value", "files": [{"path": "a.py", "content": "value = 0\n"}],
                   "allowedFiles": ["a.py"], "acceptanceCriteria": ["change value"], "assertions": [],
                   "repairBudget": 0, "deadlineMs": 5000, "endpoint": "http://127.0.0.1:1234/v1/chat/completions",
                   "authorModel": "author", "reviewerModel": "reviewer", "maxOutputTokens": 1000,
                   "reasoningEffort": "none", "language": "python", "python": sys.executable, "checkTimeoutMs": 1000}
        request.update(changes)
        harness = '''
import { createHash } from 'node:crypto';
const hashes = [];
globalThis.fetch = async (_url, init) => {
  hashes.push(createHash('sha256').update(init.body).digest('hex'));
  const body = JSON.parse(init.body);
  const answer = body.model === 'author'
    ? { candidate: { files: [{ path: 'a.py', content: CANDIDATE }] }, note: 'edit' }
    : { findings: [], summary: 'reviewed' };
  return new Response(JSON.stringify({model: body.model,
    choices: [{finish_reason: 'stop', message: {role: 'assistant', content: JSON.stringify(answer)}}],
    usage: {prompt_tokens: 10, completion_tokens: 5, total_tokens: 15}}));
};
const write = process.stdout.write.bind(process.stdout);
process.stdout.write = raw => write(JSON.stringify({...JSON.parse(raw), wireHashes: hashes}));
await import(RUNNER);
'''.replace("CANDIDATE", json.dumps(candidate)).replace("RUNNER", json.dumps((work / "runner.mjs").as_uri()))
        done = subprocess.run([nisi.find_node()[0], "--input-type=module", "-e", harness],
                              input=json.dumps(request).encode(), capture_output=True, cwd=work,
                              env=nisi._child_env(nisi.find_node()[0], str(work)), timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        return json.loads(done.stdout)

    def test_python_compiler_rejects_module_control_statements(self):
        for candidate in ("return 1\n", "break\n", "nonlocal missing\n"):
            with self.subTest(candidate=candidate):
                result = self.runner(candidate)
                self.assertNotEqual(result["report"]["outcome"], "COMPLETED")
                self.assertEqual(result["syntax"]["a.py"], "FAIL")

    def test_receipt_hashes_match_transmitted_bodies(self):
        result = self.runner()
        self.assertEqual(result["report"]["outcome"], "COMPLETED")
        receipts = result["receipts"]["author"] + result["receipts"]["reviewer"]
        self.assertEqual([r["requestSha256"] for r in receipts], result["wireHashes"])

    def test_input_fingerprint_binds_task_contents_assertions_and_options(self):
        first = self.runner()
        fingerprint = first.get("inputFingerprint")
        self.assertIsInstance(fingerprint, str)
        self.assertEqual(len(fingerprint), 64)
        self.assertEqual(self.runner()["inputFingerprint"], fingerprint)
        for changed in ({"taskText": "different"}, {"files": [{"path": "a.py", "content": "value = 99"}]},
                        {"assertions": [{"path": "a.py", "contains": "value"}]}, {"reasoningEffort": "low"}):
            result = self.runner(**changed)
            self.assertNotEqual(result["inputFingerprint"], fingerprint)
            for receipt in result["receipts"]["author"] + result["receipts"]["reviewer"]:
                self.assertEqual(receipt["inputFingerprint"], result["inputFingerprint"])


class LifecycleRegressions(IsolatedTestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX signals")
    def test_termination_cleans_runner_directory_and_ticket(self):
        import signal
        import time
        from support import subprocess_env
        child = "import os, pathlib, time; pathlib.Path('child-pid').write_text(str(os.getpid())); time.sleep(60)"
        self.write_config(author_model="author", reviewer_model="reviewer")
        script = '''
import sys
from pathlib import Path
from agiw_mcp import __main__, nisi
nisi.find_node = lambda: (sys.executable, (22, 0, 0))
nisi.permission_flags = lambda *args: ([], 'none')
nisi._copy_verified = lambda source, work: (work / 'runner.mjs').write_text(CHILD)
nisi.tempfile.tempdir = PARENT
sys.exit(__main__.main([]))
'''.replace("CHILD", repr(child)).replace("PARENT", repr(str(self.tmp)))
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                proc = subprocess.Popen([sys.executable, "-B", "-c", script],
                                        env=subprocess_env(self.config_dir, PYTHONPATH=str(nisi.PACKAGE_DIR.parent)),
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, stdin=subprocess.PIPE)
                request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": "agiw_nisi_run", "arguments": {"task": "edit", "new_files": ["a.py"],
                    "ticket": {"ticketId": "shutdown", "claimState": "Granted",
                               "writeScope": ["a.py"], "owner": "writer"}}}}
                proc.stdin.write(json.dumps(request).encode() + b"\n")
                proc.stdin.flush()
                pid = None
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        markers = list(self.tmp.glob("agiw-mcp-nisi-*/child-pid"))
                        if markers:
                            pid = int(markers[0].read_text())
                            break
                        if proc.poll() is not None:
                            self.fail("Server exited before runner started")
                        time.sleep(0.02)
                    self.assertIsNotNone(pid)
                    proc.send_signal(sig)
                    proc.communicate(timeout=8)
                    self.assertEqual(list(self.tmp.glob("agiw-mcp-nisi-*")), [])
                    self.assertEqual(list(self.config_dir.glob("tickets/scope-*.lock")), [])
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.communicate(timeout=8)
                    if pid is not None:
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    # Clean the deliberately reproduced pre-fix leftovers before the next case.
                    import shutil
                    for path in self.tmp.glob("agiw-mcp-nisi-*"):
                        shutil.rmtree(path)
                    for path in self.config_dir.glob("tickets/scope-*.lock"):
                        path.unlink()

    @unittest.skipIf(
        sys.platform == "win32",
        "ticket admission is refused on Windows before the ticket directory is touched",
    )
    def test_ticket_lock_records_owner(self):
        registry = tickets.Registry(self.tmp / "tickets")
        registry.admit({"ticketId": "owner", "claimState": "Granted", "writeScope": ["src"], "owner": "writer"})
        try:
            record = json.loads(next((self.tmp / "tickets").glob("scope-*.lock")).read_text())
            self.assertEqual(record.get("owner"), "writer")
        finally:
            registry.release("owner")


class CancellationRegressions(IsolatedTestCase):
    def test_buffered_cancellation_after_nisi_done(self):
        self.exercise_pending("done", "cancel")

    def test_buffered_eof_after_nisi_done(self):
        self.exercise_pending("done", "eof")

    def test_buffered_cancellation_between_sync_calls(self):
        self.exercise_pending("sync", "cancel")

    def test_buffered_eof_between_sync_calls(self):
        self.exercise_pending("sync", "eof")

    def test_pending_calls_keep_fifo_order(self):
        self.exercise_pending("last", "eof")

    def test_buffered_call_does_not_overtake_pending_calls(self):
        self.exercise_pending("done", "call")

    def exercise_pending(self, boundary, stop):
        import queue
        from agiw_mcp import rpc
        class InputQueue(queue.Queue):
            done_consumed = False

            def get(self, block=True, timeout=None):
                # End input only after the completion and all buffered/pending work are consumed.
                if block and self.done_consumed and self.empty():
                    return "frame", None
                event = super().get(block=block, timeout=timeout)
                if event[0] == "done":
                    self.done_consumed = True
                return event

        events = InputQueue(maxsize=16)
        calls = []
        replies = []
        target = 2 if boundary == "done" else 3
        real_thread = threading.Thread

        def tool(name, args):
            calls.append((name, args["marker"]))
            return {}, False

        class Output:
            def write(self, data):
                reply = json.loads(data)
                replies.append(reply["id"])
                trigger = {"done": 1, "sync": 2, "last": 3}[boundary]
                if reply["id"] == trigger:
                    if stop == "cancel":
                        cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled",
                                  "params": {"requestId": target}}
                        events.put(("frame", json.dumps(cancel).encode()))
                    elif stop == "call":
                        request = {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                   "params": {"name": "agiw_jev_pre", "arguments": {"marker": 4}}}
                        events.put(("frame", json.dumps(request).encode()))
                    else:
                        events.put(("frame", None))
            def flush(self):
                pass

        def thread(*args, **kwargs):
            # Feed the input queue explicitly; only the Nisi worker runs in a real thread.
            if kwargs.get("name") == "agiw-mcp-stdin":
                return mock.Mock()
            return real_thread(*args, **kwargs)

        for identifier, name in ((1, "agiw_nisi_run"), (2, "agiw_jev_pre"), (3, "agiw_jev_pre")):
            request = {"jsonrpc": "2.0", "id": identifier, "method": "tools/call",
                       "params": {"name": name, "arguments": {"marker": identifier}}}
            events.put(("frame", json.dumps(request).encode()))
        with mock.patch.object(rpc.queue, "Queue", return_value=events), \
                mock.patch.object(rpc.threading, "Thread", side_effect=thread), \
                mock.patch.object(rpc, "call_tool", side_effect=tool):
            self.assertEqual(rpc.serve(io.BytesIO(), Output()), 0)
        if stop == "call":
            expected = [1, 2, 3, 4]
        elif stop == "cancel":
            expected = [1, 3] if boundary == "done" else [1, 2]
        else:
            expected = [1, 2, 3] if boundary == "last" else [1] if boundary == "done" else [1, 2]
        self.assertEqual([marker for _, marker in calls], expected)
        self.assertEqual(replies, expected)

    def test_ping_and_cancellation_during_nisi(self):
        self.exercise_stream(cancel=True)

    def test_eof_cancels_active_nisi(self):
        self.exercise_stream(cancel=False)

    def exercise_stream(self, cancel):
        import queue
        from agiw_mcp import rpc
        entered = threading.Event()
        finished = threading.Event()
        replies = queue.Queue()
        class Output:
            def write(self, data):
                replies.put(json.loads(data))
            def flush(self):
                pass
        def tool(_name, _args):
            entered.set()
            try:
                nisi._bounded_run([sys.executable, "-I", "-S", "-B", "-c", "import time; time.sleep(2)"],
                                  data=b"", cwd=str(self.tmp), env={}, timeout=5)
                return {}, False
            finally:
                finished.set()
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb")
        writer = os.fdopen(write_fd, "wb", buffering=0)
        server = threading.Thread(target=rpc.serve, args=(reader, Output()), daemon=True)
        def send(value):
            writer.write(json.dumps(value).encode() + b"\n")
        with mock.patch.object(rpc, "call_tool", side_effect=tool):
            server.start()
            try:
                send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "agiw_nisi_run"}})
                self.assertTrue(entered.wait(2))
                if cancel:
                    send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
                    try:
                        reply = replies.get(timeout=0.5)
                    except queue.Empty:
                        self.fail("ping blocked behind Nisi")
                    self.assertEqual(reply["id"], 2)
                    send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
                else:
                    writer.close()
                self.assertTrue(finished.wait(0.5), "Nisi survived cancellation/EOF")
                self.assertTrue(replies.empty(), "cancelled Nisi sent a reply")
            finally:
                writer.close()
                server.join(6)
                reader.close()
                self.assertFalse(server.is_alive())
