# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""MCP over stdio: newline-delimited JSON-RPC 2.0.

Only JSON-RPC frames are written to stdout. Incoming lines are bounded and an
oversized line is discarded up to its newline so the next frame stays aligned. Requests are
serialized per process; ping and cancellation remain responsive during Nisi runs.
"""

from __future__ import annotations

from collections import deque
import json
import queue
import threading
import sys
from typing import BinaryIO

from . import SERVER_NAME, __version__
from . import nisi
from .tools import TOOL_NAMES, TOOLS, call_tool

MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
# Legacy initialize handshake. Newest first. 2026-07-28 is not in this list.
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
# Modern per-request protocol. A request whose params._meta carries the
# PROTOCOL_VERSION_KEY is answered statelessly under MODERN_PROTOCOLS; every
# other request, including initialize and a legacy request that carries other
# io.modelcontextprotocol/ keys, keeps the legacy handshake and its frames.
MODERN_PROTOCOLS = ("2026-07-28",)
PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"
# Cache hint for the tool list, which is a constant for a given release.
TTL_MS = 3600000
INSTRUCTIONS = (
    'AGIW MCP. When the user says "initiate online code mode" with no task, call agiw_readiness and report READY '
    "or the first missing local prerequisite with its fix; start nothing else. With a task: use agiw_nisi_run "
    "with the relevant file contents and review the proposed contents yourself. Jev pre/post checks are optional "
    "and require recorded consent and a key. Jev answers are advisory, not a verdict. The agiw_consent tool "
    "cannot grant consent. Ask the user to read the disclosure and run the consent CLI themselves; do not "
    "write consent.json or automate the prompt. Same-user shell or file access can change the consent record. "
    "agiw_nisi_run never writes project files; applying its proposed contents is a separate decision."
)

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
# Specification-reserved code for a modern request naming a version this server does not speak.
UNSUPPORTED_PROTOCOL_VERSION = -32022


class _FrameTooLarge(Exception):
    pass


def _error(identifier, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def _ok(identifier, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _reject_constant(_value):
    raise ValueError("non-finite number")


def decode_frame(raw: bytes):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_reject_constant)


def _tool_result(identifier, structured: dict, is_error: bool, *, modern: bool = False) -> dict:
    text = json.dumps(structured, ensure_ascii=False, indent=1)
    result = {"content": [{"type": "text", "text": text}], "structuredContent": structured, "isError": is_error}
    if modern:
        result = _modern_result(result, cacheable=False)
    return _ok(identifier, result)


def _modern_meta(method: object, params: dict) -> dict | None:
    """Return params._meta when this request opts into the modern protocol, else None."""
    if method == "initialize":
        return None
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    # Legacy clients may send other io.modelcontextprotocol/ keys (for example
    # related-task); only the per-request protocol version selects the modern path.
    if PROTOCOL_VERSION_KEY not in meta:
        return None
    return meta


def _server_info_meta() -> dict:
    return {SERVER_INFO_KEY: {"name": SERVER_NAME, "version": __version__}}


def _modern_result(fields: dict, *, cacheable: bool) -> dict:
    """Add the modern result fields this server can answer with, without touching the input."""
    result = dict(fields)
    result["resultType"] = "complete"
    if cacheable:
        result["ttlMs"] = TTL_MS
        result["cacheScope"] = "public"
    result["_meta"] = _server_info_meta()
    return result


def _discover_result() -> dict:
    return {
        "resultType": "complete",
        "supportedVersions": list(MODERN_PROTOCOLS),
        "capabilities": {"tools": {}},
        "instructions": INSTRUCTIONS,
        "ttlMs": TTL_MS,
        "cacheScope": "public",
        "_meta": _server_info_meta(),
    }


def _unsupported_version(identifier, requested: str) -> dict:
    reply = _error(identifier, UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version")
    reply["error"]["data"] = {"supported": list(MODERN_PROTOCOLS), "requested": requested}
    return reply


def _modern_rejection(identifier, meta: dict) -> dict | None:
    """Answer a malformed or unsupported modern request, or return None when it is valid."""
    requested = meta.get(PROTOCOL_VERSION_KEY)
    if not isinstance(requested, str) or not isinstance(meta.get(CLIENT_CAPABILITIES_KEY), dict):
        return _error(identifier, INVALID_PARAMS, "Invalid params")
    if requested not in MODERN_PROTOCOLS:
        return _unsupported_version(identifier, requested)
    return None


def _tools_list(identifier, params: dict, *, modern: bool) -> dict:
    if set(params) - {"cursor", "_meta"} or "cursor" in params:
        # This server returns every tool in one page, so any cursor is invalid.
        return _error(identifier, INVALID_PARAMS, "Invalid params")
    listed = {"tools": TOOLS}
    return _ok(identifier, _modern_result(listed, cacheable=True) if modern else listed)


def _tools_call(identifier, params: dict, *, modern: bool) -> dict:
    name, args = params.get("name"), params.get("arguments", {})
    if args is None:
        args = {}
    if set(params) - {"name", "arguments", "_meta"} or not isinstance(name, str) or not isinstance(args, dict):
        return _error(identifier, INVALID_PARAMS, "Invalid params")
    if name not in TOOL_NAMES:
        return _error(identifier, INVALID_PARAMS, f"Unknown tool: {name[:64]}")
    structured, is_error = call_tool(name, args)
    return _tool_result(identifier, structured, is_error, modern=modern)


def handle(message: object) -> dict | None:  # noqa: C901 - one branch per JSON-RPC method
    """Return a response dict, or None when no response is due."""
    if isinstance(message, list):
        return _error(None, INVALID_REQUEST, "Batch requests are not supported")
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "Invalid Request")
    if message.get("jsonrpc") != "2.0":
        identifier = message.get("id")
        return _error(identifier if _valid_id(identifier) else None, INVALID_REQUEST, "Invalid Request")
    if "method" not in message:
        if "result" in message or "error" in message:
            return None
        identifier = message.get("id")
        return _error(identifier if _valid_id(identifier) else None, INVALID_REQUEST, "Invalid Request")
    method = message.get("method")
    has_id = "id" in message
    identifier = message.get("id")
    if not isinstance(method, str) or set(message) - {"jsonrpc", "id", "method", "params"}:
        return _error(identifier if _valid_id(identifier) else None, INVALID_REQUEST, "Invalid Request")
    if not has_id:
        return None
    if not _valid_id(identifier):
        return _error(None, INVALID_REQUEST, "Invalid Request")
    params = message.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(identifier, INVALID_PARAMS, "Invalid params")
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return _ok(
            identifier,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "AGIW MCP", "version": __version__},
                "instructions": INSTRUCTIONS,
            },
        )
    meta = _modern_meta(method, params)
    if meta is not None:
        rejection = _modern_rejection(identifier, meta)
        if rejection is not None:
            return rejection
    if method == "server/discover":
        if meta is None or set(params) - {"_meta"}:
            return _error(identifier, INVALID_PARAMS, "Invalid params")
        return _ok(identifier, _discover_result())
    if method == "ping":
        return _ok(identifier, {}) if meta is None else _error(identifier, METHOD_NOT_FOUND, "Method not found")
    if method == "tools/list":
        return _tools_list(identifier, params, modern=meta is not None)
    if method == "tools/call":
        return _tools_call(identifier, params, modern=meta is not None)
    return _error(identifier, METHOD_NOT_FOUND, "Method not found")


def _valid_id(identifier) -> bool:
    if isinstance(identifier, bool):
        return False
    if isinstance(identifier, int):
        return True
    if isinstance(identifier, str) and len(identifier) <= 256:
        try:
            identifier.encode("utf-8")
            return True
        except UnicodeError:
            return False
    return False


def _encoded(message: object) -> bytes:
    try:
        raw = json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    except (TypeError, ValueError):
        raise ValueError("unencodable response") from None
    return raw


def encode(message: dict) -> bytes:
    try:
        raw = _encoded(message)
    except ValueError:
        raw = _encoded(_error(message.get("id"), INTERNAL_ERROR, "Internal error"))
    if len(raw) > MAX_OUTPUT_BYTES:
        raw = _encoded(_error(message.get("id"), INTERNAL_ERROR, "Response too large"))
    return raw + b"\n"


def _expects_reply(message: object) -> bool:
    if not isinstance(message, dict):
        return True
    if message.get("jsonrpc") != "2.0":
        return True
    if "method" not in message:
        return True
    method = message.get("method")
    if not isinstance(method, str) or set(message) - {"jsonrpc", "id", "method", "params"}:
        return True
    return "id" in message


def _reply_identifier(message: object):
    if not isinstance(message, dict):
        return None
    identifier = message.get("id")
    return identifier if _valid_id(identifier) else None


def _batch_fallback(message: object) -> dict:
    return _error(_reply_identifier(message), INTERNAL_ERROR, "Response too large")


class _Batch:
    def __init__(self, members: list) -> None:
        self.members = members
        self.cursor = 0
        self.slots = {
            index: _encoded(_batch_fallback(member)) for index, member in enumerate(members) if _expects_reply(member)
        }
        self.remaining = sum(len(fallback) + 1 for fallback in self.slots.values())
        self.used = 1
        self.encoded = []

    def reserve_size(self) -> int:
        return self.used + self.remaining

    def complete(self, index: int, reply: dict) -> None:
        fallback = self.slots.pop(index)
        self.remaining -= len(fallback) + 1
        try:
            actual = _encoded(reply)
        except ValueError:
            actual = _encoded(_error(_reply_identifier(self.members[index]), INTERNAL_ERROR, "Internal error"))
        if self.used + len(actual) + 1 + self.remaining > MAX_OUTPUT_BYTES:
            actual = fallback
        self.encoded.append(actual)
        self.used += len(actual) + 1

    def cancel(self, identifier) -> None:
        for index, fallback in tuple(self.slots.items()):
            member = self.members[index]
            if index >= self.cursor and isinstance(member, dict) and _same_id(member.get("id"), identifier):
                self.remaining -= len(fallback) + 1
                del self.slots[index]

    def cancel_active(self, index: int) -> None:
        fallback = self.slots.pop(index)
        self.remaining -= len(fallback) + 1

    def finish(self) -> bytes | None:
        if not self.encoded:
            return None
        return b"[" + b",".join(self.encoded) + b"]"


def read_frame(stream: BinaryIO) -> bytes | None:
    """Read one line. Returns None at EOF; raises _FrameTooLarge after discarding an oversized line."""
    line = stream.readline(MAX_LINE_BYTES + 1)
    if not line:
        return None
    if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
        while True:
            rest = stream.readline(65536)
            if not rest or rest.endswith(b"\n"):
                break
        raise _FrameTooLarge()
    return line.rstrip(b"\r\n")


def _progress_token(message: dict) -> str | int | None:
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    token = meta.get("progressToken") if isinstance(meta, dict) else None
    return token if type(token) in (str, int) else None


def _request_version(message: object) -> str | None:
    """The modern version this request carries in its own _meta, or None for a legacy request."""
    if not isinstance(message, dict):
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    meta = _modern_meta(message.get("method"), params)
    if meta is None:
        return None
    version = meta.get(PROTOCOL_VERSION_KEY)
    return version if isinstance(version, str) else None


def _progress_version(message: object, negotiated: str | None) -> str | None:
    """Modern requests answer from their own _meta; legacy requests keep the process version."""
    version = _request_version(message)
    return negotiated if version is None else version


def _progress_notification(token: str | int, event: dict, count: int, version: str | None) -> dict:
    params = {"progressToken": token, "progress": count}
    if version is not None and version >= "2025-03-26":
        params["message"] = f"{event['stage']} attempt {event['attempt']} {event['status']}"
    return {"jsonrpc": "2.0", "method": "notifications/progress", "params": params}


def _is_cancel_notification(message: object) -> bool:
    return (
        isinstance(message, dict)
        and message.get("jsonrpc") == "2.0"
        and "id" not in message
        and message.get("method") == "notifications/cancelled"
    )


def _cancelled_id(message: dict):
    """The valid requestId a cancellation names, or None."""
    params = message.get("params")
    identifier = params.get("requestId") if isinstance(params, dict) else None
    return identifier if _valid_id(identifier) else None


def _same_id(left, right) -> bool:
    return type(left) is type(right) and left == right


def _drop_queued(pending: deque, identifier) -> deque:
    """Queued requests minus any whose id matches the cancelled one."""
    return deque(item for item in pending if not (isinstance(item, dict) and _same_id(item.get("id"), identifier)))


def _is_nisi_run(message: object) -> bool:
    """Only the long-running Nisi tool needs a worker. Other tools retain synchronous ordering."""
    if not isinstance(message, dict) or message.get("method") != "tools/call" or not _valid_id(message.get("id")):
        return False
    params = message.get("params")
    return isinstance(params, dict) and params.get("name") == "agiw_nisi_run"


def _is_batch_nisi(message: object) -> bool:
    if not _is_nisi_run(message) or message.get("jsonrpc") != "2.0":
        return False
    if set(message) - {"jsonrpc", "id", "method", "params"}:
        return False
    params = message.get("params")
    return _modern_meta(message.get("method"), params) is None


class _Session:
    """One stdio session: a reader thread feeds events; this loop owns replies, the worker and cancellation."""

    def __init__(self, stdin: BinaryIO, stdout: BinaryIO) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.events = queue.Queue(maxsize=16)
        self.stopped = threading.Event()
        self.pending = deque()
        self.active = None  # (id, cancel event, worker thread, progress token, progress version)
        self.batch = None
        self.batch_worker_slot = None
        self.negotiated_version = None
        self.progress_count = 0

    def post(self, event: tuple) -> None:
        while not self.stopped.is_set():
            try:
                self.events.put(event, timeout=0.1)
                return
            except queue.Full:
                continue

    def read(self) -> None:
        while not self.stopped.is_set():
            try:
                frame = read_frame(self.stdin)
            except _FrameTooLarge:
                self.post(("large", None))
                continue
            except (OSError, ValueError):
                frame = None
            self.post(("frame", frame))
            if frame is None:
                return

    def respond(self, reply: dict | None) -> None:
        if reply is not None:
            self.stdout.write(encode(reply))
            self.stdout.flush()

    def respond_batch(self, batch: _Batch) -> None:
        raw = batch.finish()
        if raw is None:
            return
        if len(raw) > MAX_OUTPUT_BYTES:
            self.respond(_error(None, INTERNAL_ERROR, "Response too large"))
            return
        self.stdout.write(raw + b"\n")
        self.stdout.flush()

    def dispatch(self, message: object) -> dict | None:
        try:
            reply = handle(message)
            if isinstance(message, dict) and message.get("method") == "initialize" and reply and "result" in reply:
                self.negotiated_version = reply["result"]["protocolVersion"]
            return reply
        except Exception:  # noqa: BLE001 - JSON-RPC 2.0 §5.1 Internal error
            identifier = message.get("id") if isinstance(message, dict) and _valid_id(message.get("id")) else None
            return _error(identifier, INTERNAL_ERROR, "Internal error")

    def run(self, message: dict, cancel: threading.Event) -> None:
        def progress(event: dict) -> None:
            if self.stopped.is_set() or cancel.is_set():
                return
            try:
                self.events.put_nowait(("progress", (cancel, event)))
            except queue.Full:
                pass  # Progress is lossy; never delay the runner or terminal result.

        nisi._CANCEL.event = cancel
        if _progress_token(message) is not None:
            nisi._CANCEL.progress = progress
        try:
            self.post(("done", self.dispatch(message)))
        finally:
            if hasattr(nisi._CANCEL, "progress"):
                del nisi._CANCEL.progress
            del nisi._CANCEL.event

    def _on_progress(self, value: tuple) -> None:
        cancel, event = value
        active = self.active
        if (
            active is not None
            and cancel is active[1]
            and not cancel.is_set()
            and self.progress_count < nisi.MAX_PROGRESS_EVENTS
        ):
            self.progress_count += 1
            self.respond(_progress_notification(active[3], event, self.progress_count, active[4]))

    def _on_done(self, reply: dict | None) -> None:
        active = self.active
        if not active[1].is_set():
            if self.batch_worker_slot is None:
                self.respond(reply)
            elif reply is not None:
                self.batch.complete(self.batch_worker_slot, reply)
        elif self.batch_worker_slot is not None:
            self.batch.cancel_active(self.batch_worker_slot)
        active[2].join()
        self.active = None
        self.batch_worker_slot = None

    def _on_event(self, kind: str, value) -> tuple[str, object]:
        if kind == "progress":
            self._on_progress(value)
            return "skip", None
        if kind == "done":
            self._on_done(value)
            return "skip", None
        if kind == "large":
            self.respond(_error(None, PARSE_ERROR, "Parse error: frame too large"))
            return "skip", None
        if value is None:
            return "eof", None
        if not value.strip():
            return "skip", None
        try:
            return "message", decode_frame(value)
        except (ValueError, UnicodeError, RecursionError):
            self.respond(_error(None, PARSE_ERROR, "Parse error"))
            return "skip", None

    def _on_cancel(self, message: dict) -> None:
        identifier = _cancelled_id(message)
        if identifier is None:
            return
        if self.active and _same_id(identifier, self.active[0]):
            self.active[1].set()
        if self.batch is not None:
            self.batch.cancel(identifier)
        self.pending = _drop_queued(self.pending, identifier)

    def _while_busy(self, message: object) -> None:
        """A request that arrives while a Nisi run is active or work is queued: answer ping, queue or refuse."""
        if isinstance(message, dict) and message.get("method") == "ping":
            self.respond(self.dispatch(message))
        elif len(self.pending) < 16:
            self.pending.append(message)
        elif isinstance(message, list) and self.negotiated_version == "2025-03-26":
            if not message:
                self.respond(_error(None, INVALID_REQUEST, "Invalid Request"))
                return
            batch = _Batch(message)
            if batch.reserve_size() > MAX_OUTPUT_BYTES:
                self.respond(_error(None, INTERNAL_ERROR, "Response too large"))
            else:
                for index in tuple(batch.slots):
                    batch.complete(index, _error(_reply_identifier(message[index]), INTERNAL_ERROR, "Server busy"))
                self.respond_batch(batch)
        elif isinstance(message, dict) and _valid_id(message.get("id")):
            self.respond(_error(message["id"], INTERNAL_ERROR, "Server busy"))

    def _start(self, message: dict, batch: _Batch | None = None, slot: int | None = None) -> None:
        cancel = threading.Event()
        worker = threading.Thread(target=self.run, args=(message, cancel), name="agiw-mcp-nisi")
        self.active = (
            message["id"],
            cancel,
            worker,
            _progress_token(message),
            _progress_version(message, self.negotiated_version),
        )
        self.batch_worker_slot = slot if batch is not None else None
        self.progress_count = 0
        worker.start()

    def _batch_reply(self, batch: _Batch, index: int, message: object) -> None:
        if isinstance(message, dict) and "method" not in message:
            batch.complete(index, _error(_reply_identifier(message), INVALID_REQUEST, "Invalid Request"))
            return
        if isinstance(message, dict) and message.get("method") == "initialize":
            batch.complete(index, _error(_reply_identifier(message), INVALID_REQUEST, "Invalid Request"))
            return
        if isinstance(message, dict):
            params = message.get("params")
            if isinstance(params, dict) and _modern_meta(message.get("method"), params) is not None:
                batch.complete(index, _error(_reply_identifier(message), INVALID_REQUEST, "Invalid Request"))
                return
        reply = self.dispatch(message)
        if reply is not None:
            batch.complete(index, reply)

    def _advance_batch(self) -> None:
        batch = self.batch
        if batch is not None and self.active is None and batch.cursor < len(batch.members):
            index = batch.cursor
            message = batch.members[index]
            batch.cursor += 1
            if index in batch.slots:
                if _is_batch_nisi(message):
                    self._start(message, batch, index)
                    return
                else:
                    self._batch_reply(batch, index, message)
        if batch is not None and self.active is None and batch.cursor == len(batch.members):
            self.respond_batch(batch)
            self.batch = None

    def _start_batch(self, members: list) -> None:
        if not members:
            self.respond(_error(None, INVALID_REQUEST, "Invalid Request"))
            return
        batch = _Batch(members)
        if batch.reserve_size() > MAX_OUTPUT_BYTES:
            self.respond(_error(None, INTERNAL_ERROR, "Response too large"))
            return
        self.batch = batch
        while self.batch is batch and self.active is None:
            self._advance_batch()

    def loop(self) -> int:
        reader = threading.Thread(target=self.read, name="agiw-mcp-stdin", daemon=True)
        reader.start()
        try:
            while True:
                from_pending = False
                try:
                    # Honor buffered cancellation/EOF before dispatching queued work.
                    kind, value = (
                        self.events.get_nowait()
                        if self.active is None and (self.pending or self.batch is not None)
                        else self.events.get()
                    )
                except queue.Empty:
                    if self.batch is not None:
                        self._advance_batch()
                        continue
                    else:
                        message = self.pending.popleft()
                        from_pending = True
                else:
                    state, message = self._on_event(kind, value)
                    if state == "eof":
                        return 0
                    if state == "skip":
                        continue
                if _is_cancel_notification(message):
                    self._on_cancel(message)
                elif not from_pending and (self.active is not None or self.pending or self.batch is not None):
                    self._while_busy(message)
                elif isinstance(message, list) and self.negotiated_version == "2025-03-26":
                    self._start_batch(message)
                elif _is_nisi_run(message):
                    self._start(message)
                else:
                    self.respond(self.dispatch(message))
        finally:
            self.stopped.set()
            if self.active is not None:
                self.active[1].set()
                self.active[2].join()
            reader.join(timeout=0.2)


def serve(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    return _Session(stdin or sys.stdin.buffer, stdout or sys.stdout.buffer).loop()
