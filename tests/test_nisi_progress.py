# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Runner event validation, streaming and output limits."""

from __future__ import annotations

import json
import os
import sys
import threading
from unittest import mock

from support import IsolatedTestCase

from agiw_mcp import nisi
from agiw_mcp.errors import ToolError

EVENT = {"kind": "agiw-mcp.nisi-event.v1", "stage": "author", "attempt": 0, "status": "started"}
RESULT = {"kind": "agiw-mcp.nisi-run.v1", "report": {"outcome": "COMPLETED"}}


def line(value):
    return json.dumps(value).encode("utf-8") + b"\n"


class NisiProgressTests(IsolatedTestCase):
    def child(self, output, *, callback=None, script=None):
        if callback is not None:
            nisi._CANCEL.progress = callback
            self.addCleanup(delattr, nisi._CANCEL, "progress")
        return nisi._bounded_run(
            [sys.executable, "-I", "-S", "-B", "-c", script or f"import os; os.write(1, {output!r})"],
            data=b"",
            cwd=str(self.tmp),
            env=dict(os.environ),
            timeout=5,
        )

    def test_event_arrives_before_child_finishes(self):
        marker = self.tmp / "event-received"
        events = []

        def received(event):
            events.append(event)
            marker.write_text("received", encoding="utf-8")

        script = (
            "import os, pathlib, time\n"
            f"os.write(1, {line(EVENT)!r})\n"
            f"marker = pathlib.Path({str(marker)!r})\n"
            "deadline = time.monotonic() + 2\n"
            "while not marker.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
            f"os.write(1, {line(RESULT)!r})\n"
            "raise SystemExit(0 if marker.exists() else 3)\n"
        )
        code, raw, state = self.child(b"", callback=received, script=script)
        self.assertEqual((code, state), (0, "exited"))
        self.assertEqual(events, [EVENT])
        self.assertEqual(nisi._parse_output(raw, {}), RESULT)

    def test_only_exact_typed_event_fields_are_forwarded(self):
        invalid = [
            {**EVENT, "text": "private task path model text"},
            {key: value for key, value in EVENT.items() if key != "attempt"},
            {**EVENT, "kind": "other"},
            {**EVENT, "stage": "private text"},
            {**EVENT, "stage": []},
            {**EVENT, "status": "PASS"},
            {**EVENT, "attempt": True},
            {**EVENT, "attempt": 1.0},
            {**EVENT, "attempt": "1"},
            ["free text"],
        ]
        events = []
        output = b"not JSON\n" + b"".join(line(value) for value in invalid + [EVENT, RESULT])
        code, raw, state = self.child(output, callback=events.append)
        self.assertEqual((code, state), (0, "exited"))
        self.assertEqual(events, [EVENT])
        self.assertEqual(nisi._parse_output(raw, {}), RESULT)
        self.assertNotIn("private", json.dumps(events))

    def test_at_most_64_valid_events_are_handed_off(self):
        events = []
        records = [{**EVENT, "attempt": number} for number in range(100)]
        _, raw, state = self.child(b"".join(map(line, records + [RESULT])), callback=events.append)
        self.assertEqual(state, "exited")
        self.assertEqual(events, records[:64])
        self.assertEqual(nisi._parse_output(raw, {}), RESULT)

    def test_duplicate_event_members_are_not_forwarded(self):
        events = []
        duplicate = b'{"kind":"agiw-mcp.nisi-event.v1","stage":"author","attempt":0,"attempt":1,"status":"started"}\n'
        _, raw, state = self.child(duplicate + line(EVENT) + line(RESULT), callback=events.append)
        self.assertEqual(state, "exited")
        self.assertEqual(events, [EVENT])
        self.assertEqual(nisi._parse_output(raw, {}), RESULT)

    def test_events_after_result_are_not_forwarded(self):
        events = []
        _, raw, state = self.child(line(EVENT) + line(RESULT) + line(EVENT), callback=events.append)
        self.assertEqual(state, "exited")
        self.assertEqual(events, [EVENT])
        self.assertEqual(nisi._parse_output(raw, {}), RESULT)

    def test_cancellation_stops_event_handoff(self):
        cancel = threading.Event()
        nisi._CANCEL.event = cancel
        self.addCleanup(delattr, nisi._CANCEL, "event")
        events = []

        def received(event):
            events.append(event)
            cancel.set()

        try:
            self.child(line(EVENT) * 20 + line(RESULT), callback=received)
        except ToolError as error:
            self.assertEqual(error.code, "NISI_CANCELLED")
        self.assertEqual(events, [EVENT])

    def test_event_bytes_count_against_stdout_cap(self):
        events = []
        # The child builds the repeated output itself: 500 events inline exceed the Windows command-line limit.
        script = f"import os; os.write(1, {line(EVENT)!r} * 500 + {line(RESULT)!r})"
        with mock.patch.object(nisi, "STDOUT_CAP", 1024):
            _, raw, state = self.child(b"", callback=events.append, script=script)
        self.assertEqual(state, "overflow")
        self.assertLessEqual(len(raw), 1024)

    def test_result_only_and_no_callback_keep_the_result(self):
        for output in (line(RESULT), line(RESULT).rstrip(b"\n"), line(EVENT) + line(RESULT)):
            with self.subTest(output=output):
                code, raw, state = self.child(output)
                self.assertEqual((code, state), (0, "exited"))
                self.assertEqual(nisi._parse_output(raw, {}), RESULT)

    def test_split_event_and_unterminated_result_are_read(self):
        events = []
        output = line(EVENT) + line(RESULT).rstrip(b"\n")
        script = f"import os\nfor byte in {output!r}: os.write(1, bytes([byte]))\n"
        _, raw, state = self.child(b"", callback=events.append, script=script)
        self.assertEqual(state, "exited")
        self.assertEqual(events, [EVENT])
        self.assertEqual(nisi._parse_output(raw, {}), RESULT)

    def test_missing_result_still_fails_closed(self):
        with self.assertRaises(ToolError) as caught:
            nisi._parse_output(line(EVENT), {})
        self.assertEqual(caught.exception.code, "NISI_RUNNER_FAILED")

    def test_repeated_result_fails_closed(self):
        second = {**RESULT, "report": {"outcome": "FAILED"}}
        for raw in (line(RESULT) + line(RESULT), line(RESULT) + line(EVENT) + line(second)):
            with self.subTest(raw=raw):
                with self.assertRaises(ToolError) as caught:
                    nisi._parse_output(raw, {})
                self.assertEqual(caught.exception.code, "NISI_RUNNER_FAILED")
                self.assertIn("more than one result", caught.exception.message)
