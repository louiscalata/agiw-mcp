#!/usr/bin/env python3
# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Speak initialize and tools/list to an agiw-mcp stdio entry point.

Usage:
  python3 scripts/stdio_handshake.py agiw-mcp
  python3 scripts/stdio_handshake.py python3 -m agiw_mcp
"""
from __future__ import annotations

import json
import subprocess
import sys

PROTOCOL = "2025-03-26"
TOOL_NAMES = (
    "agiw_readiness",
    "agiw_status",
    "agiw_consent",
    "agiw_jev_pre",
    "agiw_jev_post",
    "agiw_nisi_run",
)


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def handshake(command: list[str]) -> dict:
    """Return the checked summary. Exits the process if the entry point is wrong."""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "agiw-mcp-smoke", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    payload = "".join(json.dumps(message) + "\n" for message in messages).encode()
    try:
        done = subprocess.run(command, input=payload, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _fail("entry point failed to run: " + type(exc).__name__)
    if done.returncode != 0:
        _fail(f"entry point exited {done.returncode}")
    if done.stderr.strip():
        detail = done.stderr.decode(errors="replace")[:300]
        _fail("entry point wrote to stderr: " + detail)
    replies = []
    for line in done.stdout.splitlines():
        if not line.strip():
            continue
        try:
            replies.append(json.loads(line))
        except json.JSONDecodeError:
            _fail("entry point stdout was not JSON-RPC")
    if len(replies) != 2:
        _fail(f"expected 2 replies, got {len(replies)}")
    initialized, listed = replies
    result = initialized.get("result") if initialized.get("id") == 1 else None
    info = result.get("serverInfo") if isinstance(result, dict) else None
    if (not isinstance(result, dict) or result.get("protocolVersion") != PROTOCOL
            or not isinstance(info, dict) or info.get("name") != "agiw-mcp" or not info.get("version")):
        _fail("unexpected initialize result")
    tools = listed.get("result", {}).get("tools") if listed.get("id") == 2 else None
    names = tuple(tool.get("name") for tool in tools) if isinstance(tools, list) else ()
    if names != TOOL_NAMES:
        _fail("unexpected tools/list")
    return {"protocolVersion": PROTOCOL,
            "server": {"name": info["name"], "version": info["version"]},
            "tools": list(names)}


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        _fail("usage: stdio_handshake.py COMMAND...")
    print(json.dumps(handshake(command), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
