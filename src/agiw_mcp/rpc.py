# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""MCP over stdio: newline-delimited JSON-RPC 2.0.

Only JSON-RPC frames are written to stdout; diagnostics go to stderr and never
contain request content. Incoming lines are bounded; an oversized line is
discarded up to its newline so the next frame stays aligned. Requests are
handled one at a time.
"""
from __future__ import annotations

import json
import sys
from typing import BinaryIO

from . import SERVER_NAME, __version__
from .tools import TOOL_NAMES, TOOLS, call_tool

MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "AGIW MCP. When the user says \"initiate online code mode\" with no task, call agiw_readiness and report READY "
    "or the first missing prerequisite with its fix; start nothing else. With a task: agiw_jev_pre on the task "
    "text (advisory), agiw_nisi_run with the relevant file contents, review the proposed contents yourself, then "
    "agiw_jev_post on the result. Jev answers are advisory, not a verdict. Never grant consent unless the user "
    "explicitly agreed after reading agiw_consent show. agiw_nisi_run never writes project files; applying its "
    "proposed contents is a separate decision."
)

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603


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


def _tool_result(identifier, structured: dict, is_error: bool) -> dict:
    text = json.dumps(structured, ensure_ascii=False, indent=1)
    return _ok(identifier, {"content": [{"type": "text", "text": text}], "structuredContent": structured,
                            "isError": is_error})


def handle(message):
    """Return a response dict, or None when no response is due."""
    if isinstance(message, list):
        return _error(None, INVALID_REQUEST, "Batch requests are not supported")
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "Invalid Request")
    if "method" not in message:
        # A response to a server request; this server sends none, so ignore it. Anything else parsed as JSON
        # but is not a request or a response, and must not be dropped without an error.
        if _looks_like_response(message):
            return None
        return _error(None, PARSE_ERROR, "Parse error")
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
        return _ok(identifier, {"protocolVersion": version,
                                "capabilities": {"tools": {"listChanged": False}},
                                "serverInfo": {"name": SERVER_NAME, "title": "AGIW MCP", "version": __version__},
                                "instructions": INSTRUCTIONS})
    if method == "ping":
        return _ok(identifier, {})
    if method == "tools/list":
        if set(params) - {"cursor", "_meta"}:
            return _error(identifier, INVALID_PARAMS, "Invalid params")
        return _ok(identifier, {"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments", {})
        if args is None:
            args = {}
        if (set(params) - {"name", "arguments", "_meta"} or not isinstance(name, str)
                or not isinstance(args, dict)):
            return _error(identifier, INVALID_PARAMS, "Invalid params")
        if name not in TOOL_NAMES:
            return _error(identifier, INVALID_PARAMS, f"Unknown tool: {name[:64]}")
        structured, is_error = call_tool(name, args)
        return _tool_result(identifier, structured, is_error)
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


def _looks_like_response(message: dict) -> bool:
    """True for a JSON-RPC response: an id, and exactly one of result or error."""
    if set(message) - {"jsonrpc", "id", "result", "error"} or "id" not in message:
        return False
    identifier = message["id"]
    if identifier is not None and not _valid_id(identifier):
        return False
    return ("result" in message) != ("error" in message)


def encode(message: dict) -> bytes:
    try:
        raw = json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    except (TypeError, ValueError):
        raw = json.dumps(_error(message.get("id"), INTERNAL_ERROR, "Internal error"),
                         separators=(",", ":")).encode("ascii")
    if len(raw) > MAX_OUTPUT_BYTES:
        raw = json.dumps(_error(message.get("id"), INTERNAL_ERROR, "Response too large"),
                         separators=(",", ":")).encode("ascii")
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


def serve(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    while True:
        try:
            frame = read_frame(stdin)
        except _FrameTooLarge:
            _diag("discarded an oversized frame")
            stdout.write(encode(_error(None, PARSE_ERROR, "Parse error: frame too large")))
            stdout.flush()
            continue
        if frame is None:
            return 0
        if not frame.strip():
            continue
        try:
            message = decode_frame(frame)
        except (ValueError, UnicodeError, RecursionError):
            stdout.write(encode(_error(None, PARSE_ERROR, "Parse error")))
            stdout.flush()
            continue
        try:
            reply = handle(message)
        except Exception as exc:  # noqa: BLE001 - keep serving; never echo exception text
            _diag("internal error: " + type(exc).__name__)
            identifier = message.get("id") if isinstance(message, dict) and _valid_id(message.get("id")) else None
            reply = _error(identifier, INTERNAL_ERROR, "Internal error")
        if reply is not None:
            stdout.write(encode(reply))
            stdout.flush()
