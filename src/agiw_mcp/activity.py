# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""A local record of Jev calls: hashes, sizes and outcome codes only.

Nothing here is sent anywhere. Fields are allowlisted and typed, so task or
report text cannot reach the file. A write failure never fails the tool.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

from .config import config_dir, ensure_private_dir

LOG_FILE = "jev-activity.jsonl"
MAX_LOG_BYTES = 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[A-Z0-9_]{1,64}\Z")
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


def record(**fields) -> bool:
    entry = {"at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    for key, value in fields.items():
        check = _FIELDS.get(key)
        if check is None or not check(value):
            return False
        entry[key] = value
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
