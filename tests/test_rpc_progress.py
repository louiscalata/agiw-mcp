# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Progress uses the main stdio writer and remains optional and bounded."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import json
import queue
import threading
import unittest
from unittest import mock

from support import IsolatedTestCase

from agiw_mcp import nisi, rpc

_EVENT = {"kind": "agiw-mcp.nisi-event.v1", "stage": "author", "attempt": 0, "status": "started"}
_ABSENT = object()


class _Input:
    def __init__(self):
        self.frames = queue.Queue()
        self.sent = 0
        self.reads = 0
        self.reading = threading.Condition()

    def readline(self, _limit):
        with self.reading:
            self.reads += 1
            self.reading.notify_all()
        return self.frames.get()

    def send(self, message):
        self.sent += 1
        self.frames.put(json.dumps(message).encode("utf-8") + b"\n")

    def posted(self, timeout=2):
        """Wait until the reader has queued every sent frame for the server and asked for another."""
        with self.reading:
            return self.reading.wait_for(lambda: self.reads > self.sent, timeout)


class _Output:
    def __init__(self):
        self.frames = queue.Queue()
        self.writes = []
        self.writers = set()

    def write(self, raw):
        self.writes.append(raw)
        self.writers.add(threading.get_ident())
        self.frames.put(json.loads(raw))

    def flush(self):
        pass


class _Session:
    def __init__(self, events):
        self.stdin = _Input()
        self.stdout = _Output()
        self.started = threading.Event()
        self.release = threading.Event()
        self.callback = None
        self.events = events
        self.thread = threading.Thread(target=rpc.serve, args=(self.stdin, self.stdout))

    def tool(self, _name, _args):
        self.callback = getattr(nisi._CANCEL, "progress", None)
        if self.callback:
            for event in self.events:
                self.callback(event)
        self.started.set()
        self.release.wait(5)
        return {"outcome": "COMPLETED"}, False

    def call(self, token=_ABSENT, *, name="agiw_nisi_run", meta=_ABSENT):
        params = {"name": name, "arguments": {}}
        if meta is not _ABSENT:
            params["_meta"] = meta
        elif token is not _ABSENT:
            params["_meta"] = {"progressToken": token}
        self.stdin.send({"jsonrpc": "2.0", "id": "run", "method": "tools/call", "params": params})

    def next(self):
        return self.stdout.frames.get(timeout=2)

    def ping(self, identifier="ping"):
        self.stdin.send({"jsonrpc": "2.0", "id": identifier, "method": "ping"})

    def finish(self):
        self.release.set()
        replies = []
        while True:
            reply = self.next()
            replies.append(reply)
            if reply.get("id") == "run":
                return replies


@contextmanager
def _session(test, events=(_EVENT,), *, version="2025-06-18"):
    session = _Session(events)
    with mock.patch.object(rpc, "call_tool", side_effect=session.tool):
        session.thread.start()
        try:
            if version is not None:
                session.stdin.send(
                    {
                        "jsonrpc": "2.0",
                        "id": "init",
                        "method": "initialize",
                        "params": {"protocolVersion": version},
                    }
                )
                initialized = session.next()
                test.assertEqual(initialized["result"]["capabilities"], {"tools": {"listChanged": False}})
            yield session
        finally:
            session.release.set()
            session.stdin.frames.put(b"")
            session.thread.join(timeout=3)
            test.assertFalse(session.thread.is_alive(), "stdio server did not stop")


class RpcProgressTests(IsolatedTestCase):
    def test_progress_arrives_before_result_and_ping_remains_responsive(self):
        with _session(self) as session:
            session.call("progress")
            self.assertTrue(session.started.wait(2))
            progress = session.next()
            self.assertEqual(
                progress,
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": "progress",
                        "progress": 1,
                        "message": "author attempt 0 started",
                    },
                },
            )
            session.ping()
            self.assertEqual(session.next(), {"jsonrpc": "2.0", "id": "ping", "result": {}})
            self.assertEqual(session.finish()[-1]["id"], "run")
            self.assertEqual(session.stdout.writers, {session.thread.ident})

    def test_string_and_integer_tokens_are_preserved(self):
        for token in ("", "token", 0, -4, 123):
            with self.subTest(token=token), _session(self) as session:
                session.call(token)
                self.assertTrue(session.started.wait(2))
                progress = session.next()
                self.assertEqual(progress["params"]["progressToken"], token)
                self.assertIs(type(progress["params"]["progressToken"]), type(token))
                session.finish()

    def test_invalid_tokens_and_meta_are_ignored(self):
        cases = [(token, _ABSENT) for token in (True, False, None, 1.5, [], {})]
        cases += [(_ABSENT, meta) for meta in (None, [], True, "token", {"unrelated": "value"})]
        for token, meta in cases:
            with self.subTest(token=token, meta=meta), _session(self) as session:
                session.call(token, meta=meta)
                self.assertTrue(session.started.wait(2))
                self.assertIsNone(session.callback)
                self.assertEqual([reply["id"] for reply in session.finish()], ["run"])

    def test_no_token_has_byte_identical_result_frames(self):
        with _session(self, version=None) as session:
            session.call()
            self.assertTrue(session.started.wait(2))
            self.assertIsNone(session.callback)
            session.finish()
            expected = {
                "jsonrpc": "2.0",
                "id": "run",
                "result": {
                    "content": [{"type": "text", "text": '{\n "outcome": "COMPLETED"\n}'}],
                    "structuredContent": {"outcome": "COMPLETED"},
                    "isError": False,
                },
            }
            self.assertEqual(
                b"".join(session.stdout.writes),
                json.dumps(expected, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n",
            )

    def test_other_tools_ignore_a_progress_token(self):
        with _session(self) as session:
            session.release.set()
            session.call("token", name="agiw_status")
            reply = session.next()
            self.assertEqual(reply["id"], "run")
            self.assertIsNone(session.callback)

    def test_message_uses_the_negotiated_protocol(self):
        for version, has_message in (
            ("2024-11-05", False),
            ("2025-03-26", True),
            ("2025-06-18", True),
            ("2025-11-25", True),
            ("1999-01-01", True),
            (None, False),
        ):
            with self.subTest(version=version), _session(self, version=version) as session:
                session.call(1)
                self.assertTrue(session.started.wait(2))
                params = session.next()["params"]
                self.assertEqual("message" in params, has_message)
                self.assertNotIn("total", params)
                session.finish()

    def test_progress_is_monotonic_and_capped_at_64(self):
        # Acknowledging each event avoids conflating the 64-event cap with the lossy queue.
        with _session(self, events=()) as session:
            session.call("token")
            self.assertTrue(session.started.wait(2))
            self.assertIsNotNone(session.callback)
            for index in range(64):
                session.callback({**_EVENT, "attempt": index})
                self.assertEqual(session.next()["params"]["progress"], index + 1)
            session.callback(_EVENT)
            session.ping()
            self.assertEqual(session.next()["id"], "ping")
            session.finish()

    def test_progress_after_result_is_ignored(self):
        with _session(self, events=()) as session:
            session.call("token")
            self.assertTrue(session.started.wait(2))
            self.assertIsNotNone(session.callback)
            session.finish()
            session.callback(_EVENT)
            session.ping()
            self.assertEqual(session.next()["id"], "ping")

    def test_cancelled_run_does_not_send_more_progress_or_a_result(self):
        with _session(self, events=()) as session:
            session.call("token")
            self.assertTrue(session.started.wait(2))
            self.assertIsNotNone(session.callback)
            session.stdin.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "run"}})
            session.ping("cancelled")
            self.assertEqual(session.next()["id"], "cancelled")
            session.callback(_EVENT)
            session.release.set()
            session.ping("after")
            self.assertEqual(session.next()["id"], "after")

    def test_later_cancellation_filters_queued_batch_members_without_freeing_its_frame_slot(self):
        def run(identifier):
            return {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": "tools/call",
                "params": {"name": "agiw_nisi_run", "arguments": {}},
            }

        def cancel(identifier):
            return {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": identifier},
            }

        nested_initialize = {
            "jsonrpc": "2.0",
            "id": "nested-init",
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
        modern = {
            "jsonrpc": "2.0",
            "id": "modern",
            "method": "tools/list",
            "params": {
                "_meta": {
                    rpc.PROTOCOL_VERSION_KEY: "2026-07-28",
                    rpc.CLIENT_CAPABILITIES_KEY: {},
                }
            },
        }
        wrong_version = {"jsonrpc": "1.0", "id": "wrong-version", "method": "ping"}
        response = {"jsonrpc": "2.0", "id": "response", "result": {}}
        invalid_params = {"jsonrpc": "2.0", "id": "invalid-params", "method": "ping", "params": []}
        malformed = {"jsonrpc": "2.0", "id": "malformed"}
        calls = []
        with _session(self, events=(), version="2025-03-26") as session:

            def call_tool(name, args):
                calls.append(name)
                return session.tool(name, args)

            with mock.patch.object(rpc, "call_tool", side_effect=call_tool):
                session.stdin.send(
                    [
                        run("active"),
                        {"jsonrpc": "2.0", "id": "active-kept", "method": "ping"},
                    ]
                )
                self.assertTrue(session.started.wait(2))
                for index in range(15):
                    session.stdin.send({"jsonrpc": "2.0", "id": f"queued-{index}", "method": "missing"})
                session.stdin.send(
                    [
                        run("drop"),
                        run("7"),
                        run(7),
                        nested_initialize,
                        modern,
                        wrong_version,
                        response,
                        invalid_params,
                        malformed,
                        {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        {"jsonrpc": "2.0", "id": "kept", "method": "ping"},
                    ]
                )
                session.stdin.send(cancel("drop"))
                session.stdin.send(cancel("7"))
                for identifier in (
                    "nested-init",
                    "modern",
                    "wrong-version",
                    "response",
                    "invalid-params",
                    "malformed",
                ):
                    session.stdin.send(cancel(identifier))
                session.stdin.send([run("busy-run"), {"jsonrpc": "2.0", "id": "busy-ping", "method": "ping"}])
                session.ping("barrier")

                self.assertEqual(
                    session.next(),
                    [
                        {"jsonrpc": "2.0", "id": "busy-run", "error": {"code": -32603, "message": "Server busy"}},
                        {"jsonrpc": "2.0", "id": "busy-ping", "error": {"code": -32603, "message": "Server busy"}},
                    ],
                )
                self.assertEqual(session.next(), {"jsonrpc": "2.0", "id": "barrier", "result": {}})
                session.release.set()

                active_batch = session.next()
                self.assertEqual([reply["id"] for reply in active_batch], ["active", "active-kept"])
                queued = [session.next() for _ in range(15)]
                self.assertEqual([reply["id"] for reply in queued], [f"queued-{index}" for index in range(15)])
                self.assertTrue(all(reply["error"]["code"] == -32601 for reply in queued))
                batch = session.next()
                self.assertEqual(
                    [reply["id"] for reply in batch],
                    [7, "nested-init", "modern", "wrong-version", "response", "invalid-params", "malformed", "kept"],
                )
                self.assertTrue(
                    all(reply["error"] == {"code": -32600, "message": "Invalid Request"} for reply in batch[1:5])
                )
                self.assertEqual(batch[5]["error"], {"code": -32602, "message": "Invalid params"})
                self.assertEqual(batch[6]["error"], {"code": -32600, "message": "Invalid Request"})
                self.assertEqual(batch[7], {"jsonrpc": "2.0", "id": "kept", "result": {}})
            self.assertEqual(calls, ["agiw_nisi_run", "agiw_nisi_run"])
            self.assertEqual(session.stdout.writers, {session.thread.ident})

    def test_batch_filter_keeps_notification_invalid_and_unsupported_frames(self):
        request = {
            "jsonrpc": "2.0",
            "id": "cancelled",
            "method": "ping",
        }
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        invalid = {"jsonrpc": "2.0", "id": "invalid"}
        pending = deque([[request, notification], [invalid], [request], []])

        filtered = rpc._drop_queued(pending, "cancelled", "2025-03-26")
        self.assertEqual(list(filtered), [[notification], [invalid], []])

        unsupported = rpc._drop_queued(deque([[request]]), "cancelled", "2025-06-18")
        self.assertEqual(list(unsupported), [[request]])

        ordinary = {"jsonrpc": "2.0", "id": "cancelled", "method": "ping"}
        self.assertEqual(list(rpc._drop_queued(deque([ordinary]), "cancelled", "2025-06-18")), [])

    def test_cancellation_does_not_suppress_nested_initialize_error_in_active_batch(self):
        nested_initialize = {
            "jsonrpc": "2.0",
            "id": "nested-init",
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
        cancellation = {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "nested-init"},
        }
        with _session(self, events=(), version="2025-03-26") as session:
            session.stdin.send(
                [
                    {
                        "jsonrpc": "2.0",
                        "id": "active",
                        "method": "tools/call",
                        "params": {"name": "agiw_nisi_run", "arguments": {}},
                    },
                    nested_initialize,
                ]
            )
            self.assertTrue(session.started.wait(2))
            session.stdin.send(cancellation)
            session.ping("barrier")
            self.assertEqual(session.next(), {"jsonrpc": "2.0", "id": "barrier", "result": {}})
            session.release.set()
            batch = session.next()
            self.assertEqual([reply["id"] for reply in batch], ["active", "nested-init"])
            self.assertEqual(batch[1]["error"], {"code": -32600, "message": "Invalid Request"})

    def test_fully_cancelled_queued_batch_does_not_become_an_empty_batch(self):
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
                session.call()
                self.assertTrue(session.started.wait(2))
                session.stdin.send(
                    [
                        {
                            "jsonrpc": "2.0",
                            "id": "future",
                            "method": "tools/call",
                            "params": {"name": "agiw_nisi_run", "arguments": {}},
                        }
                    ]
                )
                session.stdin.send(cancel("future"))
                session.stdin.send({"jsonrpc": "2.0", "id": "sentinel", "method": "missing"})
                session.ping("barrier")
                self.assertEqual(session.next(), {"jsonrpc": "2.0", "id": "barrier", "result": {}})
                session.release.set()
                self.assertEqual(session.next()["id"], "run")
                self.assertEqual(
                    session.next(),
                    {"jsonrpc": "2.0", "id": "sentinel", "error": {"code": -32601, "message": "Method not found"}},
                )
                self.assertTrue(session.stdout.frames.empty())
            self.assertEqual(calls, ["agiw_nisi_run"])

    def test_queued_arrays_on_other_versions_keep_the_single_message_rejection(self):
        calls = []
        with _session(self, events=(), version="2025-06-18") as session:

            def call_tool(name, args):
                calls.append(name)
                return session.tool(name, args)

            with mock.patch.object(rpc, "call_tool", side_effect=call_tool):
                session.call()
                self.assertTrue(session.started.wait(2))
                session.stdin.send(
                    [
                        {
                            "jsonrpc": "2.0",
                            "id": "future",
                            "method": "tools/call",
                            "params": {"name": "agiw_nisi_run", "arguments": {}},
                        },
                        {"jsonrpc": "2.0", "id": "kept", "method": "ping"},
                    ]
                )
                session.stdin.send(
                    {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "future"}}
                )
                session.ping("barrier")
                self.assertEqual(session.next(), {"jsonrpc": "2.0", "id": "barrier", "result": {}})
                session.release.set()
                self.assertEqual(session.next()["id"], "run")
                self.assertEqual(
                    session.next(),
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Batch requests are not supported"},
                    },
                )
            self.assertEqual(calls, ["agiw_nisi_run"])

    def test_progress_queued_before_cancellation_is_not_sent_after_it(self):
        # The worker queues the event before the main loop applies the cancel frame ahead of it in the queue.
        blocked = threading.Event()
        resume = threading.Event()
        original_write = _Output.write

        def blocking_write(output, raw):
            if json.loads(raw).get("id") == "hold":
                blocked.set()
                resume.wait(2)
            return original_write(output, raw)

        with _session(self, events=()) as session, mock.patch.object(_Output, "write", blocking_write):
            session.call("token")
            self.assertTrue(session.started.wait(2))
            self.assertIsNotNone(session.callback)
            session.ping("hold")
            self.assertTrue(blocked.wait(2))
            session.stdin.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "run"}})
            self.assertTrue(session.stdin.posted())
            session.callback(_EVENT)
            resume.set()
            self.assertEqual(session.next()["id"], "hold")
            session.release.set()
            session.ping("after")
            self.assertEqual(session.next(), {"jsonrpc": "2.0", "id": "after", "result": {}})

    def test_finished_run_callback_does_not_reach_the_next_run(self):
        with _session(self, events=()) as session:
            session.call("first")
            self.assertTrue(session.started.wait(2))
            first = session.callback
            self.assertIsNotNone(first)
            self.assertEqual([reply.get("id") for reply in session.finish()], ["run"])
            session.started.clear()
            session.release.clear()
            session.call("second")
            self.assertTrue(session.started.wait(2))
            self.assertIsNot(session.callback, first)
            first({**_EVENT, "stage": "review"})
            session.ping()
            self.assertEqual(session.next(), {"jsonrpc": "2.0", "id": "ping", "result": {}})
            session.callback(_EVENT)
            progress = session.next()["params"]
            self.assertEqual(progress["progressToken"], "second")
            self.assertEqual(progress["message"], "author attempt 0 started")
            self.assertEqual(progress["progress"], 1)
            self.assertEqual([reply.get("id") for reply in session.finish()], ["run"])

    def test_each_run_counts_progress_from_one(self):
        with _session(self) as session:
            for token in ("first", "second"):
                with self.subTest(token=token):
                    session.started.clear()
                    session.call(token)
                    replies = session.finish()
                    self.assertEqual([reply.get("method") for reply in replies], ["notifications/progress", None])
                    self.assertEqual(replies[0]["params"]["progressToken"], token)
                    self.assertEqual(replies[0]["params"]["progress"], 1)

    def test_full_progress_queue_does_not_block_the_worker(self):
        blocked = threading.Event()
        resume = threading.Event()
        original_write = _Output.write

        def blocking_write(output, raw):
            if json.loads(raw).get("id") == "hold":
                blocked.set()
                resume.wait(2)
            return original_write(output, raw)

        with _session(self, events=()) as session, mock.patch.object(_Output, "write", blocking_write):
            session.call("token")
            self.assertTrue(session.started.wait(2))
            self.assertIsNotNone(session.callback)
            session.ping("hold")
            self.assertTrue(blocked.wait(2))
            producer = threading.Thread(target=lambda: [session.callback(_EVENT) for _ in range(100)])
            producer.start()
            producer.join(timeout=0.5)
            completed_without_drain = not producer.is_alive()
            resume.set()
            producer.join(timeout=2)
            self.assertTrue(completed_without_drain, "progress producer waited for the full queue")
            self.assertEqual(session.next()["id"], "hold")
            replies = session.finish()
            progress = [
                item["params"]["progress"] for item in replies if item.get("method") == "notifications/progress"
            ]
            self.assertLessEqual(len(progress), 16)
            self.assertEqual(progress, list(range(1, len(progress) + 1)))


if __name__ == "__main__":
    unittest.main()
