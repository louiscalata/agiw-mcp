# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Local records of Jev calls and, when enabled, Nisi runs: hashes, sizes, counts and codes only.

Nothing here is sent anywhere. Fields are allowlisted and typed, so task or
report text cannot reach a file. A write failure never fails the tool.

The Jev log (jev-activity.jsonl) is always written. The Nisi log (nisi-activity.jsonl) is written only
when config.json sets nisi_activity_log to true; the caller decides, this module just refuses anything
that is not on the allowlist. Both rotate to a single ".1" file above MAX_LOG_BYTES. The Nisi log is
opened without following a symlink and only if it is a regular file.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat

from .config import config_dir, ensure_private_dir

LOG_FILE = "jev-activity.jsonl"
NISI_LOG_FILE = "nisi-activity.jsonl"
MAX_LOG_BYTES = 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[A-Z0-9_]{1,64}\Z")
_VERSION = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\Z")
_FIELDS = {
    "tool": lambda v: v in ("agiw_jev_pre", "agiw_jev_post"),
    "inputSha256": lambda v: isinstance(v, str) and bool(_HEX.fullmatch(v)),
    "requestSha256": lambda v: v is None or (isinstance(v, str) and bool(_HEX.fullmatch(v))),
    "inputBytes": lambda v: type(v) is int and v >= 0,
    "sentBytes": lambda v: type(v) is int and v >= 0,
    "truncated": lambda v: type(v) is bool,
    "outcome": lambda v: isinstance(v, str) and bool(_CODE.fullmatch(v)),
    "httpStatus": lambda v: v is None or (type(v) is int and 100 <= v <= 599),
    "flaggedCount": lambda v: v is None or (type(v) is int and v >= 0),
    "elapsedMs": lambda v: type(v) is int and v >= 0,
}
_NISI_FIELDS = {
    "outcome": _FIELDS["outcome"],
    "inputFingerprint": lambda v: v is None or (isinstance(v, str) and bool(_HEX.fullmatch(v))),
    "candidateFingerprint": lambda v: v is None or (isinstance(v, str) and bool(_HEX.fullmatch(v))),
    "repairAttempts": lambda v: v is None or (type(v) is int and 0 <= v <= 1000),
    "sandbox": lambda v: v is None or v in ("node-permission", "none"),
    "ticketed": lambda v: type(v) is bool,
    "nisiVersion": lambda v: isinstance(v, str) and bool(_VERSION.fullmatch(v)),
    "elapsedMs": _FIELDS["elapsedMs"],
}


def _entry(fields: dict, allowed: dict, *, complete: bool) -> dict | None:
    entry = {"at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    for key, value in fields.items():
        check = allowed.get(key)
        if check is None or not check(value):
            return None
        entry[key] = value
    if complete and set(allowed) - set(fields):
        return None
    return entry


def record(**fields) -> bool:
    entry = _entry(fields, _FIELDS, complete=False)
    if entry is None:
        return False
    try:
        directory = config_dir()
        ensure_private_dir(directory)
        path = directory / LOG_FILE
        try:
            if path.stat().st_size > MAX_LOG_BYTES:
                os.replace(path, directory / (LOG_FILE + ".1"))
        except FileNotFoundError:
            pass
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
        return True
    except (OSError, ValueError):
        return False


def record_nisi(**fields) -> bool:
    """Append one Nisi run line. Every field in _NISI_FIELDS is required and typed; returns False otherwise."""
    entry = _entry(fields, _NISI_FIELDS, complete=True)
    if entry is None:
        return False
    line = (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        directory = config_dir()
        ensure_private_dir(directory)
        path = directory / NISI_LOG_FILE
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            info = None
        if info is not None:
            if not stat.S_ISREG(info.st_mode):
                return False  # a symlink, pipe or directory is never opened for append
            if info.st_size > MAX_LOG_BYTES:
                os.replace(path, directory / (NISI_LOG_FILE + ".1"))
        fd = os.open(str(path), flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return False
            return os.write(fd, line) == len(line)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return False
