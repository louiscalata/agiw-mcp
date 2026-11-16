# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Deterministic stream and scheduling fakes for the stdio loop's saved corpus.

Transport cases run the real reader over bytes. Scheduling cases suppress that
reader and publish input lazily, following the existing explicit-queue tests.
Published events share one FIFO. Progress-cap cases pause a real worker inside
the fake tool until callbacks have drained, before it can publish completion.
No process, socket, or application configuration is used.
"""

from __future__ import annotations

import base64
from collections import deque
from contextlib import ExitStack
import io
import json
from pathlib import Path
import queue
import threading
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401 - registers the source tree and the existing socket guard

from agiw_mcp import rpc

CORPUS = Path(__file__).with_name("cases.json")
_ERRORS = {
    "OSError": OSError,
    "ValueError": ValueError,
    "UnicodeError": UnicodeError,
    "RecursionError": RecursionError,
    "RuntimeError": RuntimeError,
    "TypeError": TypeError,
    "BrokenPipeError": BrokenPipeError,
    "KeyboardInterrupt": KeyboardInterrupt,
    "SystemExit": SystemExit,
}


def load_cases(group):
    document = json.loads(CORPUS.read_text(encoding="utf-8"))
    return [case for case in document["cases"] if case["group"] == group]


class ScheduleError(BaseException):
    """An unscripted blocking operation fails promptly, including in mutants."""


class _Flag:
    def __init__(self, rig, number):
        self.rig, self.number, self.value = rig, number, False

    def is_set(self):
        self.rig.record("event.is_set", self.number, self.value)
        return self.value

    def set(self):
        self.rig.record("event.set", self.number)
        self.value = True


class _Thread:
    def __init__(self, rig, target, name, args=(), daemon=None):
        self.rig, self.target, self.name, self.args = rig, target, name, args
        self.ran, self.started = False, False
        self.failure = None
        self.reader = not args
        rig.record("thread", name, daemon, len(args))

    def start(self):
        self.rig.record("thread.start", self.name)
        if self.rig.case.get("thread_start_error") == ("reader" if self.reader else "worker"):
            raise RuntimeError("synthetic thread start failure")
        self.started = True
        if self.reader and self.rig.case["group"] == "streams" and not self.rig.case.get("defer_reader"):
            self.run()
        elif not self.reader:
            self.rig.workers.append(self)

    def run(self):
        if self.ran:
            raise ScheduleError("worker executed twice")
        self.ran = True
        if not self.reader and (self.rig.case.get("live_progress") or self.rig.case.get("progress_events", 0) >= 16):
            self.rig.native_ready.clear()
            self.rig.live_thread = threading.Thread(target=self.execute, daemon=True)
            self.rig.live_thread.start()
            if not self.rig.native_ready.wait(5):
                raise ScheduleError("worker did not reach its tool barrier")
        else:
            self.execute()

    def execute(self):
        previous = self.rig.reader_running
        self.rig.reader_running = self.reader
        try:
            self.target(*self.args)
        except BaseException as exc:  # noqa: BLE001 - native target failures terminate the thread, not serve
            self.failure = {"type": type(exc).__name__, "message": str(exc)}
            self.rig.record("thread.failure", self.name, self.failure)
        finally:
            self.rig.reader_running = previous
            if not self.reader:
                self.rig.native_ready.set()

    def join(self, timeout=None):
        self.rig.record("thread.join", self.name, timeout)
        if not self.started:
            raise RuntimeError("cannot join thread before it is started")
        if not self.ran:
            self.run()
        self.rig.finish_live_worker()
        self.rig.record("thread.joined", self.name, self.rig.local_state())


class _Queue:
    def __init__(self, rig, maxsize):
        self.rig, self.maxsize = rig, maxsize
        rig.record("queue", maxsize)

    def put(self, event, timeout=None):
        self.rig.record("queue.put", event[0], timeout)
        if "queue_put_error" in self.rig.case:
            raise _ERRORS[self.rig.case["queue_put_error"]]("synthetic queue put failure")
        if self.rig.post_full:
            self.rig.post_full -= 1
            raise queue.Full
        if len(self.rig.ready) >= self.maxsize:
            if self.rig.live_thread is None:
                raise ScheduleError("a full queue requires a scheduled consumer")
            self.rig.queue_blocked = True
            self.rig.native_ready.set()
            if not self.rig.queue_space.wait(5):
                raise ScheduleError("the consumer did not make queue space")
            self.rig.queue_blocked = False
            if len(self.rig.ready) >= self.maxsize:
                raise queue.Full
        self.rig.ready.append(event)

    def put_nowait(self, event):
        self.rig.record("queue.put_nowait", event[0])
        if "progress_put_error" in self.rig.case:
            raise _ERRORS[self.rig.case["progress_put_error"]]("synthetic progress put failure")
        if self.rig.progress_full or len(self.rig.ready) >= self.maxsize:
            self.rig.record("queue.full", event[0])
            raise queue.Full
        self.rig.ready.append(event)

    def get_nowait(self):
        return self.get(block=False)

    def get(self, block=True):
        self.rig.record("queue.get", block)
        if "queue_get_error" in self.rig.case:
            raise _ERRORS[self.rig.case["queue_get_error"]]("synthetic queue get failure")
        while not self.rig.ready and self.rig.script:
            action = self.rig.script.popleft()
            self.rig.step(action)
            if action == "empty":
                break
        if not self.rig.ready and not self.rig.script:
            self.rig.finish_live_worker()
        if self.rig.ready:
            event = self.rig.ready.popleft()
            if self.rig.queue_blocked:
                self.rig.queue_space.set()
                self.rig.finish_live_worker()
        elif not block:
            self.rig.record("queue.empty")
            raise queue.Empty
        elif self.rig.inbox:
            event = self.rig.inbox.popleft()
            self.rig.record("input.publish", event[0])
        else:
            raise ScheduleError("unscripted blocking event read")
        self.rig.record("queue.received", event[0])
        return event


class _Input:
    def __init__(self, rig, label="stdin"):
        self.rig, self.label = rig, label
        self.stream = io.BytesIO(base64.b64decode(rig.case["input_base64"], validate=True))
        self.reads = 0

    def __bool__(self):
        return self.rig.case.get("streams") != "falsey"

    def readline(self, limit):
        self.reads += 1
        self.rig.record(self.label + ".readline", limit)
        if self.reads == self.rig.case.get("read_error_at", 1) and "read_error" in self.rig.case:
            raise _ERRORS[self.rig.case["read_error"]]("synthetic read failure")
        return self.stream.readline(limit)


class _Output:
    def __init__(self, rig, label="stdout"):
        self.rig, self.label, self.raw, self.writes, self.flushes = rig, label, bytearray(), 0, 0

    def __bool__(self):
        return self.rig.case.get("streams") != "falsey"

    def write(self, raw):
        self.writes += 1
        self.rig.record(self.label + ".write", base64.b64encode(raw).decode("ascii"))
        if self.writes == self.rig.case.get("write_error_at"):
            self.raw.extend(raw[: self.rig.case.get("partial_write", 0)])
            raise _ERRORS[self.rig.case.get("output_error", "BrokenPipeError")]("synthetic write failure")
        self.raw.extend(raw)
        identifier = json.loads(raw).get("id")
        for trigger in self.rig.case.get("after_write", []):
            if type(identifier) is type(trigger["id"]) and identifier == trigger["id"]:
                message = trigger["message"]
                self.rig.record("input.publish", "frame")
                self.rig.ready.append(("frame", None if message is None else json.dumps(message).encode("utf-8")))
        return len(raw)

    def flush(self):
        self.flushes += 1
        self.rig.record(self.label + ".flush")
        if self.flushes == self.rig.case.get("flush_error_at"):
            raise _ERRORS[self.rig.case.get("output_error", "BrokenPipeError")]("synthetic flush failure")


class _Rig:
    def __init__(self, case):
        self.case = case
        self.calls, self.workers, self.flags, self.callbacks = [], [], [], []
        self.inbox, self.ready = deque(), deque()
        self.live_thread = None
        self.native_ready, self.live_release, self.queue_space = threading.Event(), threading.Event(), threading.Event()
        self.queue_blocked = False
        if case["group"] == "scheduling":
            self.prepare_input()
        if case.get("defer_reader"):
            self.inbox.append(("frame", None))
        self.script = deque(case.get("schedule", []))
        self.reader_running = False
        self.post_full = case.get("post_full", 0)
        self.progress_full = case.get("progress_full", False)
        self.cancellation = SimpleNamespace()
        self.stdin, self.stdout, self.stderr = _Input(self), _Output(self), io.StringIO()
        self.fallback_stdin, self.fallback_stdout = _Input(self, "default.stdin"), _Output(self, "default.stdout")
        self.original_handle = rpc.handle
        self.original_decode = rpc.decode_frame

    def prepare_input(self):
        stream = io.BytesIO(base64.b64decode(self.case["input_base64"], validate=True))
        while True:
            try:
                raw = rpc.read_frame(stream)
            except rpc._FrameTooLarge:
                self.inbox.append(("large", None))
                continue
            self.inbox.append(("frame", raw))
            if raw is None:
                break

    def finish_live_worker(self):
        if self.live_thread is not None:
            self.live_release.set()
            if self.flags and self.flags[0].value:
                self.queue_space.set()
            self.live_thread.join(timeout=5)
            if self.live_thread.is_alive():
                raise ScheduleError("worker did not leave its tool barrier")
            self.live_thread = None

    def record(self, *entry):
        if len(self.calls) >= 12000:
            raise ScheduleError("dependency call budget exhausted")
        self.calls.append(list(entry))

    def local_state(self):
        return sorted(vars(self.cancellation))

    def event(self):
        flag = _Flag(self, len(self.flags))
        self.flags.append(flag)
        self.record("event", flag.number)
        return flag

    def thread(self, **kwargs):
        return _Thread(self, **kwargs)

    def step(self, action):
        self.record("schedule", action)
        if action == "input":
            self.record("input.publish", self.inbox[0][0])
            self.ready.append(self.inbox.popleft())
        elif action == "empty":
            return
        elif action in ("worker", "input_then_worker"):
            worker = next((item for item in self.workers if not item.ran), None)
            if worker is None:
                raise ScheduleError("script requested missing worker")
            if action == "input_then_worker":
                self.record("input.publish", self.inbox[0][0])
                self.ready.append(self.inbox.popleft())
            worker.run()
        elif isinstance(action, dict):
            callback = self.callbacks[action.get("callback", -1)]
            if action.get("cancel"):
                self.workers[action.get("callback", -1)].args[1].set()
            callback(action.get("event", self.case.get("progress_event", default_event())))
        else:
            raise ScheduleError("unknown schedule action")

    def tool(self, name, arguments):
        self.record("call_tool", name, arguments, self.local_state())
        callback = getattr(self.cancellation, "progress", None)
        self.callbacks.append(callback)
        if self.case.get("live_progress"):
            self.native_ready.set()
            if not self.live_release.wait(5):
                raise ScheduleError("tool barrier was not released")
        if callback is not None:
            for index in range(self.case.get("progress_events", 0)):
                callback({**self.case.get("progress_event", default_event()), "attempt": index})
        if "tool_error" in self.case:
            raise _ERRORS[self.case["tool_error"]]("synthetic tool failure")
        return self.case.get("tool_result", {"tool": name, "arguments": arguments, "text": "snowman \u2603"}), bool(
            self.case.get("is_error", False)
        )

    def handle(self, message):
        self.record("handle", message)
        if "handle_error" in self.case:
            raise _ERRORS[self.case["handle_error"]]("synthetic handler failure")
        return self.original_handle(message)

    def decode(self, raw):
        self.record("decode_frame", base64.b64encode(raw).decode("ascii"))
        if "decode_error" in self.case:
            raise _ERRORS[self.case["decode_error"]]("synthetic decode failure")
        return self.original_decode(raw)


def default_event():
    return {"kind": "agiw-mcp.nisi-event.v1", "stage": "author", "attempt": 0, "status": "started"}


def replay(case):
    """Run the current serve implementation and capture its complete observable transcript."""
    rig = _Rig(case)
    result, raised = None, None
    fake_sys = SimpleNamespace(
        stdin=SimpleNamespace(buffer=rig.fallback_stdin),
        stdout=SimpleNamespace(buffer=rig.fallback_stdout),
        stderr=rig.stderr,
    )
    patches = (
        (rpc, "sys", fake_sys),
        (rpc, "queue", SimpleNamespace(Queue=lambda maxsize: _Queue(rig, maxsize), Full=queue.Full, Empty=queue.Empty)),
        (rpc, "threading", SimpleNamespace(Event=rig.event, Thread=rig.thread)),
        (rpc.nisi, "_CANCEL", rig.cancellation),
        (rpc, "call_tool", rig.tool),
        (rpc, "handle", rig.handle),
        (rpc, "decode_frame", rig.decode),
        (rpc, "MAX_LINE_BYTES", case.get("line_limit", rpc.MAX_LINE_BYTES)),
        (rpc, "MAX_OUTPUT_BYTES", case.get("output_limit", rpc.MAX_OUTPUT_BYTES)),
    )
    with ExitStack() as stack:
        for owner, attribute, value in patches:
            stack.enter_context(mock.patch.object(owner, attribute, value))
        try:
            if case.get("streams") == "default":
                result = rpc.serve()
            else:
                result = rpc.serve(rig.stdin, rig.stdout)
        except BaseException as exc:  # noqa: BLE001 - the corpus records escaping transport and harness failures
            raised = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            rig.finish_live_worker()
    return {
        "return": result,
        "raised": raised,
        "stdout_base64": base64.b64encode(rig.stdout.raw).decode("ascii"),
        "default_stdout_base64": base64.b64encode(rig.fallback_stdout.raw).decode("ascii"),
        "stderr": rig.stderr.getvalue(),
        "calls": rig.calls,
        "thread_local_attributes": rig.local_state(),
        "event_states": [flag.value for flag in rig.flags],
        "unconsumed_input_events": len(rig.inbox),
        "unconsumed_posted_events": len(rig.ready),
        "unused_schedule": list(rig.script),
    }


def install_cases(namespace, test_class, group):
    """Install one independently selectable test per generated case."""
    for case in load_cases(group):
        name = "test_" + case["name"]
        if hasattr(test_class, name):
            raise ValueError(f"duplicate case name: {name}")

        def test(self, case=case):
            self.assertEqual(replay(case), case["expected"], case["name"])

        test.__name__ = name
        setattr(test_class, name, test)
    namespace[test_class.__name__] = test_class
