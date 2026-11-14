# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Exercise the runner HTTP transport with loopback fakes and no model calls."""
from __future__ import annotations

import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import subprocess
import sys
import threading
import unittest

from support import IsolatedTestCase, node_available

from agiw_mcp import nisi
from agiw_mcp.config import validate_loopback_base_url

MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class _TransportServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host, *, response_bytes=None, stall=False, status=200, redirect_to=None):
        self.address_family = socket.AF_INET6 if host == "::1" else socket.AF_INET
        self.response_bytes = response_bytes
        self.stall = stall
        self.status = status
        self.redirect_to = redirect_to
        self.calls = []
        self.bytes_sent = 0
        self.disconnected = threading.Event()
        super().__init__((host, 0), _Handler)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        operation = body["response_format"]["json_schema"]["name"].replace("nisi_", "")
        self.server.calls.append((operation, body["model"]))
        if self.server.redirect_to is not None:
            self.close_connection = True
            self.send_response(307)
            self.send_header("Location", self.server.redirect_to)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        content = ({"findings": [], "summary": "Inspected the change."} if operation == "review" else
                   {"candidate": {"files": [{"path": "a.py", "content": "value = 1\n"}]}, "note": "edit"})
        payload = json.dumps({"model": body["model"], "choices": [{
            "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(content)},
        }]}).encode()
        if self.server.response_bytes is not None:
            payload += b" " * (self.server.response_bytes - len(payload))
        self.close_connection = True
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        if not self.server.stall:
            self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for offset in range(0, len(payload), 65536):
                part = payload[offset:offset + 65536]
                self.wfile.write(part)
                self.server.bytes_sent += len(part)
            if self.server.stall:
                # No EOF or Content-Length: the runner must stop reading at the cap.
                self.connection.settimeout(5)
                if self.rfile.read(1) == b"":
                    self.server.disconnected.set()
        except (BrokenPipeError, ConnectionResetError):
            self.server.disconnected.set()
        except TimeoutError:
            pass

    def log_message(self, *_args):
        pass


@unittest.skipUnless(node_available(), "Node.js 22+ required")
class LoopbackTransportLimitTests(IsolatedTestCase):
    def serve(self, host="127.0.0.1", **kwargs):
        try:
            server = _TransportServer(host, **kwargs)
        except OSError as error:
            if host == "::1" and error.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.EPROTONOSUPPORT):
                self.skipTest("IPv6 loopback is unavailable")
            raise
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def endpoint(self, server):
        host, port = server.server_address[:2]
        literal = f"[{host}]" if ":" in host else host
        return f"http://{literal}:{port}/v1/chat/completions"

    def run_runner(self, endpoint, *, reviewer_endpoint=None, deadline_ms=3000):
        work = self.tmp / f"run-{len(list(self.tmp.iterdir()))}"
        work.mkdir()
        nisi._copy_verified(nisi.VENDOR_DIR, work)
        node, _version = nisi.find_node()
        request = {"schema": 1, "taskText": "change value", "files": [{"path": "a.py", "content": "value = 0\n"}],
                   "allowedFiles": ["a.py"], "acceptanceCriteria": ["change value"], "assertions": [],
                   "repairBudget": 0, "deadlineMs": deadline_ms, "endpoint": endpoint,
                   "authorModel": "author", "reviewerModel": "reviewer", "maxOutputTokens": 1000,
                   "language": "python", "python": sys.executable, "checkTimeoutMs": 1000}
        if reviewer_endpoint is not None:
            request["reviewerEndpoint"] = reviewer_endpoint
        done = subprocess.run([node, str(work / "runner.mjs")], input=json.dumps(request).encode(),
                              capture_output=True, cwd=work, env=nisi._child_env(node, str(work)), timeout=10)
        self.assertIn(done.returncode, (0, 2), done.stderr.decode())
        return json.loads(done.stdout), done.returncode

    def test_ipv6_loopback_completes_for_author_and_reviewer(self):
        server = self.serve("::1")
        result, code = self.run_runner(self.endpoint(server))
        self.assertEqual(code, 0, result)
        self.assertEqual(result["report"]["outcome"], "COMPLETED", result)
        self.assertEqual(server.calls, [("draft", "author"), ("review", "reviewer")])

    def test_separate_ipv6_reviewer_completes(self):
        author = self.serve()
        reviewer = self.serve("::1")
        result, code = self.run_runner(self.endpoint(author), reviewer_endpoint=self.endpoint(reviewer))
        self.assertEqual(code, 0, result)
        self.assertEqual(result["report"]["outcome"], "COMPLETED", result)
        self.assertEqual(author.calls, [("draft", "author")])
        self.assertEqual(reviewer.calls, [("review", "reviewer")])

    def test_other_literal_destinations_are_refused(self):
        # Documentation addresses and non-admitted literals; validation runs before HTTP.
        for host in ("192.0.2.1", "[2001:db8::1]", "127.0.0.2", "[::2]", "[::ffff:127.0.0.1]", "0.0.0.0", "[::]"):
            with self.subTest(host=host):
                result, code = self.run_runner(f"http://{host}:9/v1/chat/completions")
                self.assertEqual(code, 2, result)
                self.assertEqual(result["error"], "LOCAL_CHAT_DESTINATION_REFUSED", result)

    def test_non_canonical_loopback_spellings_are_refused(self):
        # WHATWG URL parsing canonicalizes each of these to an accepted literal. Configuration
        # rejects them, so the runner must too, before any request reaches the server.
        v4 = self.serve()
        port = v4.server_address[1]
        hosts = ["2130706433", "0x7f000001", "0x7f.1", "0177.0.0.1", "127.1", "127.0.0.1.", "127.0.0.01",
                 "\uff11\uff12\uff17.0.0.1", "127\u30020\u30020\u30021", "@127.0.0.1", "user@127.0.0.1",
                 "user:secret@127.0.0.1", "localhost", "LOCALHOST", "localhost."]
        for host in hosts:
            with self.subTest(host=host):
                result, code = self.run_runner(f"http://{host}:{port}/v1/chat/completions")
                self.assertEqual(code, 2, result)
                self.assertEqual(result["error"], "LOCAL_CHAT_DESTINATION_REFUSED", result)
        for endpoint in (f"https://127.0.0.1:{port}/v1/chat/completions", f"http:127.0.0.1:{port}/v1/chat/completions",
                         f"http:\\\\127.0.0.1:{port}/v1/chat/completions", f" http://127.0.0.1:{port}/v1/chat/completions",
                         f"http://127.0.0.1:{port}/v1/chat\t/completions", f"http://127.0.0.1:{port}/v1/chat/completions\n",
                         f"http://127.0.0.1:{port}/v1/chat/completions?", f"http://127.0.0.1:{port}/v1/chat/completions#",
                         f"http://127.0.0.1:{port}"):
            with self.subTest(endpoint=endpoint):
                result, code = self.run_runner(endpoint)
                self.assertEqual(code, 2, result)
                self.assertEqual(result["error"], "LOCAL_CHAT_DESTINATION_REFUSED", result)
        self.assertEqual(v4.calls, [])

    def test_spellings_configuration_accepts_complete(self):
        # validate_loopback_base_url admits an upper-case scheme and a zero-padded port.
        server = self.serve()
        port = server.server_address[1]
        for base in (f"HTTP://127.0.0.1:{port}/v1", f"http://127.0.0.1:0{port}/v1"):
            with self.subTest(base=base):
                endpoint = validate_loopback_base_url(base) + "/chat/completions"
                result, code = self.run_runner(endpoint)
                self.assertEqual(code, 0, result)
                self.assertEqual(result["report"]["outcome"], "COMPLETED", result)
        self.assertEqual(len(server.calls), 4)

    def test_out_of_range_ports_are_refused(self):
        # Configuration admits ports 1-65535 only. Port 0 parses under WHATWG URL rules and
        # out-of-range ports must be refused as a destination before any request is made.
        v4 = self.serve()
        v6 = self.serve("::1")
        for host in ("127.0.0.1", "[::1]"):
            for port in ("0", "00", "000000", "65536", "99999", "4294967297", "1" * 40):
                with self.subTest(host=host, port=port):
                    result, code = self.run_runner(f"http://{host}:{port}/v1/chat/completions")
                    self.assertEqual(code, 2, result)
                    self.assertEqual(result["error"], "LOCAL_CHAT_DESTINATION_REFUSED", result)
        self.assertEqual(v4.calls, [])
        self.assertEqual(v6.calls, [])

    def test_out_of_range_reviewer_port_is_refused(self):
        server = self.serve()
        for port in ("0", "65536"):
            with self.subTest(port=port):
                result, code = self.run_runner(self.endpoint(server),
                                               reviewer_endpoint=f"http://127.0.0.1:{port}/v1/chat/completions")
                self.assertEqual(code, 2, result)
                self.assertEqual(result["error"], "LOCAL_CHAT_DESTINATION_REFUSED", result)
        self.assertEqual(server.calls, [])

    def test_port_range_boundaries_pass_validation(self):
        # Ports 1 and 65535 (also zero-padded) are admitted. No fake listens there, so the run
        # fails later at connect, but not as a refused destination or an invalid endpoint.
        for port in ("1", "65535", "065535"):
            with self.subTest(port=port):
                result, _code = self.run_runner(f"http://127.0.0.1:{port}/v1/chat/completions", deadline_ms=1000)
                self.assertNotEqual(result.get("error"), "LOCAL_CHAT_DESTINATION_REFUSED", result)
                self.assertNotEqual(result.get("error"), "LOCAL_CHAT_ENDPOINT_INVALID", result)

    def test_configuration_and_runner_agree_on_port_range(self):
        for port in ("0", "65536", "99999"):
            with self.subTest(port=port):
                with self.assertRaises(ValueError):
                    validate_loopback_base_url(f"http://127.0.0.1:{port}/v1")
        for port in ("1", "65535", "065535"):
            with self.subTest(port=port):
                self.assertEqual(validate_loopback_base_url(f"http://127.0.0.1:{port}/v1"),
                                 f"http://127.0.0.1:{port}/v1")

    def test_non_canonical_ipv6_loopback_spellings_are_refused(self):
        v6 = self.serve("::1")
        port = v6.server_address[1]
        for host in ("[0:0:0:0:0:0:0:1]", "[::0001]", "[0::1]", "[::1%25lo0]", "[::1%lo0]", "[::ffff:7f00:1]", "::1"):
            with self.subTest(host=host):
                result, code = self.run_runner(f"http://{host}:{port}/v1/chat/completions")
                self.assertEqual(code, 2, result)
                self.assertEqual(result["error"], "LOCAL_CHAT_DESTINATION_REFUSED", result)
        self.assertEqual(v6.calls, [])

    def test_non_canonical_reviewer_endpoint_is_refused(self):
        server = self.serve()
        port = server.server_address[1]
        for reviewer in (f"http://127.1:{port}/v1/chat/completions", f"http://127.0.0.1:{port}/v1/chat/completions?"):
            with self.subTest(reviewer=reviewer):
                result, code = self.run_runner(self.endpoint(server), reviewer_endpoint=reviewer)
                self.assertEqual(code, 2, result)
                self.assertEqual(result["error"], "LOCAL_CHAT_DESTINATION_REFUSED", result)
        self.assertEqual(server.calls, [])

    def test_redirect_is_not_followed(self):
        target = self.serve()
        redirector = self.serve(redirect_to=self.endpoint(target))
        result, _code = self.run_runner(self.endpoint(redirector))
        self.assertNotEqual(result.get("report", {}).get("outcome"), "COMPLETED", result)
        self.assertEqual(len(redirector.calls), 1)
        self.assertEqual(target.calls, [])

    def test_exact_response_cap_completes(self):
        server = self.serve(response_bytes=MAX_RESPONSE_BYTES)
        result, code = self.run_runner(self.endpoint(server), deadline_ms=8000)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["report"]["outcome"], "COMPLETED", result)
        self.assertEqual(server.bytes_sent, 2 * MAX_RESPONSE_BYTES)

    def test_over_cap_response_aborts_before_eof(self):
        for status in (200, 503):
            with self.subTest(status=status):
                server = self.serve(response_bytes=MAX_RESPONSE_BYTES + 1, stall=True, status=status)
                result, _code = self.run_runner(self.endpoint(server))
                self.assertEqual(result["receipts"]["author"][0]["code"], "LOCAL_CHAT_RESPONSE_TOO_LARGE", result)
                self.assertTrue(server.disconnected.wait(1), "runner did not close the over-cap response")
                self.assertLessEqual(server.bytes_sent, MAX_RESPONSE_BYTES + 1)
                self.assertEqual(server.calls, [("draft", "author")])
