# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""MCP over stdio: newline-delimited JSON-RPC 2.0.

Only JSON-RPC frames are written to stdout; diagnostics go to stderr and never
contain request content. Incoming lines are bounded; an oversized line is
discarded up to its newline so the next frame stays aligned. Requests are
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
# Modern per-request protocol. A request whose params._meta carries any key with
# this prefix is answered statelessly under MODERN_PROTOCOLS; every other
# request, including initialize, keeps the legacy handshake and its frames.
MODERN_PROTOCOLS = ("2026-07-28",)
MODERN_META_PREFIX = "io.modelcontextprotocol/"
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


def _diag(message: str) -> None:
    try:
        sys.stderr.write(f"{SERVER_NAME}: {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
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
    if not any(isinstance(key, str) and key.startswith(MODERN_META_PREFIX) for key in meta):
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


def _handle_modern(identifier, method: str, params: dict, meta: dict) -> dict:
    """Serve one stateless modern request. Never reads or writes the process era."""
    rejection = _modern_rejection(identifier, meta)
    if rejection is not None:
        return rejection
    if method == "server/discover":
        if set(params) - {"_meta"}:
            return _error(identifier, INVALID_PARAMS, "Invalid params")
        return _ok(identifier, _discover_result())
    if method == "ping":
        # ping is removed in 2026-07-28; a dual-era server keeps it for legacy clients only.
        return _error(identifier, METHOD_NOT_FOUND, "Method not found")
    if method == "tools/list":
        return _tools_list(identifier, params, modern=True)
    if method == "tools/call":
        return _tools_call(identifier, params, modern=True)
    return _error(identifier, METHOD_NOT_FOUND, "Method not found")


def handle(message: object) -> dict | None:  # noqa: C901 - one branch per JSON-RPC method
    """Return a response dict, or None when no response is due."""
    if isinstance(message, list):
        return _error(None, INVALID_REQUEST, "Batch requests are not supported")
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "Invalid Request")
    if "method" not in message:
        if "result" in message or "error" in message:
            # A response to a server request; this server sends none, so ignore it.
            return None
        # Neither a request nor a response: answer it, or a client waiting on that id hangs forever.
        identifier = message.get("id")
        return _error(identifier if _valid_id(identifier) else None, INVALID_REQUEST, "Invalid Request")
    method = message.get("method")
    has_id = "id" in message
    identifier = message.get("id")
    if not isinstance(method, str) or set(message) - {"jsonrpc", "id", "method", "params"}:
        return _error(identifier if _valid_id(identifier) else None, INVALID_REQUEST, "Invalid Request")
    if not has_id:
        return None  # notifications (initialized, cancelled, ...) need no reply
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
        return _handle_modern(identifier, method, params, meta)
    if method == "server/discover":
        # A modern method without modern _meta is a malformed request, not an unknown one.
        return _error(identifier, INVALID_PARAMS, "Invalid params")
    if method == "ping":
        return _ok(identifier, {})
    if method == "tools/list":
        return _tools_list(identifier, params, modern=False)
    if method == "tools/call":
        return _tools_call(identifier, params, modern=False)
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


def encode(message: dict) -> bytes:
    try:
        raw = json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    except (TypeError, ValueError):
        raw = json.dumps(_error(message.get("id"), INTERNAL_ERROR, "Internal error"), separators=(",", ":")).encode(
            "ascii"
        )
    if len(raw) > MAX_OUTPUT_BYTES:
        raw = json.dumps(_error(message.get("id"), INTERNAL_ERROR, "Response too large"), separators=(",", ":")).encode(
            "ascii"
        )
    return raw + b"\n"


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


def serve(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:  # noqa: C901 - one loop owns reads, workers and cancellation
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    events = queue.Queue(maxsize=16)
    stopped = threading.Event()
    pending = deque()
    active = None
    negotiated_version = None
    progress_count = 0

    def post(event: tuple) -> None:
        while not stopped.is_set():
            try:
                events.put(event, timeout=0.1)
                return
            except queue.Full:
                continue

    def read() -> None:
        while not stopped.is_set():
            try:
                frame = read_frame(stdin)
            except _FrameTooLarge:
                post(("large", None))
                continue
            except (OSError, ValueError):
                frame = None
            post(("frame", frame))
            if frame is None:
                return

    def respond(reply: dict | None) -> None:
        if reply is not None:
            stdout.write(encode(reply))
            stdout.flush()

    def dispatch(message: object) -> dict | None:
        nonlocal negotiated_version
        try:
            reply = handle(message)
            if isinstance(message, dict) and message.get("method") == "initialize" and reply and "result" in reply:
                negotiated_version = reply["result"]["protocolVersion"]
            return reply
        except Exception as exc:  # noqa: BLE001 - request boundary: logged to stderr, answered as INTERNAL_ERROR
            _diag("internal error: " + type(exc).__name__)
            identifier = message.get("id") if isinstance(message, dict) and _valid_id(message.get("id")) else None
            return _error(identifier, INTERNAL_ERROR, "Internal error")

    def run(message: dict, cancel: threading.Event) -> None:
        def progress(event: dict) -> None:
            if stopped.is_set() or cancel.is_set():
                return
            try:
                events.put_nowait(("progress", (cancel, event)))
            except queue.Full:
                pass  # Progress is lossy; never delay the runner or terminal result.

        nisi._CANCEL.event = cancel
        if _progress_token(message) is not None:
            nisi._CANCEL.progress = progress
        try:
            post(("done", dispatch(message)))
        finally:
            if hasattr(nisi._CANCEL, "progress"):
                del nisi._CANCEL.progress
            del nisi._CANCEL.event

    reader = threading.Thread(target=read, name="agiw-mcp-stdin", daemon=True)
    reader.start()
    try:
        while True:
            from_pending = False
            try:
                # Honor buffered cancellation/EOF before dispatching queued work.
                kind, value = events.get_nowait() if active is None and pending else events.get()
            except queue.Empty:
                message = pending.popleft()
                from_pending = True
            else:
                if kind == "progress":
                    cancel, event = value
                    if (
                        active is not None
                        and cancel is active[1]
                        and not cancel.is_set()
                        and progress_count < nisi.MAX_PROGRESS_EVENTS
                    ):
                        progress_count += 1
                        respond(_progress_notification(active[3], event, progress_count, active[4]))
                    continue
                if kind == "done":
                    if not active[1].is_set():
                        respond(value)
                    active[2].join()
                    active = None
                    continue
                if kind == "large":
                    _diag("discarded an oversized frame")
                    respond(_error(None, PARSE_ERROR, "Parse error: frame too large"))
                    continue
                if value is None:
                    return 0
                if not value.strip():
                    continue
                try:
                    message = decode_frame(value)
                except (ValueError, UnicodeError, RecursionError):
                    respond(_error(None, PARSE_ERROR, "Parse error"))
                    continue
            if (
                isinstance(message, dict)
                and message.get("jsonrpc") == "2.0"
                and "id" not in message
                and message.get("method") == "notifications/cancelled"
            ):
                params = message.get("params")
                identifier = params.get("requestId") if isinstance(params, dict) else None
                if _valid_id(identifier):
                    if active and type(identifier) is type(active[0]) and identifier == active[0]:
                        active[1].set()
                    pending = deque(
                        item
                        for item in pending
                        if not (
                            isinstance(item, dict)
                            and type(item.get("id")) is type(identifier)
                            and item.get("id") == identifier
                        )
                    )
                continue
            if not from_pending and (active is not None or pending):
                if isinstance(message, dict) and message.get("method") == "ping":
                    respond(dispatch(message))
                elif len(pending) < 16:
                    pending.append(message)
                elif isinstance(message, dict) and _valid_id(message.get("id")):
                    respond(_error(message["id"], INTERNAL_ERROR, "Server busy"))
                continue
            # Only the long-running Nisi tool needs a worker. Other tools retain synchronous ordering.
            params = message.get("params") if isinstance(message, dict) else None
            if (
                isinstance(message, dict)
                and message.get("method") == "tools/call"
                and _valid_id(message.get("id"))
                and isinstance(params, dict)
                and params.get("name") == "agiw_nisi_run"
            ):
                cancel = threading.Event()
                worker = threading.Thread(target=run, args=(message, cancel), name="agiw-mcp-nisi")
                active = (
                    message["id"],
                    cancel,
                    worker,
                    _progress_token(message),
                    _progress_version(message, negotiated_version),
                )
                progress_count = 0
                worker.start()
            else:
                respond(dispatch(message))
    finally:
        stopped.set()
        if active is not None:
            active[1].set()
            active[2].join()
        reader.join(timeout=0.2)
