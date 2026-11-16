# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Exercise live progress through stdio, the Python host, and a fake Node child."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading

from support import ROOT, IsolatedTestCase, subprocess_env, write_fake_node


class _StdioSession:
    def __init__(self, config_dir, path):
        self.frames = queue.Queue()
        self.process = subprocess.Popen(
            [sys.executable, "-B", "-m", "agiw_mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
            env=subprocess_env(config_dir, PATH=path),
        )
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        try:
            for line in self.process.stdout:
                self.frames.put(json.loads(line))
        finally:
            self.frames.put(None)

    def send(self, message):
        self.process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.process.stdin.flush()

    def next(self):
        try:
            return self.frames.get(timeout=2)
        except queue.Empty:
            raise AssertionError("stdio frame did not arrive within two seconds") from None

    def stop(self):
        if not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()


class NisiRpcProgressTests(IsolatedTestCase):
    def session(self):
        marker = self.tmp / "finish-run"
        fake = write_fake_node(
            self.tmp / "bin",
            "import json, pathlib, sys, time\n"
            "if '--version' in sys.argv:\n"
            "    print('v22.13.0'); sys.exit(0)\n"
            "if '--help' in sys.argv:\n"
            "    print('  --permission'); sys.exit(0)\n"
            "request = json.load(sys.stdin)\n"
            "event = {'kind': 'agiw-mcp.nisi-event.v1', 'stage': 'author', 'attempt': 0, 'status': 'started'}\n"
            "print(json.dumps(event), flush=True)\n"
            f"marker = pathlib.Path({str(marker)!r})\n"
            "deadline = time.monotonic() + 5\n"
            "while not marker.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "print(json.dumps(dict(event, status='finished')), flush=True)\n"
            "report = {'outcome': 'COMPLETED', 'code': None, 'candidate': {'files': []},\n"
            "          'repairAttempts': 0, 'stages': [], 'runId': 'r',\n"
            "          'taskFingerprint': 't', 'candidateFingerprint': 'c'}\n"
            "print(json.dumps({'kind': 'agiw-mcp.nisi-run.v1', 'report': report,\n"
            "                  'receipts': {'author': [], 'reviewer': []}, 'syntax': {}}), flush=True)\n",
        )
        self.write_config(author_model="author-model", reviewer_model="reviewer-model")
        session = _StdioSession(self.config_dir, str(fake.parent) + os.pathsep + os.environ.get("PATH", ""))
        self.addCleanup(session.stop)
        session.send(
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        self.assertEqual(session.next()["id"], "init")
        session.send(
            {
                "jsonrpc": "2.0",
                "id": "run",
                "method": "tools/call",
                "params": {
                    "name": "agiw_nisi_run",
                    "arguments": {
                        "task": "Change a value.",
                        "files": [{"path": "answer.py", "content": "answer = 0\n"}],
                    },
                    "_meta": {"progressToken": "live-run"},
                },
            }
        )
        return session, marker

    def assert_started(self, message):
        self.assertEqual(
            message,
            {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progressToken": "live-run", "progress": 1, "message": "author attempt 0 started"},
            },
        )

    def test_live_progress_precedes_result_and_ping_during_child_wait(self):
        session, marker = self.session()
        self.assert_started(session.next())
        self.assertFalse(marker.exists(), "the child must still be waiting when progress arrives")
        session.send({"jsonrpc": "2.0", "id": "ping", "method": "ping"})
        self.assertEqual(session.next(), {"jsonrpc": "2.0", "id": "ping", "result": {}})
        marker.touch()
        finished = session.next()
        self.assertEqual(finished["method"], "notifications/progress")
        self.assertEqual(finished["params"]["progress"], 2)
        self.assertEqual(finished["params"]["message"], "author attempt 0 finished")
        result = session.next()
        self.assertEqual(result["id"], "run")
        self.assertFalse(result["result"]["isError"], result)
        self.assertEqual(result["result"]["structuredContent"]["outcome"], "COMPLETED")
        session.stop()
        self.assertIsNone(session.next(), "progress must not follow the result")

    def test_cancelled_child_run_sends_no_later_progress_or_result(self):
        session, marker = self.session()
        self.assert_started(session.next())
        session.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "run"}})
        session.send({"jsonrpc": "2.0", "id": "cancelled", "method": "ping"})
        self.assertEqual(session.next(), {"jsonrpc": "2.0", "id": "cancelled", "result": {}})
        marker.touch()
        session.stop()
        self.assertIsNone(session.next(), "cancelled run sent progress or a result")
