# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Claim tickets: validation and an all-or-nothing admission registry.

A ticket is {ticketId, claimState, writeScope, owner}. Only claimState
"Granted" or "Active" is admissible. writeScope is a non-empty list of scope
keys or '/'-separated paths; two scopes overlap when they are equal or one is
a path prefix of the other on a '/' boundary ("src" overlaps "src/a.py", not
"srcx"). Scopes name paths, and filesystems such as APFS and NTFS ignore case,
so scopes are compared case- and normalization-insensitively: "README.md" and
"readme.MD" are the same scope. Each scope is canonicalized once (NFD,
casefold, NFC) and that form is used for comparison, hashing and the stored
lock records. This can refuse two distinct opaque keys as overlapping, never
the reverse. Win32 also treats "calc.py." and "calc.py " as "calc.py", maps
reserved device names (CON, NUL, COM1, ... with any extension) to devices and
reads ':' as an alternate data stream, so scope parts with any of those are
refused rather than normalized.

admit() checks every requested scope against the scopes already admitted and
registers all of them or none. Threads are serialized by a lock; processes by
an admission lock file and one lock file per scope, each created with
O_CREAT|O_EXCL in a private state directory (AGIW_MCP_TICKET_DIR if set,
otherwise "tickets" under the config directory). On POSIX that directory is
reached by a descriptor walk from "/": every directory consulted on the way,
including the targets of and the directories holding any symlinks, must be
owned by root or the effective user and not group- or other-writable unless
sticky, and every symlink must be owned by root or the effective user. The
state directory itself must be a real directory (not a symlink) owned by the
effective user with mode 0700. Anything else fails closed. The descriptor of
the state directory is kept for each admitted ticket and its locks are
released through it, so a registry renamed or replaced in the meantime is
never touched. Any failure, including a failed write or close of a lock file,
removes the files this call created. Locks left behind by a crashed process
are not cleaned up: they refuse later admissions with a clear error until
removed.

On Windows admission is refused (TICKET_UNSUPPORTED_PLATFORM) before anything
is validated or the filesystem is touched: there is no reparse-point,
ownership or permission check on the state directory yet, so it cannot be
trusted.

Refusals name only the ticket id, never the owner, the state or the scopes.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
import unicodedata

from .config import config_dir, ensure_private_dir
from .errors import ToolError

ADMISSIBLE_STATES = frozenset({"Granted", "Active"})
TICKET_KEYS = frozenset({"ticketId", "claimState", "writeScope", "owner"})
MAX_SCOPES = 64
MAX_SCOPE_CHARS = 256
MAX_OWNER_CHARS = 256
ADMIT_LOCK = "admit.lock"
ADMIT_WAIT_SECONDS = 2.0
MAX_SYMLINKS = 40

_TICKET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
# Win32 device names, reserved in any case and with any extension ("nul.txt", "COM1.tar.gz").
_WINDOWS_DEVICES = frozenset({"con", "prn", "aux", "nul", "conin$", "conout$"}
                             | {f"{name}{digit}" for name in ("com", "lpt") for digit in "123456789¹²³"})
# Descriptor-relative lock operations (POSIX); elsewhere the state directory is used by path.
_DIR_FD = (os.name == "posix" and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
           and {os.open, os.unlink, os.mkdir, os.stat, os.readlink} <= os.supports_dir_fd
           and os.stat in os.supports_follow_symlinks and os.listdir in os.supports_fd)


def _invalid(message: str, ticket_id: str | None = None) -> ToolError:
    details = {"ticketId": ticket_id} if ticket_id else {}
    return ToolError("TICKET_INVALID", message, **details)


def _refuse_unsupported_platform() -> None:
    """Windows admission is disabled: the state directory's reparse points, owner and ACL are not checked."""
    if os.name == "nt" or sys.platform == "win32":
        raise ToolError("TICKET_UNSUPPORTED_PLATFORM", "Ticket admission is disabled on Windows until the "
                        "ticket state directory gets a reparse-point, ownership and permission check; "
                        "nothing was admitted.")


def _encodable(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def canonical_scope(key: str) -> str:
    """The case- and normalization-insensitive identity of a scope (canonical caseless form)."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", key).casefold())


def _part_ok(part: str) -> bool:
    """A canonical scope part that names one file the same way on every supported filesystem."""
    if part in ("", ".", "..") or part != part.strip() or part.endswith((".", " ")) or ":" in part:
        return False
    return part.split(".", 1)[0].rstrip(" ") not in _WINDOWS_DEVICES


def _normalize_scope(value) -> str | None:
    """Canonicalize, strip surrounding '/' and refuse malformed or Win32-aliased parts; None when malformed."""
    if (not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > MAX_SCOPE_CHARS
            or "\\" in value or any(ord(c) < 32 for c in value) or not _encodable(value)):
        return None
    key = canonical_scope(value).strip("/")
    if not key or len(key) > MAX_SCOPE_CHARS or "\\" in key or any(ord(c) < 32 for c in key):
        return None
    if not all(_part_ok(part) for part in key.split("/")):
        return None
    return key


def validate(ticket) -> dict:
    """Return a normalized ticket or raise TICKET_INVALID / TICKET_NOT_ADMISSIBLE.

    writeScope comes back as canonical scopes (see canonical_scope)."""
    if not isinstance(ticket, dict) or not ticket:
        raise _invalid("ticket must be an object with ticketId, claimState, writeScope and owner.")
    ticket_id = ticket.get("ticketId")
    if not isinstance(ticket_id, str) or not _TICKET_ID.fullmatch(ticket_id):
        raise _invalid("ticket.ticketId must be 1 to 128 letters, digits, '.', '_', ':' or '-'.")
    if set(ticket) != TICKET_KEYS:
        raise _invalid("ticket must have exactly ticketId, claimState, writeScope and owner.", ticket_id)
    owner = ticket["owner"]
    if (not isinstance(owner, str) or not owner.strip() or len(owner) > MAX_OWNER_CHARS or "\x00" in owner
            or not _encodable(owner)):
        raise _invalid("ticket.owner must be non-empty text.", ticket_id)
    state = ticket["claimState"]
    if not isinstance(state, str) or not state.strip():
        raise _invalid("ticket.claimState must be non-empty text.", ticket_id)
    scope = ticket["writeScope"]
    if not isinstance(scope, list) or not 1 <= len(scope) <= MAX_SCOPES:
        raise _invalid(f"ticket.writeScope must be a list of 1 to {MAX_SCOPES} scope keys or paths.", ticket_id)
    keys = []
    for item in scope:
        key = _normalize_scope(item)
        if key is None:
            raise _invalid("Each ticket.writeScope entry must be a non-empty key or relative path without '.', "
                           "'..' or empty parts, parts ending in '.' or space, ':', or Windows device names.",
                           ticket_id)
        if key not in keys:
            keys.append(key)
    if state not in ADMISSIBLE_STATES:
        raise ToolError("TICKET_NOT_ADMISSIBLE", "The ticket is not admissible; only 'Granted' or 'Active' "
                        "tickets may run.", ticketId=ticket_id)
    return {"ticketId": ticket_id, "claimState": state, "writeScope": tuple(keys), "owner": owner}


def overlaps(a: str, b: str) -> bool:
    a, b = canonical_scope(a), canonical_scope(b)
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def state_dir() -> Path:
    override = os.environ.get("AGIW_MCP_TICKET_DIR")
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ToolError("TICKET_DIR_INVALID", "AGIW_MCP_TICKET_DIR must be an absolute path.")
        return path
    return config_dir() / "tickets"


def _scope_name(key: str) -> str:
    return "scope-" + hashlib.sha256(canonical_scope(key).encode("utf-8")).hexdigest() + ".lock"


def _unsafe(ticket_id: str) -> ToolError:
    return ToolError("TICKET_REGISTRY_UNSAFE", "The ticket state directory must be a directory (not a symlink) "
                     "owned by this user with mode 0700, below directories that only root or this user can "
                     "change; nothing was admitted.", ticketId=ticket_id)


def _trusted_owner(info: os.stat_result) -> bool:
    return info.st_uid in (0, os.geteuid())


def _secure_ancestor(info: os.stat_result) -> bool:
    """A directory no other user can rename entries in: trusted owner, not group/other-writable unless sticky."""
    mode = info.st_mode
    return (stat.S_ISDIR(mode) and _trusted_owner(info)
            and (not mode & (stat.S_IWGRP | stat.S_IWOTH) or bool(mode & stat.S_ISVTX)))


def _open_child(parent: int, name: str) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _walk_parent(parts: list[str], ticket_id: str) -> int:
    """Open the directory named by ``parts`` (from "/") with every step validated; returns its descriptor.

    Symlinks are resolved here, one component at a time, so the directory holding each link and every
    directory its target passes through are validated too. Missing directories are created (0700)
    inside directories that already passed. Raises TICKET_REGISTRY_UNSAFE or OSError."""
    pending = list(reversed(parts))
    links = 0
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        if not _secure_ancestor(os.fstat(fd)):
            raise _unsafe(ticket_id)
        while pending:
            name = pending.pop()
            if name in ("", "."):
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if name == "..":
                    raise
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                links += 1
                if links > MAX_SYMLINKS or not _trusted_owner(info):
                    raise _unsafe(ticket_id)
                target = os.readlink(name, dir_fd=fd)
                pending.extend(reversed(target.split("/")))
                if target.startswith("/"):
                    os.close(fd)
                    fd = -1
                    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
                    if not _secure_ancestor(os.fstat(fd)):
                        raise _unsafe(ticket_id)
                continue
            child = _open_child(fd, name)
            os.close(fd)
            fd = child
            if not _secure_ancestor(os.fstat(fd)):
                raise _unsafe(ticket_id)
        return fd
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise


def _check_private(fd: int, ticket_id: str) -> os.stat_result:
    """The state directory itself: a directory owned by this user with mode 0700 (tightened if possible)."""
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise _unsafe(ticket_id)
    if stat.S_IMODE(info.st_mode) != 0o700:
        try:
            os.fchmod(fd, 0o700)
            info = os.fstat(fd)
        except OSError:
            raise _unsafe(ticket_id) from None
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise _unsafe(ticket_id)
    return info


def _open_dir(root: Path, ticket_id: str) -> int | None:
    """Create ``root`` if needed and return a descriptor for it once it and its ancestry are verified.

    None where descriptor-relative operations are unavailable: there ``root`` is used by path. (Windows
    never gets here; admit() refuses it first.)"""
    try:
        if not _DIR_FD:
            ensure_private_dir(root)
            return None
        parts = Path(os.path.normpath(root)).parts[1:]
        if not parts or parts[-1] in (".", ".."):
            raise _unsafe(ticket_id)
        parent = _walk_parent(list(parts[:-1]), ticket_id)
        try:
            try:
                os.mkdir(parts[-1], 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            fd = _open_child(parent, parts[-1])
        finally:
            os.close(parent)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _unsafe(ticket_id) from None
        raise ToolError("TICKET_REGISTRY_UNAVAILABLE", "The ticket state directory could not be created; "
                        "nothing was admitted.", ticketId=ticket_id) from None
    try:
        _check_private(fd, ticket_id)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _identity(root: Path, dir_fd: int | None) -> tuple[int, int] | None:
    try:
        info = os.fstat(dir_fd) if dir_fd is not None else os.stat(root)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def _at(root: Path, dir_fd: int | None, name: str) -> tuple[str, dict]:
    return (name, {"dir_fd": dir_fd}) if dir_fd is not None else (str(root / name), {})


def _create_exclusive(root: Path, dir_fd: int | None, name: str, data: bytes) -> None:
    """Create ``name`` exclusively holding all of ``data``; on any failure the created file is removed."""
    target, at = _at(root, dir_fd, name)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, **at)
    done = False
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError(errno.EIO, "lock file write made no progress")
                view = view[written:]
        finally:
            os.close(fd)
        done = True
    finally:
        if not done:
            _unlink(root, dir_fd, name)


def _read(root: Path, dir_fd: int | None, name: str) -> bytes:
    target, at = _at(root, dir_fd, name)
    fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), **at)
    try:
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _unlink(root: Path, dir_fd: int | None, name: str) -> None:
    target, at = _at(root, dir_fd, name)
    try:
        os.unlink(target, **at)
    except FileNotFoundError:
        pass


class _Admission:
    """The lock files of one admitted ticket and the state directory they were created in."""

    def __init__(self, names: list[str], dir_fd: int | None, identity: tuple[int, int] | None):
        self.names = names
        self.dir_fd = dir_fd
        self.identity = identity


class Registry:
    """Admitted tickets backed by lock files under ``root``."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.Lock()
        self._owned: dict[str, _Admission] = {}

    def _held(self, dir_fd: int | None) -> list[tuple[str, str]]:
        """(scope, ticketId) for every scope lock file currently present."""
        held = []
        names = os.listdir(self.root if dir_fd is None else dir_fd)
        for name in sorted(n for n in names if n.startswith("scope-") and n.endswith(".lock")):
            try:
                record = json.loads(_read(self.root, dir_fd, name).decode("utf-8"))
            except FileNotFoundError:
                continue  # released between listing and reading
            except (OSError, UnicodeError, ValueError):
                record = None
            if (not isinstance(record, dict) or not isinstance(record.get("scope"), str)
                    or not isinstance(record.get("ticketId"), str)):
                raise ToolError("TICKET_REGISTRY_CORRUPT", "A ticket scope lock could not be read; nothing was "
                                "admitted. Remove the ticket state directory once no runs are active.")
            held.append((record["scope"], record["ticketId"]))
        return held

    def _acquire_admission(self, dir_fd: int | None, ticket_id: str) -> None:
        deadline = time.monotonic() + ADMIT_WAIT_SECONDS
        while True:
            try:
                _create_exclusive(self.root, dir_fd, ADMIT_LOCK, str(os.getpid()).encode("ascii"))
                return
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ToolError("TICKET_REGISTRY_BUSY", "Another admission holds the ticket registry lock "
                                    "(or a crashed run left it behind); nothing was admitted. Try again, or "
                                    "remove admit.lock from the ticket state directory if no run is active.",
                                    ticketId=ticket_id) from None
                time.sleep(0.01)
            except OSError:
                raise ToolError("TICKET_REGISTRY_UNAVAILABLE", "The ticket registry lock could not be written; "
                                "nothing was admitted.", ticketId=ticket_id) from None

    def admit(self, ticket) -> dict:
        """Validate ``ticket`` and register all of its scopes, or none of them.

        On POSIX the state directory's descriptor stays open until release(). Refused on Windows
        (TICKET_UNSUPPORTED_PLATFORM) before anything else."""
        _refuse_unsupported_platform()
        clean = validate(ticket)
        ticket_id = clean["ticketId"]
        with self._lock:
            if ticket_id in self._owned:
                raise ToolError("TICKET_SCOPE_CONFLICT", "This ticket is already admitted.", ticketId=ticket_id)
            dir_fd = _open_dir(self.root, ticket_id)
            try:
                identity = _identity(self.root, dir_fd)
                if identity is None:
                    raise ToolError("TICKET_REGISTRY_UNAVAILABLE", "The ticket state directory could not be "
                                    "read; nothing was admitted.", ticketId=ticket_id)
                names = self._admit_locked(dir_fd, clean)
            except BaseException:
                if dir_fd is not None:
                    os.close(dir_fd)
                raise
            self._owned[ticket_id] = _Admission(names, dir_fd, identity)
        return clean

    def _admit_locked(self, dir_fd: int | None, clean: dict) -> list[str]:
        ticket_id = clean["ticketId"]
        self._acquire_admission(dir_fd, ticket_id)
        created: list[str] = []
        try:
            for scope_key, holder in self._held(dir_fd):
                if holder == ticket_id or any(overlaps(scope_key, key) for key in clean["writeScope"]):
                    raise ToolError("TICKET_SCOPE_CONFLICT", "The ticket's write scope overlaps a ticket "
                                    "that is already admitted; nothing was admitted.", ticketId=ticket_id)
            for key in clean["writeScope"]:
                name = _scope_name(key)
                data = json.dumps({"scope": key, "ticketId": ticket_id}).encode("utf-8")
                try:
                    _create_exclusive(self.root, dir_fd, name, data)
                except FileExistsError:
                    raise ToolError("TICKET_SCOPE_CONFLICT", "The ticket's write scope overlaps a ticket "
                                    "that is already admitted; nothing was admitted.",
                                    ticketId=ticket_id) from None
                except OSError:
                    raise ToolError("TICKET_REGISTRY_UNAVAILABLE", "A ticket scope lock could not be "
                                    "written; nothing was admitted.", ticketId=ticket_id) from None
                created.append(name)
        except BaseException:
            for name in created:
                _unlink(self.root, dir_fd, name)
            raise
        finally:
            _unlink(self.root, dir_fd, ADMIT_LOCK)
        return created

    def release(self, ticket_id: str) -> bool:
        """Remove the scope locks this registry created for ``ticket_id``. False if it was not admitted.

        Locks are removed only from the directory they were created in: through the descriptor kept
        since admission, or (without descriptor support) by path only while that path still names the
        same directory. If that directory is no longer private or no longer the same, the locks stay:
        they keep refusing overlapping admissions until removed by hand (fail closed)."""
        with self._lock:
            admission = self._owned.pop(ticket_id, None)
            if admission is None:
                return False
            dir_fd = admission.dir_fd
            try:
                if dir_fd is not None:
                    try:
                        _check_private(dir_fd, ticket_id)
                    except ToolError:
                        return True
                elif _identity(self.root, None) != admission.identity:
                    return True
                for name in admission.names:
                    _unlink(self.root, dir_fd, name)
            finally:
                if dir_fd is not None:
                    os.close(dir_fd)
            return True

    def admitted(self) -> list[str]:
        with self._lock:
            return sorted(self._owned)


_REGISTRIES: dict[str, Registry] = {}
_REGISTRIES_LOCK = threading.Lock()


def registry(root: Path | None = None) -> Registry:
    """One Registry per state directory in this process, so its thread lock is shared."""
    path = Path(state_dir() if root is None else root)
    with _REGISTRIES_LOCK:
        key = str(path)
        if key not in _REGISTRIES:
            _REGISTRIES[key] = Registry(path)
        return _REGISTRIES[key]
