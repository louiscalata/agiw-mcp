# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Model calls use loopback node:http, not undici's 300s headers timeout."""
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest

from support import IsolatedTestCase, node_available

from agiw_mcp import nisi


class _DelayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, delay):
        self.delay = delay
        super().__init__(("127.0.0.1", 0), _Handler)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        time.sleep(self.server.delay)
        body = json.loads(raw)
        operation = body["response_format"]["json_schema"]["name"].replace("nisi_", "")
        if operation == "review":
            content = {"findings": [], "summary": "Inspected the change."}
        else:
            content = {"candidate": {"files": [{"path": "a.py", "content": "value = 1\n"}]}, "note": "edit"}
        payload = json.dumps({"model": body["model"], "choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(content)},
        }], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
        try:
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, *_args):
        return


@unittest.skipUnless(node_available(), "Node.js 22+ required")
class LoopbackTimeoutTests(IsolatedTestCase):
    def serve(self, delay):
        server = _DelayServer(delay)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server

    def run_runner(self, server, *, deadline_ms, undici_ms):
        work = self.tmp / "run"
        work.mkdir()
        nisi._copy_verified(nisi.VENDOR_DIR, work)
        node, _version = nisi.find_node()
        request = {"schema": 1, "taskText": "change value", "files": [{"path": "a.py", "content": "value = 0\n"}],
                   "allowedFiles": ["a.py"], "acceptanceCriteria": ["change value"], "assertions": [],
                   "repairBudget": 0, "deadlineMs": deadline_ms,
                   "endpoint": f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
                   "authorModel": "author", "reviewerModel": "reviewer", "maxOutputTokens": 1000,
                   "language": "python", "python": sys.executable, "checkTimeoutMs": 1000}
        env = nisi._child_env(node, str(work))
        env["AGIW_MCP_UNDICI_HEADERS_TIMEOUT_MS"] = str(undici_ms)
        done = subprocess.run([node, str(work / "runner.mjs")], input=json.dumps(request).encode(),
                              capture_output=True, cwd=work, env=env, timeout=40)
        self.assertIn(done.returncode, (0, 2), done.stderr.decode())
        return json.loads(done.stdout), done.returncode

    def test_headers_withheld_past_the_configured_timeout_are_a_timeout(self):
        # Headers arrive 1.2s after the 0.4s deadline. Undici used to fail this as an
        # unnamed transport error; the receipt must name the timeout.
        result, _code = self.run_runner(self.serve(1.2), deadline_ms=400, undici_ms=100)
        blob = json.dumps(result)
        self.assertNotIn("LOCAL_CHAT_UNAVAILABLE", blob)
        if result.get("error"):
            self.assertEqual(result["error"], "LOCAL_CHAT_TIMEOUT")
            return
        self.assertNotEqual(result["report"]["outcome"], "COMPLETED")
        codes = [item.get("code") for item in result["receipts"]["author"]]
        self.assertTrue(codes, result)
        self.assertIn(codes[0], ("LOCAL_CHAT_TIMEOUT", "ABORTED"))
        self.assertIn(result["report"]["code"], ("ADAPTER_EXCEPTION", "DEADLINE_EXCEEDED"))

    def test_response_after_the_undici_cap_still_completes(self):
        # 0.6s is longer than the injected undici headers timeout (0.2s) and shorter
        # than deadlineMs. Applying that cap as a headers timeout would fail the run.
        result, code = self.run_runner(self.serve(0.6), deadline_ms=8000, undici_ms=200)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["report"]["outcome"], "COMPLETED", result)
        self.assertNotIn("LOCAL_CHAT_UNAVAILABLE", json.dumps(result))
