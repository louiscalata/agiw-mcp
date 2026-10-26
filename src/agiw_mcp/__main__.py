# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Entry point. With no arguments, serve MCP on stdio.

Human commands (never used by MCP clients):
  agiw-mcp --version
  agiw-mcp readiness | status
  agiw-mcp consent show | grant | revoke

Command handlers are imported when that command runs, so `--version` does not
load the server.
"""
from __future__ import annotations

import json
import sys

from . import __version__

USAGE = "usage: agiw-mcp [--version | readiness | status | consent show|grant|revoke]"


def _print(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _consent_cli(action: str) -> int:
    from . import consent
    from .errors import ToolError

    if action == "show":
        info = consent.show()
        print(info["disclosure"])
        print(f"Consent recorded: {'yes' if info['granted'] else 'no'}")
        return 0
    if action == "revoke":
        _print(consent.revoke())
        return 0
    if action == "grant":
        print(consent.disclosure_text())
        if not sys.stdin.isatty():
            print("Refused: run this in an interactive terminal to grant consent.", file=sys.stderr)
            return 2
        print()
        print("To allow agiw-mcp to send the items above to TypeSafe, type the two words")
        print('"I agree" (without the quotes) and press Return. Anything else records nothing.')
        answer = input("> ")
        # Trim outer whitespace, surrounding straight quotes, trailing periods, then outer whitespace.
        if answer.strip().strip("'\"").rstrip(".").strip().casefold() != "i agree":
            print('Consent was not recorded. Run the command again and type only the two words "I agree".')
            return 1
        try:
            _print(consent.grant(consent.disclosure_sha256()))
        except ToolError as exc:
            print(exc.message, file=sys.stderr)
            return 1
        return 0
    print(USAGE, file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if not args:
        from .rpc import serve
        try:
            return serve()
        except KeyboardInterrupt:
            return 0
    if args == ["--version"]:
        print(f"agiw-mcp {__version__}")
        return 0
    if args == ["readiness"]:
        from .readiness import evaluate
        result = evaluate(stop_at_first=True)
        _print(result)
        return 0 if result["status"] == "READY" else 1
    if args == ["status"]:
        from .tools import status_tool
        _print(status_tool({}))
        return 0
    if len(args) == 2 and args[0] == "consent":
        return _consent_cli(args[1])
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
