# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
import io
import json
import subprocess
import sys
import unittest
from unittest import mock

from support import IsolatedTestCase, ROOT, subprocess_env

from agiw_mcp import rpc


def exchange(*frames: bytes) -> list[dict]:
    stdin = io.BytesIO(b"".join(frames))
    stdout = io.BytesIO()
    rpc.serve(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def frame(message) -> bytes:
    return json.dumps(message).encode() + b"\n"


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}


class RpcTests(IsolatedTestCase):
    def test_encode_nonserializable_and_nan_results(self):
        for value in (object(), float("nan")):
            with self.subTest(value=type(value).__name__):
                raw = rpc.encode({"jsonrpc": "2.0", "id": "request-id", "result": value})
                self.assertTrue(raw.endswith(b"\n"))
                self.assertEqual(json.loads(raw), {"jsonrpc": "2.0", "id": "request-id",
                                                  "error": {"code": -32603, "message": "Internal error"}})

    def test_encode_oversized_response(self):
        with mock.patch.object(rpc, "MAX_OUTPUT_BYTES", 256):
            raw = rpc.encode({"jsonrpc": "2.0", "id": 7, "result": "x" * 257})
        self.assertLess(len(raw), 256)
        self.assertEqual(json.loads(raw), {"jsonrpc": "2.0", "id": 7,
                                          "error": {"code": -32603, "message": "Response too large"}})

    def test_initialize_list_and_call(self):
        replies = exchange(frame(INIT), frame({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                           frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                           frame({"jsonrpc": "2.0", "id": "c", "method": "tools/call",
                                  "params": {"name": "agiw_consent", "arguments": {"action": "show"}}}))
        self.assertEqual(len(replies), 3)
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "agiw-mcp")
        names = [tool["name"] for tool in replies[1]["result"]["tools"]]
        self.assertEqual(names, ["agiw_readiness", "agiw_status", "agiw_consent", "agiw_jev_pre", "agiw_jev_post",
                                 "agiw_nisi_run"])
        for tool in replies[1]["result"]["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            self.assertTrue(tool["description"])
        call = replies[2]
        self.assertEqual(call["id"], "c")
        self.assertFalse(call["result"]["isError"])
        self.assertEqual(json.loads(call["result"]["content"][0]["text"]), call["result"]["structuredContent"])

    def test_unknown_protocol_version_gets_latest(self):
        message = dict(INIT, params={"protocolVersion": "1999-01-01"})
        reply = exchange(frame(message))[0]
        self.assertEqual(reply["result"]["protocolVersion"], rpc.SUPPORTED_PROTOCOLS[0])

    def test_error_codes(self):
        replies = exchange(b"{not json\n",
                           frame([INIT]),
                           frame({"jsonrpc": "2.0", "id": 3, "method": "nope"}),
                           frame({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                  "params": {"name": "missing_tool", "arguments": {}}}),
                           frame({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": 7}}),
                           frame({"jsonrpc": "2.0", "id": True, "method": "ping"}),
                           frame({"jsonrpc": "1.0", "id": 6, "method": "ping"}),
                           b'{"jsonrpc":"2.0","id":7,"id":8,"method":"ping"}\n',
                           frame({"jsonrpc": "2.0", "id": 9, "method": "ping"}))
        codes = [reply.get("error", {}).get("code") for reply in replies]
        self.assertEqual(codes, [-32700, -32600, -32601, -32602, -32602, -32600, -32600, -32700, None])
        self.assertEqual(replies[-1], {"jsonrpc": "2.0", "id": 9, "result": {}})

    def test_tool_errors_are_results_not_protocol_errors(self):
        reply = exchange(frame({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "agiw_readiness", "arguments": {"bogus": 1}}}))[0]
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(reply["result"]["structuredContent"]["error"]["code"], "INPUT_INVALID")

    def test_oversized_frame_is_discarded_and_stream_stays_aligned(self):
        big = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"' + b"a" * (rpc.MAX_LINE_BYTES + 10) + b'"}}\n'
        replies = exchange(big, frame({"jsonrpc": "2.0", "id": 2, "method": "ping"}))
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": 2, "result": {}})

    def test_internal_errors_hide_details(self):
        from unittest import mock
        with mock.patch.object(rpc, "call_tool", side_effect=RuntimeError("secret detail")):
            reply = exchange(frame({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "agiw_status", "arguments": {}}}))[0]
        self.assertEqual(reply["error"], {"code": -32603, "message": "Internal error"})

    def test_stdio_subprocess_smoke(self):
        frames = frame(INIT) + frame({"jsonrpc": "2.0", "method": "notifications/initialized"}) + \
            frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + \
            frame({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "agiw_readiness", "arguments": {}}})
        done = subprocess.run([sys.executable, "-B", "-m", "agiw_mcp"], input=frames, capture_output=True,
                              timeout=60, cwd=str(ROOT), env=subprocess_env(self.config_dir))
        self.assertEqual(done.returncode, 0, done.stderr)
        replies = [json.loads(line) for line in done.stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])
        readiness = replies[2]["result"]["structuredContent"]
        self.assertIn(readiness["status"], ("READY", "NOT_READY"))
        self.assertNotIn(b"Traceback", done.stderr)


if __name__ == "__main__":
    unittest.main()
