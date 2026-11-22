# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Modern protocol 2026-07-28 requests are answered statelessly beside the legacy handshake."""

from __future__ import annotations

import io
import json
import queue
import threading
import unittest
from unittest import mock

from support import IsolatedTestCase

from agiw_mcp import nisi, rpc

MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "0"},
}
SERVER_INFO = {"io.modelcontextprotocol/serverInfo": {"name": rpc.SERVER_NAME, "version": rpc.__version__}}
TOOL_NAMES = [
    "agiw_readiness",
    "agiw_status",
    "agiw_consent",
    "agiw_jev_pre",
    "agiw_jev_post",
    "agiw_nisi_run",
]
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}
EVENT = {"kind": "agiw-mcp.nisi-event.v1", "stage": "author", "attempt": 0, "status": "started"}


def exchange(*frames: bytes) -> list[dict]:
    stdin = io.BytesIO(b"".join(frames))
    stdout = io.BytesIO()
    rpc.serve(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def frame(message) -> bytes:
    return json.dumps(message).encode() + b"\n"


def request(method: str, params: dict, identifier=1) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}


def modern(method: str, params: dict | None = None, identifier=1) -> dict:
    body = dict(params or {})
    body["_meta"] = dict(MODERN_META)
    return request(method, body, identifier)


class RpcModernTests(IsolatedTestCase):
    def test_modern_requests_need_protocol_version_and_client_capabilities(self):
        cases = [
            {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
            {"io.modelcontextprotocol/protocolVersion": None, "io.modelcontextprotocol/clientCapabilities": {}},
            {
                "io.modelcontextprotocol/protocolVersion": 20260728,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
            {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": [],
            },
        ]
        for meta in cases:
            with self.subTest(meta=meta):
                reply = exchange(frame(request("tools/list", {"_meta": meta})))[0]
                self.assertEqual(reply["error"], {"code": -32602, "message": "Invalid params"})

    def test_unsupported_modern_version_lists_supported_and_requested(self):
        self.assertEqual(rpc.MODERN_PROTOCOLS, ("2026-07-28",))
        meta = dict(MODERN_META, **{"io.modelcontextprotocol/protocolVersion": "1900-01-01"})
        cases = (
            ("server/discover", {}),
            ("server/discover", {"cursor": "next"}),
            ("tools/list", {}),
            ("tools/list", {"cursor": "next"}),
            ("tools/call", {"arguments": []}),
            ("ping", {}),
            ("unknown", {}),
        )
        for method, params in cases:
            with self.subTest(method=method, params=params):
                reply = exchange(frame(request(method, {**params, "_meta": meta})))[0]
                self.assertEqual(
                    reply["error"],
                    {
                        "code": -32022,
                        "message": "Unsupported protocol version",
                        "data": {"supported": ["2026-07-28"], "requested": "1900-01-01"},
                    },
                )

    def test_discover_result_fields(self):
        self.assertEqual(rpc.TTL_MS, 3600000)
        reply = exchange(frame(modern("server/discover", identifier="discover")))[0]
        self.assertEqual(reply["id"], "discover")
        result = reply["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["supportedVersions"], ["2026-07-28"])
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertEqual(result["instructions"], rpc.INSTRUCTIONS)
        self.assertIs(type(result["ttlMs"]), int)
        self.assertEqual(result["ttlMs"], rpc.TTL_MS)
        self.assertEqual(result["cacheScope"], "public")
        self.assertEqual(result["_meta"], SERVER_INFO)

    def test_discover_without_modern_meta_is_invalid_params(self):
        replies = exchange(
            frame({"jsonrpc": "2.0", "id": 1, "method": "server/discover"}),
            frame(request("server/discover", {"_meta": {"progressToken": 1}}, identifier=2)),
        )
        for reply in replies:
            self.assertEqual(reply["error"], {"code": -32602, "message": "Invalid params"})

    def test_modern_tools_list_and_call_carry_modern_fields(self):
        replies = exchange(
            frame(modern("tools/list", identifier=1)),
            frame(modern("tools/call", {"name": "agiw_consent", "arguments": {"action": "show"}}, identifier=2)),
        )
        listed, called = replies[0]["result"], replies[1]["result"]
        for result in (listed, called):
            self.assertEqual(result["resultType"], "complete")
            self.assertEqual(result["_meta"], SERVER_INFO)
        self.assertEqual(listed["ttlMs"], rpc.TTL_MS)
        self.assertEqual(listed["cacheScope"], "public")
        self.assertNotIn("ttlMs", called)
        self.assertNotIn("cacheScope", called)
        self.assertEqual([tool["name"] for tool in listed["tools"]], TOOL_NAMES)
        self.assertFalse(called["isError"])
        self.assertEqual(json.loads(called["content"][0]["text"]), called["structuredContent"])

    def test_modern_tools_list_keeps_the_legacy_cursor_rule(self):
        for cursor in ("next", "", 0, None):
            with self.subTest(cursor=cursor):
                reply = exchange(frame(modern("tools/list", {"cursor": cursor})))[0]
                self.assertEqual(reply["error"]["code"], -32602)
                self.assertIsNone(reply["error"].get("data"))

    def test_modern_unknown_tool_is_still_invalid_params(self):
        reply = exchange(frame(modern("tools/call", {"name": "missing_tool", "arguments": {}})))[0]
        self.assertEqual(reply["error"], {"code": -32602, "message": "Unknown tool: missing_tool"})

    def test_legacy_frames_have_none_of_the_modern_fields(self):
        replies = exchange(
            frame(INIT),
            frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            frame(request("tools/call", {"name": "agiw_consent", "arguments": {"action": "show"}}, identifier=3)),
        )
        for reply in replies:
            self.assertNotIn("resultType", reply["result"])
            self.assertNotIn("_meta", reply["result"])
        self.assertEqual(set(replies[1]["result"]), {"tools"})
        self.assertEqual(set(replies[2]["result"]), {"content", "structuredContent", "isError"})

    def test_other_modern_meta_keys_without_protocol_version_stay_legacy(self):
        related = {"io.modelcontextprotocol/related-task": {"taskId": "task-1"}}
        caps_only = {"io.modelcontextprotocol/clientCapabilities": {}}
        replies = exchange(
            frame(INIT),
            frame(
                request(
                    "tools/call",
                    {"name": "agiw_consent", "arguments": {"action": "show"}, "_meta": related},
                    identifier=2,
                )
            ),
            frame(request("tools/list", {"_meta": caps_only}, identifier=3)),
            frame(request("ping", {"_meta": related}, identifier=4)),
        )
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3, 4])
        for reply in replies:
            self.assertNotIn("error", reply)
        called, listed = replies[1]["result"], replies[2]["result"]
        self.assertEqual(set(called), {"content", "structuredContent", "isError"})
        self.assertFalse(called["isError"])
        self.assertEqual(set(listed), {"tools"})
        self.assertEqual(replies[3], {"jsonrpc": "2.0", "id": 4, "result": {}})

    def test_ping_is_removed_for_modern_requests_only(self):
        replies = exchange(
            frame(modern("ping", identifier=1)),
            frame({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
        )
        self.assertEqual(replies[0]["error"], {"code": -32601, "message": "Method not found"})
        self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": 2, "result": {}})

    def test_initialize_then_modern_request_stays_modern(self):
        replies = exchange(
            frame(INIT),
            frame(modern("tools/list", identifier=2)),
            frame(modern("ping", identifier=3)),
        )
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(replies[1]["result"]["resultType"], "complete")
        self.assertEqual(replies[1]["result"]["ttlMs"], rpc.TTL_MS)
        self.assertEqual(replies[2]["error"]["code"], -32601)

    def test_modern_request_then_legacy_request_stays_legacy(self):
        replies = exchange(
            frame(modern("tools/list", identifier=1)),
            frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            frame(dict(INIT, id=3)),
        )
        self.assertEqual(replies[0]["result"]["resultType"], "complete")
        self.assertEqual(set(replies[1]["result"]), {"tools"})
        self.assertEqual(replies[2]["result"]["protocolVersion"], "2025-06-18")
        self.assertNotIn("resultType", replies[2]["result"])

    def test_modern_requests_need_no_initialize(self):
        reply = exchange(frame(modern("tools/call", {"name": "agiw_consent", "arguments": {"action": "show"}})))[0]
        self.assertEqual(reply["result"]["resultType"], "complete")
        self.assertEqual(reply["result"]["_meta"], SERVER_INFO)

    def test_two_modern_tools_list_calls_are_identical(self):
        replies = exchange(
            frame(modern("tools/list", identifier=1)),
            frame(modern("tools/list", identifier=2)),
        )
        self.assertEqual(replies[0]["result"]["tools"], replies[1]["result"]["tools"])
        self.assertEqual([tool["name"] for tool in replies[1]["result"]["tools"]], TOOL_NAMES)


class _Input:
    def __init__(self):
        self.frames = queue.Queue()

    def readline(self, _limit):
        return self.frames.get()

    def send(self, message):
        self.frames.put(json.dumps(message).encode("utf-8") + b"\n")

    def close(self):
        self.frames.put(b"")


class _Output:
    def __init__(self):
        self.frames = queue.Queue()

    def write(self, raw):
        self.frames.put(json.loads(raw))

    def flush(self):
        pass


class RpcModernProgressTests(IsolatedTestCase):
    def test_modern_run_progress_has_message_without_initialize(self):
        stdin, stdout = _Input(), _Output()
        callbacks = []

        def tool(_name, _args):
            callback = getattr(nisi._CANCEL, "progress", None)
            callbacks.append(callback)
            if callback is not None:
                callback(EVENT)
            return {"outcome": "COMPLETED"}, False

        params = {"name": "agiw_nisi_run", "arguments": {}, "_meta": dict(MODERN_META, progressToken="token")}
        with mock.patch.object(rpc, "call_tool", side_effect=tool):
            server = threading.Thread(target=rpc.serve, args=(stdin, stdout), daemon=True)
            server.start()
            try:
                stdin.send(request("tools/call", params, identifier="run"))
                progress = stdout.frames.get(timeout=5)
                result = stdout.frames.get(timeout=5)
            finally:
                stdin.close()
                server.join(timeout=5)
        self.assertFalse(server.is_alive(), "stdio server did not stop")
        self.assertIsNotNone(callbacks[0])
        self.assertEqual(
            progress,
            {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progressToken": "token", "progress": 1, "message": "author attempt 0 started"},
            },
        )
        self.assertEqual(result["id"], "run")
        self.assertEqual(result["result"]["resultType"], "complete")

    def test_modern_run_with_bad_version_errors_without_progress_or_tool_call(self):
        stdin, stdout = _Input(), _Output()
        meta = dict(MODERN_META, progressToken="token", **{"io.modelcontextprotocol/protocolVersion": "1900-01-01"})
        params = {"name": "agiw_nisi_run", "arguments": {}, "_meta": meta}
        with mock.patch.object(rpc, "call_tool") as tool:
            server = threading.Thread(target=rpc.serve, args=(stdin, stdout), daemon=True)
            server.start()
            try:
                stdin.send(request("tools/call", params, identifier="run"))
                reply = stdout.frames.get(timeout=5)
                stdin.send({"jsonrpc": "2.0", "id": "after", "method": "ping"})
                after = stdout.frames.get(timeout=5)
            finally:
                stdin.close()
                server.join(timeout=5)
        self.assertFalse(server.is_alive(), "stdio server did not stop")
        tool.assert_not_called()
        self.assertEqual(reply["id"], "run")
        self.assertNotIn("result", reply)
        self.assertEqual(reply["error"]["code"], -32022)
        self.assertEqual(reply["error"]["data"], {"supported": ["2026-07-28"], "requested": "1900-01-01"})
        self.assertEqual(after, {"jsonrpc": "2.0", "id": "after", "result": {}})
        self.assertTrue(stdout.frames.empty())


if __name__ == "__main__":
    unittest.main()
