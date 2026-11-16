# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Boundary cases for the README and SECURITY progress contract."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import unittest
from unittest import mock

from support import ROOT, IsolatedTestCase, node_available, runner_result
from test_rpc_progress import _Output, _session

from agiw_mcp import nisi, rpc

EVENT = {"kind": "agiw-mcp.nisi-event.v1", "stage": "author", "attempt": 0, "status": "started"}
RESULT = {"kind": "agiw-mcp.nisi-run.v1", "report": {"outcome": "COMPLETED"}}


def _line(value):
    return json.dumps(value).encode("utf-8") + b"\n"


class DocumentedProgressTests(IsolatedTestCase):
    def assert_documented(self, filename, phrase):
        path = ROOT / filename
        if path.is_file():  # README and SECURITY are not both included in an sdist.
            text = " ".join(path.read_text(encoding="utf-8").split())
            self.assertIn(phrase, text)

    def test_message_cutoff_is_inclusive_and_has_no_total(self):
        self.assert_documented("README.md", "`2025-03-26` or newer and is omitted for older protocols.")
        for version, message in (
            (None, False),
            ("2024-11-05", False),
            ("2025-03-25", False),
            ("2025-03-26", True),
            ("2025-03-27", True),
            ("2025-06-18", True),
            ("2025-11-25", True),
        ):
            with self.subTest(version=version):
                params = {"progressToken": "token", "progress": 1}
                if message:
                    params["message"] = "author attempt 0 started"
                self.assertEqual(
                    rpc._progress_notification("token", EVENT, 1, version),
                    {"jsonrpc": "2.0", "method": "notifications/progress", "params": params},
                )

    def test_tokens_keep_their_types_without_treating_booleans_as_integers(self):
        self.assert_documented("SECURITY.md", "booleans and other values are ignored")
        for token in (True, False, None, 0.0, 1.0, [], {}):
            with self.subTest(invalid=token):
                self.assertIsNone(rpc._progress_token({"params": {"_meta": {"progressToken": token}}}))
        for token in (0, 1, -1, "", "token"):
            with self.subTest(valid=token):
                actual = rpc._progress_token({"params": {"_meta": {"progressToken": token}}})
                self.assertEqual(actual, token)
                self.assertIs(type(actual), type(token))

    def test_attempts_are_preserved_for_every_stage_and_status(self):
        self.assert_documented("README.md", "The attempt number is the engine's zero-based attempt.")
        for stage in ("author", "static", "tests", "review"):
            for status in ("started", "finished"):
                for attempt in (0, 1, 3):
                    with self.subTest(stage=stage, status=status, attempt=attempt):
                        event = {**EVENT, "stage": stage, "status": status, "attempt": attempt}
                        forwarded = []
                        nisi._RunnerEvents(forwarded.append, None).feed(_line(event))
                        self.assertEqual(forwarded, [event])
                        self.assertEqual(
                            rpc._progress_notification(0, forwarded[0], 1, "2025-03-26"),
                            {
                                "jsonrpc": "2.0",
                                "method": "notifications/progress",
                                "params": {
                                    "progressToken": 0,
                                    "progress": 1,
                                    "message": f"{stage} attempt {attempt} {status}",
                                },
                            },
                        )

    @unittest.skipUnless(node_available(), "Node 22+ required for the host-runner attempt contract")
    def test_host_runner_emits_zero_based_draft_and_repair_attempts(self):
        work = self.tmp / "runner"
        work.mkdir()
        nisi._copy_verified(nisi.VENDOR_DIR, work)
        request = {
            "schema": 1,
            "taskText": "Fix the value.",
            "files": [{"path": "a.py", "content": "value = 0\n"}],
            "allowedFiles": ["a.py"],
            "acceptanceCriteria": ["Set value to one."],
            "assertions": [],
            "repairBudget": 1,
            "deadlineMs": 5000,
            "endpoint": "http://127.0.0.1:1234/v1/chat/completions",
            "authorModel": "author",
            "reviewerModel": "reviewer",
            "maxOutputTokens": 1000,
            "reasoningEffort": "none",
            "language": "python",
            "python": sys.executable,
            "checkTimeoutMs": 1000,
        }
        # The existing runner injection point returns model fixtures without opening sockets.
        harness = """
globalThis.agiwMcpFetch = async (_url, init) => {
  const body = JSON.parse(init.body);
  const operation = body.response_format.json_schema.name;
  let answer;
  if (operation === 'nisi_draft') {
    answer = {candidate: {files: [{path: 'a.py', content: 'value = (\\n'}]}, note: 'draft'};
  } else if (operation === 'nisi_repair') {
    answer = {status: 'REPAIRED', candidate: {files: [{path: 'a.py', content: 'value = 1\\n'}]}, note: 'fix'};
  } else {
    answer = {findings: [], summary: 'reviewed'};
  }
  return new Response(JSON.stringify({model: body.model,
    choices: [{finish_reason: 'stop', message: {role: 'assistant', content: JSON.stringify(answer)}}],
    usage: {prompt_tokens: 10, completion_tokens: 5, total_tokens: 15}}));
};
await import(RUNNER);
""".replace("RUNNER", json.dumps((work / "runner.mjs").as_uri()))
        node = nisi.find_node()[0]
        done = subprocess.run(
            [node, "--input-type=module", "-e", harness],
            input=json.dumps(request).encode(),
            capture_output=True,
            cwd=work,
            env=nisi._child_env(node, str(work)),
            timeout=10,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        result = runner_result(done.stdout)
        self.assertEqual(result["report"]["outcome"], "COMPLETED", result)
        self.assertEqual(result["report"]["repairAttempts"], 1)
        events = [json.loads(raw) for raw in done.stdout.splitlines()][:-1]
        self.assertEqual(events[0], EVENT)
        self.assertEqual([event["attempt"] for event in events if event["stage"] == "author"], [0, 0, 1, 1])
        self.assertEqual({event["attempt"] for event in events}, {0, 1})

    def test_event_schema_rejects_each_missing_duplicate_and_extra_field(self):
        self.assert_documented("SECURITY.md", "against a closed set of fields and stage/status values")
        invalid = []
        for key in EVENT:
            invalid.append((f"missing {key}", _line({name: value for name, value in EVENT.items() if name != key})))
            pairs = list(EVENT.items()) + [(key, EVENT[key])]
            duplicate = "{" + ",".join(json.dumps(name) + ":" + json.dumps(value) for name, value in pairs) + "}\n"
            invalid.append((f"duplicate {key}", duplicate.encode("utf-8")))
        for key in ("text", "task", "path", "model", "message"):
            invalid.append((f"extra {key}", _line({**EVENT, key: "private content"})))
        for key, value in (
            ("kind", "other"),
            ("stage", "repair"),
            ("status", "passed"),
            ("attempt", True),
            ("attempt", False),
            ("attempt", 0.0),
            ("attempt", "0"),
        ):
            invalid.append((f"invalid {key}={value!r}", _line({**EVENT, key: value})))
        invalid.append(("array of pairs", _line(list(EVENT.items()))))
        for label, raw in invalid:
            with self.subTest(case=label):
                events = []
                parser = nisi._RunnerEvents(events.append, None)
                parser.feed(raw + _line(EVENT))
                self.assertEqual(events, [EVENT])

    def test_event_cap_keeps_the_64th_and_drops_the_65th_in_each_chunking(self):
        self.assert_documented("README.md", "At most 64 events are forwarded")
        self.assert_documented("SECURITY.md", "Events are capped at 64 per run")
        records = [{**EVENT, "attempt": attempt} for attempt in range(65)]
        for size in (63, 64, 65):
            for split in (False, True):
                with self.subTest(size=size, split=split):
                    events = []
                    parser = nisi._RunnerEvents(events.append, None)
                    if split:
                        for record in records[:size]:
                            parser.feed(_line(record))
                    else:
                        parser.feed(b"".join(map(_line, records[:size])))
                    self.assertEqual(events, records[: min(size, 64)])

    def test_cancellation_stops_buffered_and_later_event_handoff(self):
        self.assert_documented("README.md", "No progress is sent after cancellation or the result.")
        for coalesced in (False, True):
            with self.subTest(coalesced=coalesced):
                cancel = threading.Event()
                events = []

                def receive(event, events=events, cancel=cancel):
                    events.append(event)
                    cancel.set()

                parser = nisi._RunnerEvents(receive, cancel)
                if coalesced:
                    parser.feed(_line(EVENT) * 2)
                else:
                    parser.feed(_line(EVENT) + _line(EVENT)[:10])
                    parser.feed(_line(EVENT)[10:])
                parser.feed(_line(EVENT), eof=True)
                self.assertEqual(events, [EVENT])
        events = []
        cancel = threading.Event()
        cancel.set()
        nisi._RunnerEvents(events.append, cancel).feed(_line(EVENT))
        self.assertEqual(events, [])

    def test_result_stops_events_in_the_same_chunk_and_later_chunks(self):
        self.assert_documented("SECURITY.md", "no progress is sent after cancellation or the final result.")
        for coalesced in (False, True):
            with self.subTest(coalesced=coalesced):
                events = []
                parser = nisi._RunnerEvents(events.append, None)
                if coalesced:
                    parser.feed(_line(EVENT) + _line(RESULT) + _line(EVENT))
                else:
                    parser.feed(_line(EVENT) + _line(RESULT))
                    parser.feed(_line(EVENT), eof=True)
                self.assertEqual(events, [EVENT])

    def test_full_queue_drops_an_event_and_retains_the_terminal_result(self):
        self.assert_documented("README.md", "events may be dropped if the internal queue is full.")
        queue_type = queue.Queue
        full = threading.Event()
        held = threading.Event()
        resume = threading.Event()
        errors = []
        original_write = _Output.write

        class ObservedQueue(queue_type):
            def put_nowait(self, item):
                try:
                    return super().put_nowait(item)
                except queue.Full:
                    if item[0] == "progress":
                        full.set()
                    raise

        # Force saturation without pinning the implementation's queue capacity.
        events = ObservedQueue(maxsize=1)

        def make_queue(maxsize=0):
            return events if maxsize else queue_type(maxsize)

        def hold_output(output, raw):
            if json.loads(raw).get("id") == "hold":
                held.set()
                resume.wait(5)
            return original_write(output, raw)

        with (
            mock.patch.object(rpc.queue, "Queue", side_effect=make_queue),
            mock.patch.object(_Output, "write", hold_output),
            _session(self, events=()) as session,
        ):
            session.call("token")
            self.assertTrue(session.started.wait(2))
            session.ping("hold")
            self.assertTrue(held.wait(2))

            def produce():
                try:
                    for attempt in range(2):
                        session.callback({**EVENT, "attempt": attempt})
                except Exception as error:  # noqa: BLE001 - retain producer failures for the test thread
                    errors.append(error)

            producer = threading.Thread(target=produce, daemon=True)
            try:
                producer.start()
                producer.join(timeout=1)
                completed = not producer.is_alive()
                dropped = full.is_set()
            finally:
                resume.set()
                producer.join(timeout=2)
            self.assertFalse(producer.is_alive(), "progress producer did not stop after draining resumed")
            self.assertEqual(errors, [])
            self.assertTrue(completed, "a full queue blocked the runner")
            self.assertTrue(dropped, "the test must exercise an actual queue-full drop")
            self.assertEqual(session.next()["id"], "hold")
            self.assertEqual(
                session.next(),
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": "token",
                        "progress": 1,
                        "message": "author attempt 0 started",
                    },
                },
            )
            session.ping("drained")
            self.assertEqual(session.next(), {"jsonrpc": "2.0", "id": "drained", "result": {}})
            self.assertEqual([reply.get("id") for reply in session.finish()], ["run"])


if __name__ == "__main__":
    unittest.main()
