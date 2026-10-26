# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Tool definitions and dispatch. Each handler returns a JSON object."""
from __future__ import annotations

import os
import platform
import sys

from . import SERVER_NAME, __version__, consent, jev, keys, nisi, readiness, tickets
from .config import Config, ConfigError, config_dir, config_file_path, display_path, load_config
from .errors import ToolError

_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
_FILE = {"type": "object", "properties": {"path": {"type": "string", "maxLength": 256},
                                          "content": {"type": "string"}},
         "required": ["path", "content"], "additionalProperties": False}
_ASSERTION = {"type": "object", "properties": {"path": {"type": "string"},
                                               "contains": {"type": "string", "maxLength": 2048},
                                               "not_contains": {"type": "string", "maxLength": 2048}},
              "required": ["path"], "additionalProperties": False}
_TICKET = {"type": "object", "properties": {
    "ticketId": {"type": "string", "maxLength": 128},
    "claimState": {"type": "string", "description": "Only Granted or Active is admitted."},
    "writeScope": {"type": "array", "minItems": 1, "maxItems": tickets.MAX_SCOPES,
                   "items": {"type": "string", "maxLength": tickets.MAX_SCOPE_CHARS}},
    "owner": {"type": "string", "maxLength": tickets.MAX_OWNER_CHARS}},
    "required": ["ticketId", "claimState", "writeScope", "owner"], "additionalProperties": False,
    "description": ("Optional claim ticket (required when config require_ticket is true). Admitted before the "
                    "run starts, refused if its write scope overlaps an admitted ticket, released afterwards.")}

TOOLS = [
    {
        "name": "agiw_readiness",
        "title": "Online code mode readiness",
        "description": ("The \"initiate online code mode\" check. Only checks, never starts work. Returns READY, or "
                        "the FIRST missing prerequisite with an exact fix, in this order: Python, configuration, "
                        "Node.js 22+, vendored Nisi hashes, local OpenAI-compatible model server listing two "
                        "distinct configured models, TypeSafe API key present (value never read), consent recorded."),
        "inputSchema": _EMPTY,
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "agiw_status",
        "title": "AGIW MCP status",
        "description": ("Versions, configuration (home directory shown as ~) and which prerequisites are met. "
                        "Contains no secrets and starts no work."),
        "inputSchema": _EMPTY,
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "agiw_consent",
        "title": "Consent for Jev (TypeSafe)",
        "description": ("Show or revoke consent to send text to TypeSafe for the Jev checks. \"show\" returns "
                        "exactly what is sent. Consent cannot be granted through this tool: only the user can "
                        "grant it, by running `agiw-mcp consent grant` in a terminal."),
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["show", "revoke"]}},
            "required": ["action"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "agiw_jev_pre",
        "title": "Jev pre-analysis (advisory)",
        "description": ("Send a task description (at most 12 KB) to Jev at TypeSafe with fixed questions about risk, "
                        "blockers, missing detail, irreversible actions and reliance on old reports. Returns Jev's "
                        "answers and which are flagged. Advisory, not a verdict. Refused unless consent is recorded "
                        "and a key is available. Never retried automatically."),
        "inputSchema": {"type": "object", "properties": {"task": {"type": "string", "maxLength": 12288}},
                        "required": ["task"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                        "openWorldHint": True},
    },
    {
        "name": "agiw_jev_post",
        "title": "Jev post-analysis (advisory)",
        "description": ("Send a result or report (truncated to 12 KB) to Jev at TypeSafe with fixed questions about "
                        "self-contradiction, unbacked claims, risk of acting on it and misclassified findings. "
                        "Advisory, not a verdict. Refused unless consent is recorded and a key is available. Never "
                        "retried automatically."),
        "inputSchema": {"type": "object", "properties": {"report": {"type": "string"}},
                        "required": ["report"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                        "openWorldHint": True},
    },
    {
        "name": "agiw_nisi_run",
        "title": "Run one Nisi workflow locally",
        "description": ("Run one Nisi v0.2.0 workflow (draft, syntax check, tests, independent review, bounded "
                        "repair) on file CONTENTS you pass in, using the configured local author and reviewer "
                        "models. Runs in a private temporary directory that is removed afterwards. Never reads or "
                        "writes project files and never executes generated code; returns proposed contents and "
                        "Nisi's report for you to review and apply yourself. Tests are the built-in checks (some "
                        "file changed, no empty file) plus optional contains/not_contains assertions."),
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string", "maxLength": nisi.MAX_TASK_CHARS,
                     "description": "What to change, in plain language."},
            "files": {"type": "array", "maxItems": nisi.MAX_FILES, "items": _FILE,
                      "description": "Current contents of the files the models may change (relative paths)."},
            "new_files": {"type": "array", "maxItems": nisi.MAX_FILES, "items": {"type": "string"},
                          "description": "Relative paths the author may create."},
            "acceptance_criteria": {"type": "array", "maxItems": nisi.MAX_CRITERIA,
                                    "items": {"type": "string", "maxLength": nisi.MAX_CRITERION_CHARS}},
            "assertions": {"type": "array", "maxItems": nisi.MAX_ASSERTIONS, "items": _ASSERTION},
            "repair_budget": {"type": "integer", "minimum": 0, "maximum": 3, "default": 1},
            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 1800,
                                "default": nisi.DEFAULT_TIMEOUT_SECONDS},
            "ticket": _TICKET},
            "required": ["task"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)


def _config() -> Config:
    try:
        return load_config()
    except ConfigError as exc:
        raise ToolError(exc.code, exc.message + " Run agiw_readiness for the fix.") from None


def _no_args(args: dict) -> None:
    if args:
        raise ToolError("INPUT_INVALID", "This tool takes no arguments.")


def _only(args: dict, allowed: set[str]) -> None:
    extra = sorted(set(args) - allowed)
    if extra:
        raise ToolError("INPUT_INVALID", "Unknown arguments: " + ", ".join(extra[:5]) + ".")


def readiness_tool(args: dict) -> dict:
    _no_args(args)
    return readiness.evaluate(stop_at_first=True)


def status_tool(args: dict) -> dict:
    _no_args(args)
    try:
        config_info = load_config().public()
        config_error = None
    except ConfigError as exc:
        config_info, config_error = None, exc.message
    node_path, node_version = nisi.find_node()
    try:
        nisi_info = nisi.verify_vendor()
        nisi_info["verified"] = True
    except nisi.NisiError as exc:
        nisi_info = {"version": nisi.NISI_VERSION, "verified": False, "code": exc.code}
    checks = readiness.evaluate(stop_at_first=False)["checks"]
    return {
        "server": {"name": SERVER_NAME, "version": __version__},
        "python": {"version": platform.python_version(), "executable": display_path(sys.executable)},
        "platform": sys.platform,
        "node": {"path": display_path(node_path) if node_path else None,
                 "version": ".".join(map(str, node_version)) if node_version else None},
        "nisi": nisi_info,
        "configDir": display_path(config_dir()),
        "configFile": display_path(config_file_path()),
        "config": config_info,
        "configError": config_error,
        "jevEndpoint": consent.JEV_ENDPOINT,
        "keyEnvironmentVariableSet": bool(os.environ.get(keys.ENV_VAR)),
        "prerequisites": {row["id"]: row["ok"] for row in checks},
        "details": checks,
    }


def consent_tool(args: dict) -> dict:
    _only(args, {"action"})
    action = args.get("action")
    if action == "show":
        return consent.show()
    if action == "revoke":
        return consent.revoke()
    if action == "grant":
        raise ToolError("CONSENT_USER_ONLY", "Consent cannot be granted through this tool. Show the user the "
                        "disclosure; if they agree, they run `agiw-mcp consent grant` in a terminal themselves.")
    raise ToolError("INPUT_INVALID", "action must be show or revoke.")


def jev_pre_tool(args: dict) -> dict:
    _only(args, {"task"})
    return jev.analyse("pre", args.get("task"), _config())


def jev_post_tool(args: dict) -> dict:
    _only(args, {"report"})
    return jev.analyse("post", args.get("report"), _config())


def nisi_tool(args: dict) -> dict:
    return nisi.run(args, _config())


HANDLERS = {
    "agiw_readiness": readiness_tool,
    "agiw_status": status_tool,
    "agiw_consent": consent_tool,
    "agiw_jev_pre": jev_pre_tool,
    "agiw_jev_post": jev_post_tool,
    "agiw_nisi_run": nisi_tool,
}


def call_tool(name: str, args: dict) -> tuple[dict, bool]:
    """Return (structured result, is_error). Unexpected exceptions become a fixed error."""
    try:
        return HANDLERS[name](args), False
    except ToolError as exc:
        return exc.payload(), True
    except Exception:  # noqa: BLE001 - never surface exception text to the client
        return ToolError("INTERNAL_ERROR", "The tool failed unexpectedly. No details are reported.").payload(), True
