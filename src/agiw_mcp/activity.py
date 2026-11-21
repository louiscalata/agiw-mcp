# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Local records of Jev calls and, when enabled, Nisi runs: hashes, sizes, counts and codes only.

Nothing here is sent anywhere. Fields are allowlisted and typed, so task or
report text cannot reach a file. A write failure never fails the tool.

The Jev log (jev-activity.jsonl) is always written. The Nisi log (nisi-activity.jsonl) is written only
when config.json sets nisi_activity_log to true; the caller decides, this module just refuses anything
that is not on the allowlist. Both rotate to a single ".1" file above MAX_LOG_BYTES. The Nisi log is
opened without following a symlink and only if it is a regular file.

On POSIX each writer serializes rotation and append with flock on its own persistent lock file, so a killed
writer cannot wedge the record. Without fcntl it falls back to an exclusive-create lock file. A failed Nisi
rollback creates nisi-activity.jsonl.damaged; while it exists no Nisi line is written.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

from .config import config_dir, ensure_private_dir

try:
    import fcntl
except ImportError:  # Windows: the exclusive-create fallback below is used instead
    fcntl = None

LOG_FILE = "jev-activity.jsonl"
NISI_LOG_FILE = "nisi-activity.jsonl"
NISI_DAMAGED_SUFFIX = ".damaged"
MAX_LOG_BYTES = 1024 * 1024
_LOCK_ATTEMPTS = 20
_LOCK_DELAY = 0.005
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
        line = json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
        return _record_jev_line(config_dir(), line)
    except (OSError, ValueError):
        return False


class _NisiRollbackError(OSError):
    """A partial write could not be rolled back; the caller writes the damaged marker."""


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


def _take_flock(path: str, kwargs: dict[str, int]) -> int | None:
    """Lock the persistent lock file with flock; the kernel releases it when the holder exits or closes it."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600, **kwargs)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("The activity lock is not a regular file.")
        for attempt in range(_LOCK_ATTEMPTS):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if attempt + 1 < _LOCK_ATTEMPTS:
                    time.sleep(_LOCK_DELAY)
    except BaseException:
        _close_quietly(fd)
        raise
    _close_quietly(fd)
    return None


def _take_lock(path: str, kwargs: dict[str, int]) -> int | None:
    """Fallback without fcntl: an exclusive-create lock file that the holder removes by name."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    for attempt in range(_LOCK_ATTEMPTS):
        try:
            return os.open(path, flags, 0o600, **kwargs)
        except FileExistsError:
            if attempt + 1 < _LOCK_ATTEMPTS:
                time.sleep(_LOCK_DELAY)
    return None


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _record_jev_line(directory: Path, line: str) -> bool:
    """Serialize Jev rotation and append across participating processes."""
    ensure_private_dir(directory)
    path = directory / LOG_FILE
    lock_path = directory / (LOG_FILE + ".lock")
    persistent = fcntl is not None and os.name == "posix"
    lock_fd = _take_flock(str(lock_path), {}) if persistent else _take_lock(str(lock_path), {})
    if lock_fd is None:
        return False
    try:
        try:
            if path.stat().st_size > MAX_LOG_BYTES:
                os.replace(path, directory / (LOG_FILE + ".1"))
        except FileNotFoundError:
            pass
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)
        return True
    finally:
        _close_quietly(lock_fd)
        if not persistent:
            try:
                os.unlink(lock_path)
            except OSError:
                pass


def _mark_nisi_damaged(path: str, kwargs: dict[str, int]) -> bool:
    """Create the damaged marker; True when it exists afterwards. Its contents are empty."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        _close_quietly(os.open(path, flags, 0o600, **kwargs))
    except FileExistsError:
        pass
    except OSError:
        return False
    return True


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
            # Preserve the rollback failure so the caller marks the log as damaged.
            if not rollback_failed:
                raise


def _record_nisi_line(directory: Path, line: bytes) -> str:
    """Return "written", or the reason the line was skipped: "locked", "damaged" or "error"."""
    info = _prepare_nisi_directory(directory)
    directory_fd = None
    try:
        kwargs = {}
        path = str(directory / NISI_LOG_FILE)
        if os.name == "posix":
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
            directory_fd = os.open(directory, flags)
            opened = os.fstat(directory_fd)
            if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return "error"
            kwargs = {"dir_fd": directory_fd}
            path = NISI_LOG_FILE
        previous = path + ".1"
        lock_path = path + ".lock"
        damaged_path = path + NISI_DAMAGED_SUFFIX
        persistent = fcntl is not None and bool(kwargs)
        lock_fd = _take_flock(lock_path, kwargs) if persistent else _take_lock(lock_path, kwargs)
        if lock_fd is None:
            return "locked"
        remove_lock = not persistent
        try:
            try:
                os.lstat(damaged_path, **kwargs)
                return "damaged"
            except FileNotFoundError:
                pass
            try:
                return "written" if _append_nisi_line(path, previous, kwargs, line) else "error"
            except _NisiRollbackError:
                # Without a marker, the fallback keeps its lock file so later calls still skip the damaged log.
                if not _mark_nisi_damaged(damaged_path, kwargs) and not persistent:
                    remove_lock = False
                return "damaged"
        finally:
            _close_quietly(lock_fd)
            if remove_lock:
                # Separate from the close, so a close error cannot leave the fallback lock behind.
                try:
                    os.unlink(lock_path, **kwargs)
                except OSError:
                    pass
    finally:
        if directory_fd is not None:
            _close_quietly(directory_fd)


def _skipped(reason: str) -> None:
    """One fixed stderr line per skipped record; it never carries request content, paths or fingerprints."""
    try:
        sys.stderr.write(f"agiw-mcp: nisi activity record skipped ({reason})\n")
        sys.stderr.flush()
    except (OSError, ValueError, AttributeError):
        pass


def record_nisi(**fields) -> bool:
    """Append one typed Nisi run line; True only when the line was written.

    A busy lock, the damaged marker or a write failure returns False without failing the run and prints one
    fixed line to stderr. After a partial write that cannot be rolled back, nisi-activity.jsonl.damaged is
    created and every later call skips the record until an operator inspects or removes the log and deletes
    that marker."""
    entry = _entry(fields, _NISI_FIELDS, complete=True)
    if entry is None:
        return False
    line = (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        status = _record_nisi_line(config_dir(), line)
    except (OSError, ValueError):
        status = "error"
    if status == "written":
        return True
    _skipped(status)
    return False
