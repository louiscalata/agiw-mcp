# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
import contextlib
import io
import json
import subprocess
import sys
import threading
import unittest
from unittest import mock

from support import IsolatedTestCase, ROOT, subprocess_env
from test_rpc_progress import _EVENT, _session

from agiw_mcp import rpc


def exchange(*frames: bytes) -> list[dict]:
    stdin = io.BytesIO(b"".join(frames))
    stdout = io.BytesIO()
    rpc.serve(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def frame(message) -> bytes:
    return json.dumps(message).encode() + b"\n"


def _batch_bytes(members: list) -> bytes:
    """Run a batch on a session that has already negotiated its legacy version."""
    stdin = io.BytesIO(frame(members))
    stdout = io.BytesIO()
    session = rpc._Session(stdin, stdout)
    session.negotiated_version = "2025-03-26"
    session.loop()
    return stdout.getvalue()


def _initialize(version: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "init",
        "method": "initialize",
        "params": {**INIT["params"], "protocolVersion": version},
    }


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}


class RpcTests(IsolatedTestCase):
    def test_frames_reject_duplicate_names_and_non_finite_numbers(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}'):
            with self.subTest(raw=raw):
                self.assertEqual(
                    exchange(raw + b"\n"),
                    [{"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}],
                )

    def test_stdio_batches_are_enabled_only_after_2025_03_26_negotiation(self):
        members = [{"jsonrpc": "2.0", "id": index, "method": "ping"} for index in range(2)]
        replies = exchange(frame(_initialize("2025-03-26")), frame(members))
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(
            replies[1],
            [
                {"jsonrpc": "2.0", "id": 0, "result": {}},
                {"jsonrpc": "2.0", "id": 1, "result": {}},
            ],
        )

        for version in (*rpc.SUPPORTED_PROTOCOLS, "1999-01-01"):
            if version == "2025-03-26":
                continue
            with self.subTest(version=version):
                output = exchange(frame(_initialize(version)), frame(members))
                negotiated = output[0]["result"]["protocolVersion"]
                self.assertEqual(
                    negotiated, version if version in rpc.SUPPORTED_PROTOCOLS else rpc.SUPPORTED_PROTOCOLS[0]
                )
                self.assertEqual(
                    output[1],
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Batch requests are not supported"},
                    },
                )

        self.assertEqual(
            rpc.handle(members),
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Batch requests are not supported"},
            },
        )

    def test_batch_members_get_ordered_replies_with_exact_invalid_ids(self):
        members = [
            {"jsonrpc": "2.0", "id": "first", "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "1.0", "id": "wrong-version", "method": "ping"},
            {"jsonrpc": "2.0", "id": True, "method": "ping"},
            3,
            {"jsonrpc": "2.0", "id": "response", "result": {}},
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": "missing"},
                "extra": True,
            },
            {"jsonrpc": "2.0", "id": "last", "method": "ping"},
        ]
        replies = [json.loads(line) for line in _batch_bytes(members).splitlines()]
        self.assertEqual(
            replies,
            [
                [
                    {"jsonrpc": "2.0", "id": "first", "result": {}},
                    {"jsonrpc": "2.0", "id": "wrong-version", "error": {"code": -32600, "message": "Invalid Request"}},
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
                    {"jsonrpc": "2.0", "id": "response", "error": {"code": -32600, "message": "Invalid Request"}},
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
                    {"jsonrpc": "2.0", "id": "last", "result": {}},
                ]
            ],
        )
        self.assertIsNone(rpc.handle({"jsonrpc": "2.0", "id": "response", "result": {}}))

    def test_empty_batch_is_invalid_and_valid_notification_batch_is_silent(self):
        empty = exchange(frame(_initialize("2025-03-26")), frame([]))
        self.assertEqual(len(empty), 2)
        self.assertEqual(
            empty[1], {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        )

        notifications = [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "absent"}},
        ]
        silent = exchange(frame(_initialize("2025-03-26")), frame(notifications))
        self.assertEqual(len(silent), 1)
        self.assertEqual(silent[0]["id"], "init")

    def test_batch_does_not_dispatch_idless_request_shaped_members(self):
        idless_run = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "agiw_nisi_run", "arguments": {}},
        }
        idless_initialize = _initialize("2025-03-26")
        idless_initialize.pop("id")
        modern_notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {"_meta": {rpc.PROTOCOL_VERSION_KEY: "2026-07-28", rpc.CLIENT_CAPABILITIES_KEY: {}}},
        }
        supported_notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        ping = {"jsonrpc": "2.0", "id": "after", "method": "ping"}
        members = [idless_run, idless_initialize, modern_notification, supported_notification, ping]

        session = rpc._Session(io.BytesIO(frame(members)), io.BytesIO())
        session.negotiated_version = "2025-03-26"
        with (
            mock.patch.object(session, "dispatch", wraps=session.dispatch) as dispatch,
            mock.patch.object(rpc, "call_tool") as call_tool,
        ):
            session.loop()

        dispatched = [call.args[0] for call in dispatch.call_args_list]
        for unsupported in (idless_run, idless_initialize, modern_notification):
            self.assertNotIn(unsupported, dispatched)
        self.assertEqual(
            [json.loads(line) for line in session.stdout.getvalue().splitlines()],
            [[{"jsonrpc": "2.0", "id": "after", "result": {}}]],
        )
        call_tool.assert_not_called()

        with mock.patch.object(rpc, "call_tool") as call_tool:
            for standalone in (idless_run, idless_initialize, modern_notification):
                self.assertIsNone(rpc.handle(standalone))
        call_tool.assert_not_called()

    def test_batch_keeps_initialize_legacy_and_rejects_modern_marked_members(self):
        modern_meta = {
            rpc.PROTOCOL_VERSION_KEY: "2026-07-28",
            rpc.CLIENT_CAPABILITIES_KEY: {},
        }
        members = [
            _initialize("2025-03-26") | {"id": "nested-init"},
            {
                "jsonrpc": "2.0",
                "id": "modern",
                "method": "tools/list",
                "params": {"_meta": modern_meta},
            },
            {"jsonrpc": "2.0", "id": "legacy", "method": "ping"},
        ]
        with mock.patch.object(rpc, "call_tool") as call_tool:
            replies = [json.loads(line) for line in _batch_bytes(members).splitlines()]
        self.assertEqual(
            replies,
            [
                [
                    {"jsonrpc": "2.0", "id": "nested-init", "error": {"code": -32600, "message": "Invalid Request"}},
                    {"jsonrpc": "2.0", "id": "modern", "error": {"code": -32600, "message": "Invalid Request"}},
                    {"jsonrpc": "2.0", "id": "legacy", "result": {}},
                ]
            ],
        )
        call_tool.assert_not_called()

    def test_batch_exact_aggregate_limit_and_member_fallback(self):
        members = [{"jsonrpc": "2.0", "id": "boundary", "method": "tools/list"}]
        expected = [rpc.handle(member) for member in members]
        expected_raw = json.dumps(expected, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
        with mock.patch.object(rpc, "MAX_OUTPUT_BYTES", len(expected_raw)):
            raw = _batch_bytes(members)
        self.assertEqual(raw, expected_raw + b"\n")
        self.assertEqual(len(raw) - 1, len(expected_raw))

        oversized = {"jsonrpc": "2.0", "id": "list", "method": "tools/list"}
        ping = {"jsonrpc": "2.0", "id": "fits", "method": "ping"}
        fallback = rpc._batch_fallback(oversized)
        ping_fallback = rpc._batch_fallback(ping)
        minimum = 1 + len(rpc._encoded(fallback)) + 1 + len(rpc._encoded(ping_fallback)) + 1
        with mock.patch.object(rpc, "MAX_OUTPUT_BYTES", minimum):
            raw = _batch_bytes([oversized, ping])
        self.assertEqual(
            json.loads(raw),
            [fallback, {"jsonrpc": "2.0", "id": "fits", "result": {}}],
        )

    def test_batch_over_reserve_refuses_before_dispatch_without_partial_output(self):
        scalar = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "Response too large"}}
        member = {
            "jsonrpc": "2.0",
            "id": "x" * 256,
            "method": "tools/call",
            "params": {"name": "agiw_readiness", "arguments": {}},
        }
        with (
            mock.patch.object(rpc, "MAX_OUTPUT_BYTES", len(rpc._encoded(scalar))),
            mock.patch.object(rpc, "call_tool") as call_tool,
        ):
            raw = _batch_bytes([member])
        self.assertEqual(json.loads(raw), scalar)
        self.assertEqual(len(raw.splitlines()), 1)
        call_tool.assert_not_called()

    def test_more_than_16_synchronous_batch_members_do_not_use_pending_frame_slots(self):
        members = [{"jsonrpc": "2.0", "id": index, "method": "ping"} for index in range(24)]
        replies = exchange(frame(_initialize("2025-03-26")), frame(members))
        self.assertEqual(len(replies), 2)
        self.assertEqual(len(replies[1]), 24)
        self.assertEqual([reply["id"] for reply in replies[1]], list(range(24)))

    def test_same_array_cancel_does_not_cancel_active_nisi_member(self):
        run = {
            "jsonrpc": "2.0",
            "id": "run",
            "method": "tools/call",
            "params": {"name": "agiw_nisi_run", "arguments": {}},
        }
        cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "run"}}
        ping = {"jsonrpc": "2.0", "id": "after", "method": "ping"}
        with _session(self, events=(), version="2025-03-26") as session:
            session.stdin.send([run, cancel, ping])
            self.assertTrue(session.started.wait(2))
            session.ping("outside")
            self.assertEqual(session.next(), {"jsonrpc": "2.0", "id": "outside", "result": {}})
            self.assertTrue(session.stdout.frames.empty())
            session.release.set()
            self.assertEqual(
                session.next(),
                [
                    {
                        "jsonrpc": "2.0",
                        "id": "run",
                        "result": {
                            "content": [{"type": "text", "text": '{\n "outcome": "COMPLETED"\n}'}],
                            "structuredContent": {"outcome": "COMPLETED"},
                            "isError": False,
                        },
                    },
                    {"jsonrpc": "2.0", "id": "after", "result": {}},
                ],
            )
            self.assertEqual(session.stdout.writers, {session.thread.ident})

    def test_later_line_cancel_drops_active_and_future_batch_runs(self):
        def run(identifier):
            return {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": "tools/call",
                "params": {"name": "agiw_nisi_run", "arguments": {}, "_meta": {"progressToken": identifier}},
            }

        def cancel(identifier):
            return {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": identifier},
            }

        calls = []
        with _session(self, events=(), version="2025-03-26") as session:

            def call_tool(name, args):
                calls.append(name)
                return session.tool(name, args)

            with mock.patch.object(rpc, "call_tool", side_effect=call_tool):
                session.stdin.send([run("active"), run("future"), {"jsonrpc": "2.0", "id": "kept", "method": "ping"}])
                self.assertTrue(session.started.wait(2))
                self.assertIsNotNone(session.callback)
                session.callback(_EVENT)
                self.assertEqual(session.next()["method"], "notifications/progress")
                session.stdin.send(cancel("active"))
                session.stdin.send(cancel("future"))
                session.ping("barrier")
                self.assertEqual(session.next(), {"jsonrpc": "2.0", "id": "barrier", "result": {}})
                session.callback(_EVENT)
                session.release.set()
                self.assertEqual(session.next(), [{"jsonrpc": "2.0", "id": "kept", "result": {}}])
            self.assertEqual(calls, ["agiw_nisi_run"])
            self.assertEqual(session.stdout.writers, {session.thread.ident})

    def test_eof_while_batch_run_is_active_emits_no_partial_aggregate(self):
        eof_posted = threading.Event()
        original_post = rpc._Session.post

        def track_eof(session, event):
            original_post(session, event)
            if event == ("frame", None):
                eof_posted.set()

        run = {
            "jsonrpc": "2.0",
            "id": "run",
            "method": "tools/call",
            "params": {"name": "agiw_nisi_run", "arguments": {}},
        }
        with (
            mock.patch.object(rpc._Session, "post", track_eof),
            _session(self, events=(), version="2025-03-26") as session,
        ):
            initialized = list(session.stdout.writes)
            session.stdin.send([run, {"jsonrpc": "2.0", "id": "later", "method": "ping"}])
            self.assertTrue(session.started.wait(2))
            session.stdin.frames.put(b"")
            self.assertTrue(eof_posted.wait(2))
            session.release.set()
            session.thread.join(timeout=2)
            self.assertFalse(session.thread.is_alive())
            self.assertEqual(session.stdout.writes, initialized)

    def test_encode_nonserializable_and_nan_results(self):
        for value in (object(), float("nan")):
            with self.subTest(value=type(value).__name__):
                raw = rpc.encode({"jsonrpc": "2.0", "id": "request-id", "result": value})
                self.assertTrue(raw.endswith(b"\n"))
                self.assertEqual(
                    json.loads(raw),
                    {"jsonrpc": "2.0", "id": "request-id", "error": {"code": -32603, "message": "Internal error"}},
                )

    def test_encode_oversized_response(self):
        with mock.patch.object(rpc, "MAX_OUTPUT_BYTES", 256):
            raw = rpc.encode({"jsonrpc": "2.0", "id": 7, "result": "x" * 257})
        self.assertLess(len(raw), 256)
        self.assertEqual(
            json.loads(raw), {"jsonrpc": "2.0", "id": 7, "error": {"code": -32603, "message": "Response too large"}}
        )

    def test_initialize_list_and_call(self):
        replies = exchange(
            frame(INIT),
            frame({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            frame(
                {
                    "jsonrpc": "2.0",
                    "id": "c",
                    "method": "tools/call",
                    "params": {"name": "agiw_consent", "arguments": {"action": "show"}},
                }
            ),
        )
        self.assertEqual(len(replies), 3)
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "agiw-mcp")
        names = [tool["name"] for tool in replies[1]["result"]["tools"]]
        self.assertEqual(
            names, ["agiw_readiness", "agiw_status", "agiw_consent", "agiw_jev_pre", "agiw_jev_post", "agiw_nisi_run"]
        )
        for tool in replies[1]["result"]["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            self.assertTrue(tool["description"])
        call = replies[2]
        self.assertEqual(call["id"], "c")
        self.assertFalse(call["result"]["isError"])
        self.assertEqual(json.loads(call["result"]["content"][0]["text"]), call["result"]["structuredContent"])

    def test_accepted_legacy_versions_are_negotiated(self):
        self.assertEqual(rpc.SUPPORTED_PROTOCOLS, ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"))
        for version in ("2025-11-25", "2025-06-18"):
            params = {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
            reply = exchange(frame({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}))[0]
            self.assertEqual(reply["result"]["protocolVersion"], version)
            self.assertNotIn("resultType", reply["result"])

    def test_tools_list_rejects_a_cursor(self):
        for cursor in ("next", "", 0, None):
            with self.subTest(cursor=cursor):
                reply = exchange(
                    frame({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"cursor": cursor}})
                )[0]
                self.assertEqual(reply["error"]["code"], -32602)
                self.assertIsNone(reply["error"].get("data"))
        listed = exchange(frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": {}}}))[0]
        self.assertEqual(len(listed["result"]["tools"]), 6)

    def test_unknown_protocol_version_gets_latest(self):
        message = dict(INIT, params={"protocolVersion": "1999-01-01"})
        reply = exchange(frame(message))[0]
        self.assertEqual(reply["result"]["protocolVersion"], rpc.SUPPORTED_PROTOCOLS[0])

    def test_error_codes(self):
        replies = exchange(
            b"{not json\n",
            frame([INIT]),
            frame({"jsonrpc": "2.0", "id": 3, "method": "nope"}),
            frame(
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "missing_tool", "arguments": {}}}
            ),
            frame({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": 7}}),
            frame({"jsonrpc": "2.0", "id": True, "method": "ping"}),
            frame({"jsonrpc": "1.0", "id": 6, "method": "ping"}),
            b'{"jsonrpc":"2.0","id":7,"id":8,"method":"ping"}\n',
            frame({"jsonrpc": "2.0", "id": 9, "method": "ping"}),
        )
        codes = [reply.get("error", {}).get("code") for reply in replies]
        self.assertEqual(codes, [-32700, -32600, -32601, -32602, -32602, -32600, -32600, -32700, None])
        self.assertEqual(replies[-1], {"jsonrpc": "2.0", "id": 9, "result": {}})

    def test_rejected_jsonrpc_versions_echo_only_valid_ids(self):
        def invalid_request(identifier):
            return {
                "jsonrpc": "2.0",
                "id": identifier,
                "error": {"code": -32600, "message": "Invalid Request"},
            }

        versions = (("missing", False, None), ("null", True, None), ("wrong", True, "1.0"))
        valid_ids = ("", "request-id", 0, 6)
        invalid_ids = (None, True, 1.5, [], {})
        for version_name, has_version, version in versions:
            for identifier in valid_ids:
                with self.subTest(version=version_name, identifier=identifier):
                    message = {"id": identifier, "method": "ping"}
                    if has_version:
                        message["jsonrpc"] = version
                    self.assertEqual(rpc.handle(message), invalid_request(identifier))

            for identifier in invalid_ids:
                with self.subTest(version=version_name, invalid_id=identifier):
                    message = {"id": identifier, "method": "ping"}
                    if has_version:
                        message["jsonrpc"] = version
                    self.assertEqual(rpc.handle(message), invalid_request(None))

            with self.subTest(version=version_name, id="absent"):
                message = {"method": "ping"}
                if has_version:
                    message["jsonrpc"] = version
                self.assertEqual(rpc.handle(message), invalid_request(None))

        self.assertIsNone(rpc.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(rpc.handle(42), invalid_request(None))
        self.assertEqual(
            rpc.handle([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]),
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Batch requests are not supported"},
            },
        )

    def test_a_frame_that_is_neither_request_nor_response_is_answered(self):
        replies = exchange(
            frame([INIT]),
            frame({"jsonrpc": "2.0", "id": 11}),
            frame({"jsonrpc": "2.0", "id": 12, "result": {}}),
            frame({"jsonrpc": "2.0", "id": 13, "method": "ping"}),
        )
        # The bare {"id": 11} frame gets Invalid Request; a real response (id 12) still gets no reply.
        self.assertEqual(
            [(reply.get("id"), reply.get("error", {}).get("code")) for reply in replies[1:]], [(11, -32600), (13, None)]
        )

    def test_tool_errors_are_results_not_protocol_errors(self):
        reply = exchange(
            frame(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "agiw_readiness", "arguments": {"bogus": 1}},
                }
            )
        )[0]
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(reply["result"]["structuredContent"]["error"]["code"], "INPUT_INVALID")

    def test_oversized_frame_is_discarded_and_stream_stays_aligned(self):
        big = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"' + b"a" * (rpc.MAX_LINE_BYTES + 10) + b'"}}\n'
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            replies = exchange(big, frame({"jsonrpc": "2.0", "id": 2, "method": "ping"}))
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": 2, "result": {}})

    def test_internal_errors_hide_details(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(rpc, "call_tool", side_effect=RuntimeError("secret detail")),
            contextlib.redirect_stderr(stderr),
        ):
            reply = exchange(
                frame(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "agiw_status", "arguments": {}},
                    }
                )
            )[0]
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(reply["error"], {"code": -32603, "message": "Internal error"})

    def test_stdio_subprocess_smoke(self):
        frames = (
            frame(INIT)
            + frame({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + frame(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "agiw_readiness", "arguments": {}},
                }
            )
        )
        done = subprocess.run(
            [sys.executable, "-B", "-m", "agiw_mcp"],
            input=frames,
            capture_output=True,
            timeout=60,
            cwd=str(ROOT),
            env=subprocess_env(self.config_dir),
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        replies = [json.loads(line) for line in done.stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])
        readiness = replies[2]["result"]["structuredContent"]
        self.assertIn(readiness["status"], ("READY", "NOT_READY"))
        self.assertNotIn(b"Traceback", done.stderr)


if __name__ == "__main__":
    unittest.main()
