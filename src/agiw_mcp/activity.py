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
from pathlib import Path
import re
import stat
import time

from .config import config_dir, ensure_private_dir

LOG_FILE = "jev-activity.jsonl"
NISI_LOG_FILE = "nisi-activity.jsonl"
MAX_LOG_BYTES = 1024 * 1024
_NISI_LOCK_ATTEMPTS = 20
_NISI_LOCK_DELAY = 0.005
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


class _NisiRollbackError(OSError):
    """A failed rollback leaves the lock in place until the damaged log is repaired."""


def _prepare_nisi_directory(directory: Path) -> os.stat_result:
    """Create missing directories privately, preserving existing modes and refusing a linked config directory."""
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        _prepare_nisi_directory(directory.parent)
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode):
        raise OSError("The activity directory is not a directory.")
    return info


def _take_nisi_lock(path: str, kwargs: dict[str, int]) -> int | None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    for attempt in range(_NISI_LOCK_ATTEMPTS):
        try:
            return os.open(path, flags, 0o600, **kwargs)
        except FileExistsError:
            if attempt + 1 < _NISI_LOCK_ATTEMPTS:
                time.sleep(_NISI_LOCK_DELAY)
    return None


def _append_nisi_line(path: str, previous: str, kwargs: dict[str, int], line: bytes) -> bool:
    # The caller holds the exclusive lock, so this size check and rotation see the latest successful append.
    try:
        info = os.lstat(path, **kwargs)
    except FileNotFoundError:
        info = None
    if info is not None:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return False
        if info.st_size > MAX_LOG_BYTES:
            replace_kwargs = {"src_dir_fd": kwargs["dir_fd"], "dst_dir_fd": kwargs["dir_fd"]} if kwargs else {}
            os.replace(path, previous, **replace_kwargs)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags, 0o600, **kwargs)
    rollback_failed = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return False
        try:
            if os.write(fd, line) == len(line):
                return True
        except OSError:
            pass
        try:
            os.ftruncate(fd, info.st_size)
        except OSError as exc:
            rollback_failed = True
            raise _NisiRollbackError("The partial activity write could not be rolled back.") from exc
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            # Preserve the rollback failure so the caller keeps the damaged log locked.
            if not rollback_failed:
                raise


def _record_nisi_line(directory: Path, line: bytes) -> bool:
    info = _prepare_nisi_directory(directory)
    directory_fd = None
    try:
        kwargs = {}
        path = str(directory / NISI_LOG_FILE)
        previous = path + ".1"
        lock_path = path + ".lock"
        if os.name == "posix":
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
            directory_fd = os.open(directory, flags)
            opened = os.fstat(directory_fd)
            if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return False
            kwargs = {"dir_fd": directory_fd}
            path = NISI_LOG_FILE
            previous = path + ".1"
            lock_path = path + ".lock"
        lock_fd = _take_nisi_lock(lock_path, kwargs)
        if lock_fd is None:
            return False
        release_lock = True
        try:
            return _append_nisi_line(path, previous, kwargs, line)
        except _NisiRollbackError:
            release_lock = False
            return False
        finally:
            os.close(lock_fd)
            if release_lock:
                os.unlink(lock_path, **kwargs)
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def record_nisi(**fields) -> bool:
    """Append one typed Nisi run line; a busy lock or write failure returns False without failing the run."""
    entry = _entry(fields, _NISI_FIELDS, complete=True)
    if entry is None:
        return False
    line = (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        return _record_nisi_line(config_dir(), line)
    except (OSError, ValueError):
        return False
