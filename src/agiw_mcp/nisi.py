# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Run one vendored Nisi v0.3.2 workflow on caller-supplied file contents.

The caller passes file contents, not paths. agiw-mcp never reads or writes the
user's project. Each run gets a private temporary directory (mode 0700) that
receives a hash-verified copy of Nisi plus the host runner, and is removed afterwards.
Node's version and permission probes receive only PATH, LANG=C.UTF-8 and
NODE_NO_WARNINGS=1. A failed permission probe refuses the run with
NODE_UNAVAILABLE. When Node exposes a supported permission flag, the runner
uses it to limit file access to the temporary directory. When Node's help
lists no such flag the run is refused with NODE_SANDBOX_UNAVAILABLE, unless
config allow_unsandboxed_nisi is true; then it proceeds without permission
flags and reports sandbox "none". Both probes first run before the temporary
directory is created, so these refusals leave no directory behind. On that
opt-out path a "no flag" answer is not cached, so `node --help` is asked a
second time when the flags are built, after the directory exists; a failure
there still refuses with NODE_UNAVAILABLE and the directory is removed. A comma in
the resolved temporary path refuses the run with TEMP_DIR_UNSAFE. The runner
also uses a minimal environment.
An optional claim ticket (required when config require_ticket is true) is
validated and admitted before any of this starts and released afterwards. A
ticketed run refused by a Node probe has therefore created its ticket lock
files and removes them again before returning. A configured check_python is
probed in the same interval, with isolated flags and a three-second limit.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from . import activity, tickets
from .config import Config
from .errors import ToolError

PACKAGE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = PACKAGE_DIR / "vendor" / "nisi"
RUNNER = PACKAGE_DIR / "nisi_runner.mjs"
MANIFEST_SHA256 = "53940edc65d014c163bf5e34c8243835cd3c068f860e25b5e814a20fe6aafb87"
NISI_VERSION = "0.3.2"
MIN_NODE = (22, 0, 0)
RUNNER_KIND = "agiw-mcp.nisi-run.v1"
EVENT_KIND = "agiw-mcp.nisi-event.v1"
MAX_PROGRESS_EVENTS = 64
OUTCOMES = frozenset({"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "TIMED_OUT", "REPAIR_LIMIT", "NO_PROGRESS"})

MAX_FILES = 64
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 512 * 1024
MAX_TASK_CHARS = 8000
MAX_CRITERIA = 16
MAX_CRITERION_CHARS = 2048
MAX_ASSERTIONS = 32
MAX_OUTPUT_CONTENT_BYTES = 2 * 1024 * 1024
STDOUT_CAP = 16 * 1024 * 1024
STDERR_CAP = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 600

_PATH_PART = re.compile(r"[A-Za-z0-9_@+-][A-Za-z0-9._@+-]{0,127}\Z|\.[A-Za-z0-9_@+-][A-Za-z0-9._@+-]{0,126}\Z")
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")
_LANGUAGES = {
    "py": "python",
    "js": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "json": "json",
    "md": "markdown",
    "swift": "swift",
    "go": "go",
    "rs": "rust",
    "sh": "shell",
    "toml": "toml",
    "yaml": "yaml",
    "yml": "yaml",
    "html": "html",
    "css": "css",
}
FRAMING_CRITERION = (
    "Carry out the request in task.request, starting from the contents in task.currentFiles and "
    "proposing complete contents only for paths in task.allowedFiles."
)


class NisiError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --- vendored package -----------------------------------------------------


def _regular_bytes(path: Path, limit: int = 4 * 1024 * 1024) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise NisiError("NISI_FILE_MISSING")
    data = path.read_bytes()
    if len(data) > limit:
        raise NisiError("NISI_FILE_TOO_LARGE")
    return data


def _manifest(root: Path) -> dict:
    raw = _regular_bytes(root / "manifest.json", 65536)
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise NisiError("NISI_MANIFEST_MISMATCH")
    manifest = json.loads(raw.decode("utf-8"))
    files = manifest.get("files")
    if manifest.get("version") != NISI_VERSION or not isinstance(files, dict) or not files:
        raise NisiError("NISI_MANIFEST_INVALID")
    for name, digest in files.items():
        if (
            not isinstance(name, str)
            or not _SAFE_NAME.fullmatch(name)
            or any(part in (".", "..") for part in name.split("/"))
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise NisiError("NISI_MANIFEST_INVALID")
    return manifest


def verify_vendor(root: Path | None = None) -> dict:
    """Check the pinned manifest, every file hash, and that no other files exist."""
    root = Path(VENDOR_DIR if root is None else root)
    try:
        manifest = _manifest(root)
        expected = {"manifest.json"} | {"package/" + name for name in manifest["files"]}
        found = set()
        for base, dirs, names in os.walk(root, followlinks=False):
            for name in dirs:
                if (Path(base) / name).is_symlink():
                    raise NisiError("NISI_SYMLINK")
            for name in names:
                found.add((Path(base) / name).relative_to(root).as_posix())
        if found != expected:
            raise NisiError("NISI_FILE_SET_MISMATCH")
        for name, digest in manifest["files"].items():
            if hashlib.sha256(_regular_bytes(root / "package" / name)).hexdigest() != digest:
                raise NisiError("NISI_HASH_MISMATCH")
    except (OSError, ValueError, UnicodeError):
        raise NisiError("NISI_UNREADABLE") from None
    return {
        "version": manifest["version"],
        "files": len(manifest["files"]),
        "sourceCommit": manifest.get("source_commit"),
        "license": manifest.get("license"),
    }


def _copy_verified(root: Path, destination: Path) -> None:
    """Copy exactly the manifest's files, hashing the bytes that are written."""
    manifest = _manifest(root)
    target_root = destination / "nisi"
    _write_new(target_root / "manifest.json", _regular_bytes(root / "manifest.json", 65536))
    for name, digest in manifest["files"].items():
        data = _regular_bytes(root / "package" / name)
        if hashlib.sha256(data).hexdigest() != digest:
            raise NisiError("NISI_HASH_MISMATCH")
        _write_new(target_root / "package" / name, data)
    _write_new(destination / "runner.mjs", _regular_bytes(RUNNER, 1024 * 1024))


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


# --- Node discovery ---------------------------------------------------------

_PROBE_WINDOWS_NAMES = ("SystemRoot", "SYSTEMDRIVE", "WINDIR")


def _probe_env() -> dict:
    """Only what Node and syntax-Python probes need: no keys, proxies or other variables.

    On Windows, SystemRoot, SYSTEMDRIVE and WINDIR are also passed through when set (matched
    case-insensitively): without SystemRoot, Node 24 aborts with exit 134 initializing its CSPRNG."""
    env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "NODE_NO_WARNINGS": "1"}
    if os.name == "nt":
        for canonical in _PROBE_WINDOWS_NAMES:
            if os.environ.get(canonical):
                env[canonical] = os.environ[canonical]
                continue
            for key, value in os.environ.items():
                if key.upper() == canonical.upper() and value:
                    env[canonical] = value
                    break
    return env


def _windows_node_exe(path: str) -> str:
    """Use node.exe beside a .cmd or .bat shim. CreateProcess cannot start those shims."""
    if os.name != "nt":
        return path
    root, ext = os.path.splitext(path)
    if ext.lower() not in {".cmd", ".bat"}:
        return path
    exe = root + ".exe"
    if not os.path.isfile(exe):
        exe = os.path.join(os.path.dirname(path), "node.exe")
    return exe if os.path.isfile(exe) else path


def find_node() -> tuple[str | None, tuple[int, int, int] | None]:
    path = shutil.which("node")
    if not path:
        return None, None
    path = _windows_node_exe(path)
    try:
        done = subprocess.run(
            [path, "--version"], stdin=subprocess.DEVNULL, capture_output=True, timeout=3, check=False, env=_probe_env()
        )
    except (OSError, subprocess.SubprocessError):
        return path, None
    match = re.fullmatch(rb"v(\d+)\.(\d+)\.(\d+)\s*", done.stdout or b"")
    if done.returncode != 0 or not match:
        return path, None
    return path, tuple(int(part) for part in match.groups())


def probe_check_python(path: str) -> str:
    """Probe a configured syntax interpreter without exposing its path or output in refusals."""
    try:
        done = subprocess.run(
            [path, "-I", "-S", "-B", "-c", 'import sys; print("%d.%d.%d" % sys.version_info[:3])'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=3,
            check=False,
            env=_probe_env(),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        done = None
    match = re.fullmatch(rb"([0-9]+\.[0-9]+\.[0-9]+)(?:\r?\n)?", done.stdout or b"") if done is not None else None
    if done is None or done.returncode != 0 or match is None:
        raise ToolError(
            "CHECK_PYTHON_UNAVAILABLE",
            "The check_python interpreter could not report X.Y.Z within 3 seconds. "
            "Set check_python in config.json to a working Python interpreter, or remove the key "
            "to use the server's interpreter.",
        ) from None
    return match.group(1).decode("ascii")


_FLAG_CACHE: dict[str, list[str] | None] = {}
_PERMISSION_SWITCHES = frozenset({"--permission", "--experimental-permission"})


def permission_base(node: str) -> list[str] | None:
    """Ask `node --help` which permission-model flags this Node lists; None when it lists none.

    A failed probe is never cached and never means "no sandbox": NODE_UNAVAILABLE is raised instead.
    "Lists none" is not cached either, so a Node replaced in place by one that lists the flag is
    accepted without restarting the server."""
    if node not in _FLAG_CACHE:
        try:
            done = subprocess.run(
                [node, "--help"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
                check=False,
                env=_probe_env(),
            )
        except (OSError, subprocess.SubprocessError):
            done = None
        if done is None or done.returncode != 0:
            raise ToolError(
                "NODE_UNAVAILABLE",
                "Node's permission model could not be checked (node --help "
                "failed), so Nisi was not started without a sandbox. Try again.",
            )
        text = (done.stdout or b"").decode("utf-8", "replace")
        # Node 22.13+ lists "--experimental-permission, --permission" on one line; prefer the stable name.
        if re.search(r"(?<![\w-])--permission\b", text):
            base = ["--permission"]
        elif "--experimental-permission" in text:
            base = ["--experimental-permission"]
        else:
            base = None
        if base is None:
            return None
        if "--allow-net" in text:
            base = base + ["--allow-net"]
        _FLAG_CACHE[node] = base
    return list(_FLAG_CACHE[node])


def sandbox_label(node: str) -> str:
    """The `sandbox` value a run on this Node would report: "node-permission" or "none"."""
    return "none" if permission_base(node) is None else "node-permission"


def _sandbox_unavailable() -> ToolError:
    return ToolError(
        "NODE_SANDBOX_UNAVAILABLE",
        "This Node.js lists no permission-model flag (--permission or "
        "--experimental-permission) in `node --help`, so Nisi was not started: it would run without "
        'a file-access limit. Use a Node.js that lists the flag, or set "allow_unsandboxed_nisi": '
        "true in config.json to run without that limit.",
    )


def permission_flags(node: str, workdir: str) -> tuple[list[str], str]:
    """Return the permission-model flags that confine this Node to workdir, or none if unsupported."""
    base = permission_base(node)
    if base is None:
        return [], "none"
    # Node's Windows permission parser has treated `\` as an escape. `/` names the same path.
    allow = workdir.replace("\\", "/") if os.name == "nt" else workdir
    return base + [f"--allow-fs-read={allow}", f"--allow-fs-write={allow}", "--allow-child-process"], "node-permission"


# --- input validation ------------------------------------------------------


def _path_ok(path) -> bool:
    if not isinstance(path, str) or not 1 <= len(path) <= 256 or "\\" in path or path.startswith("/"):
        return False
    parts = path.split("/")
    return all(_PATH_PART.fullmatch(part) and tickets._part_ok(tickets.canonical_scope(part)) for part in parts)


def _text(value, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ToolError("INPUT_INVALID", f"{name} must be non-empty text of at most {limit} characters.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ToolError("INPUT_INVALID", f"{name} must be valid Unicode.") from None
    return value


def _paths_collide(paths: list[str]) -> bool:
    """True when two paths name the same file, or one is a parent directory of another, on a
    case-insensitive or normalizing filesystem."""
    keys = [tickets.canonical_scope(p) for p in paths]
    if len(set(keys)) != len(keys):
        return True
    taken = set(keys)
    return any("/".join(k.split("/")[:i]) in taken for k in keys for i in range(1, k.count("/") + 1))


def validate_request(args: dict) -> dict:  # noqa: C901 - one check per request field
    if not isinstance(args, dict):
        raise ToolError("INPUT_INVALID", "Arguments must be an object.")
    allowed_keys = {
        "task",
        "files",
        "new_files",
        "acceptance_criteria",
        "assertions",
        "repair_budget",
        "timeout_seconds",
    }
    unknown = sorted(set(args) - allowed_keys)
    if unknown:
        raise ToolError("INPUT_INVALID", "Unknown arguments: " + ", ".join(unknown[:5]) + ".")
    task = _text(args.get("task"), "task", MAX_TASK_CHARS)
    files = args.get("files", [])
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise ToolError("INPUT_INVALID", f"files must be a list of at most {MAX_FILES} objects.")
    seen, clean_files, total = set(), [], 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise ToolError("INPUT_INVALID", "Each file must be an object with exactly path and content.")
        path, content = item["path"], item["content"]
        if not _path_ok(path):
            raise ToolError(
                "INPUT_INVALID",
                "File paths must be relative ASCII paths (letters, digits, ._@+- and /), "
                "without '.', '..', trailing-dot or Windows device-name parts.",
            )
        if path in seen:
            raise ToolError("INPUT_INVALID", "File paths must be unique.")
        if not isinstance(content, str) or "\x00" in content:
            raise ToolError("INPUT_INVALID", "File content must be text without NUL characters.")
        try:
            size = len(content.encode("utf-8"))
        except UnicodeError:
            raise ToolError("INPUT_INVALID", "File content must be valid Unicode.") from None
        if size > MAX_FILE_BYTES:
            raise ToolError("INPUT_TOO_LARGE", f"Each file may be at most {MAX_FILE_BYTES} bytes.")
        total += size
        seen.add(path)
        clean_files.append({"path": path, "content": content})
    if total > MAX_TOTAL_BYTES:
        raise ToolError("INPUT_TOO_LARGE", f"All files together may be at most {MAX_TOTAL_BYTES} bytes.")
    new_files = args.get("new_files", [])
    if not isinstance(new_files, list) or len(new_files) > MAX_FILES or not all(_path_ok(p) for p in new_files):
        raise ToolError("INPUT_INVALID", "new_files must be a list of relative paths.")
    if len(set(new_files)) != len(new_files) or set(new_files) & seen:
        raise ToolError("INPUT_INVALID", "new_files must be unique and must not repeat a path from files.")
    allowed = sorted(seen | set(new_files))
    if not allowed:
        raise ToolError("INPUT_INVALID", "Provide at least one file in files or one path in new_files.")
    if _paths_collide(allowed):
        raise ToolError(
            "INPUT_INVALID",
            "File paths must not differ only by case or Unicode form, and no path may be a directory of another.",
        )
    criteria = args.get("acceptance_criteria", [])
    if not isinstance(criteria, list) or len(criteria) > MAX_CRITERIA:
        raise ToolError("INPUT_INVALID", f"acceptance_criteria must be a list of at most {MAX_CRITERIA} items.")
    criteria = [_text(c, "Each acceptance criterion", MAX_CRITERION_CHARS) for c in criteria]
    assertions = args.get("assertions", [])
    if not isinstance(assertions, list) or len(assertions) > MAX_ASSERTIONS:
        raise ToolError("INPUT_INVALID", f"assertions must be a list of at most {MAX_ASSERTIONS} items.")
    clean_assertions = []
    for item in assertions:
        if (
            not isinstance(item, dict)
            or len(item) != 2
            or "path" not in item
            or not ({"contains", "not_contains"} & set(item))
        ):
            raise ToolError("INPUT_INVALID", "Each assertion is {path, contains} or {path, not_contains}.")
        if item["path"] not in allowed:
            raise ToolError("INPUT_INVALID", "Assertion paths must be in files or new_files.")
        key = "contains" if "contains" in item else "not_contains"
        clean_assertions.append({"path": item["path"], key: _text(item[key], "Assertion text", 2048)})
    budget = args.get("repair_budget", 1)
    if type(budget) is not int or not 0 <= budget <= 3:
        raise ToolError("INPUT_INVALID", "repair_budget must be an integer from 0 to 3.")
    timeout = args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if type(timeout) is not int or not 30 <= timeout <= 1800:
        raise ToolError("INPUT_INVALID", "timeout_seconds must be an integer from 30 to 1800.")
    languages = {_LANGUAGES.get(p.rsplit(".", 1)[-1].lower(), "text") if "." in p else "text" for p in allowed}
    return {
        "task": task,
        "files": clean_files,
        "allowed": allowed,
        "criteria": criteria,
        "assertions": clean_assertions,
        "repair_budget": budget,
        "timeout": timeout,
        "language": languages.pop() if len(languages) == 1 else "mixed",
    }


# --- bounded process -------------------------------------------------------

# Set only by the stdio request worker; direct library calls have no cancellation event or progress callback.
_CANCEL = threading.local()


def _cancelled() -> bool:
    event = getattr(_CANCEL, "event", None)
    return event is not None and event.is_set()


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass


class _RunnerEvents:
    """Read allowlisted event lines without exposing runner text to the client."""

    def __init__(self, callback: Callable[[dict], None], cancel: threading.Event | None) -> None:
        self.callback = callback
        self.cancel = cancel
        self.pending = bytearray()
        self.count = 0
        self.finished = False

    def feed(self, chunk: bytes, *, eof: bool = False) -> None:
        if self.finished or self.count >= MAX_PROGRESS_EVENTS or (self.cancel is not None and self.cancel.is_set()):
            self.pending.clear()
            return
        self.pending.extend(chunk)
        while b"\n" in self.pending:
            raw, _, rest = self.pending.partition(b"\n")
            self.pending = rest
            self._line(raw)
        if eof and self.pending:
            self._line(self.pending)
            self.pending.clear()

    def _line(self, raw: bytes) -> None:
        if self.finished or self.count >= MAX_PROGRESS_EVENTS or (self.cancel is not None and self.cancel.is_set()):
            return
        try:
            # Preserve object members until the closed event schema has checked their count.
            # JSON arrays stay lists, so an array of pairs cannot masquerade as an object.
            pairs = json.loads(raw.decode("utf-8"), object_pairs_hook=tuple)
        except (UnicodeError, ValueError, RecursionError):
            return
        if not isinstance(pairs, tuple):
            return
        value = dict(pairs)
        if value.get("kind") == RUNNER_KIND:
            self.finished = True
            return
        if (
            len(pairs) == 4
            and set(value) == {"kind", "stage", "attempt", "status"}
            and value["kind"] == EVENT_KIND
            and value["stage"] in ("author", "static", "tests", "review")
            and type(value["attempt"]) is int
            and value["status"] in ("started", "finished")
        ):
            self.count += 1
            self.callback(value)


def _bounded_run(  # noqa: C901 - deadline, output caps and cancellation share one loop
    argv: list[str], *, data: bytes, cwd: str, env: dict, timeout: float
) -> tuple[int | None, bytes, str]:
    """Run a child with capped output and a wall-clock limit. Returns (code, stdout, state)."""
    # CREATE_NEW_PROCESS_GROUP can make CreateProcess fail on Windows, and the kill path does not use it.
    extra = {"start_new_session": True} if os.name == "posix" else {}
    if _cancelled():
        raise ToolError("NISI_CANCELLED", "The Nisi request was cancelled.")
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env, **extra
    )
    out = bytearray()
    overflow = threading.Event()
    stdout_eof = threading.Event()
    # Capture the worker's thread-local context here; the pipe reader has its own thread locals.
    progress = getattr(_CANCEL, "progress", None)
    runner_events = _RunnerEvents(progress, getattr(_CANCEL, "event", None)) if progress is not None else None

    def pump(stream, keep: bool, cap: int):
        size = 0
        try:
            while True:
                # read1 returns available pipe bytes instead of waiting for a full 64 KiB buffer.
                chunk = stream.read1(65536)
                if not chunk:
                    if keep:
                        stdout_eof.set()
                        if runner_events is not None:
                            runner_events.feed(b"", eof=True)
                    break
                size += len(chunk)
                if size > cap:
                    overflow.set()
                    _kill_tree(proc)
                    break
                if keep:
                    out.extend(chunk)
                    if runner_events is not None:
                        runner_events.feed(chunk)
        except (OSError, ValueError):
            pass

    def feed():
        try:
            proc.stdin.write(data)
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, True, STDOUT_CAP), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, False, STDERR_CAP * 16), daemon=True),
        threading.Thread(target=feed, daemon=True),
    ]
    for thread in threads:
        thread.start()
    state = "running"
    try:
        deadline = time.monotonic() + timeout
        while True:
            if _cancelled():
                raise ToolError("NISI_CANCELLED", "The Nisi request was cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                code = proc.wait(timeout=min(0.1, remaining))
                state = "exited"
                break
            except subprocess.TimeoutExpired:
                continue
    except subprocess.TimeoutExpired:
        state, code = "timeout", None
    finally:
        if state != "exited":
            _kill_tree(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if state == "exited" and os.name == "posix":
            # Never signal a reaped child that finished cleanly. Only if a pipe is still held open after a short
            # grace does a live group member exist (it keeps the pgid from being reused), so sweep it then.
            grace = time.monotonic() + 1.0
            for thread in threads[:2]:
                thread.join(timeout=max(0.0, grace - time.monotonic()))
            if any(thread.is_alive() for thread in threads[:2]):
                _kill_tree(proc)
        for thread in threads:
            thread.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
    if overflow.is_set():
        state = "overflow"
    elif code == 0 and not stdout_eof.is_set():
        raise ToolError("NISI_RUNNER_FAILED", "The Nisi runner's output could not be read completely.")
    return code, bytes(out), state


# --- run --------------------------------------------------------------------


def _child_env(node: str, workdir: str) -> dict:
    path_dirs = [os.path.dirname(node)] + (["/usr/bin", "/bin"] if os.name == "posix" else [])
    env = {
        "PATH": os.pathsep.join(path_dirs),
        "HOME": workdir,
        "TMPDIR": workdir,
        "TMP": workdir,
        "TEMP": workdir,
        "LANG": "C.UTF-8",
        "NODE_NO_WARNINGS": "1",
    }
    if os.name == "nt":  # Node cannot start on Windows without these; nothing else needs them
        for canonical in _PROBE_WINDOWS_NAMES:
            if os.environ.get(canonical):
                env[canonical] = os.environ[canonical]
                continue
            for key, value in os.environ.items():
                if key.upper() == canonical.upper() and value:
                    env[canonical] = value
                    break
    return env


def _parse_output(raw: bytes, request: dict) -> dict:
    value = None
    for line in raw.splitlines():
        try:
            item = json.loads(line.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            continue
        if isinstance(item, dict) and item.get("kind") == RUNNER_KIND:
            if value is not None:
                raise ToolError("NISI_RUNNER_FAILED", "The Nisi runner returned more than one result.")
            value = item
    if value is None:
        raise ToolError("NISI_RUNNER_FAILED", "The Nisi runner did not return a valid result.")
    if "error" in value:
        code = value["error"] if isinstance(value["error"], str) and len(value["error"]) <= 64 else "RUNNER_FAILED"
        if code == "MODEL_ANSWER_IN_REASONING" and value.get("modelRole") in ("author", "reviewer"):
            model = request[value["modelRole"] + "Model"]
            raise ToolError(
                code,
                f"Model {model!r} returned an answer only in reasoning_content with empty "
                "or missing content. Reasoning was not accepted as an answer. Set reasoning_effort "
                "to 'none' in config.json or set AGIW_MCP_REASONING_EFFORT=none, then retry.",
            )
        raise ToolError("NISI_RUNNER_FAILED", "The Nisi runner refused the request.", runnerCode=code)
    report = value.get("report")
    if not isinstance(report, dict) or report.get("outcome") not in OUTCOMES:
        raise ToolError("NISI_RUNNER_FAILED", "The Nisi runner did not return a valid report.")
    return value


def run(args: dict, config: Config, *, temp_parent: str | None = None) -> dict:
    """Validate, admit the claim ticket (if any or if required), run, and release the ticket.

    Admission happens before Node or check_python is probed or the temporary directory is created, so a refused
    ticket has no side effects."""
    has_ticket = isinstance(args, dict) and "ticket" in args
    if not has_ticket and config.require_ticket:
        raise ToolError(
            "TICKET_REQUIRED",
            "require_ticket is set, so agiw_nisi_run needs an admissible claim "
            "ticket ({ticketId, claimState, writeScope, owner}).",
        )
    ticket = tickets.validate(args["ticket"]) if has_ticket else None
    request = validate_request({k: v for k, v in args.items() if k != "ticket"} if has_ticket else args)
    if not config.nisi_activity_log:
        return _admit_and_run(args, request, ticket, config, temp_parent)
    started = time.monotonic()
    try:
        result = _admit_and_run(args, request, ticket, config, temp_parent)
    except ToolError as exc:
        _log_run(started, ticket is not None, exc.code)
        raise
    except Exception:
        _log_run(started, ticket is not None, "INTERNAL_ERROR")
        raise
    _log_run(started, ticket is not None, result)
    return result


_LOG_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _log_run(started: float, ticketed: bool, outcome) -> None:
    """Append the nisi-activity.jsonl line for one validated call. Never raises: a record is not the run.

    outcome is the summary dict of a finished run, or the refusal code. Only the report outcome, the two
    fingerprints, the repair count and the sandbox label are copied; nothing else of the request or the
    result is read."""
    try:
        fields = {"inputFingerprint": None, "candidateFingerprint": None, "repairAttempts": None, "sandbox": None}
        if isinstance(outcome, dict):
            code = outcome.get("outcome")
            for key in ("inputFingerprint", "candidateFingerprint"):
                value = outcome.get(key)
                fields[key] = value if isinstance(value, str) and _LOG_HEX.fullmatch(value) else None
            repairs = outcome.get("repairAttempts")
            fields["repairAttempts"] = repairs if type(repairs) is int and 0 <= repairs <= 1000 else None
            sandbox = outcome.get("sandbox")
            fields["sandbox"] = sandbox if isinstance(sandbox, str) and sandbox in ("node-permission", "none") else None
        else:
            code = outcome
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", code):
            code = "REFUSED"
        activity.record_nisi(
            outcome=code,
            ticketed=ticketed,
            nisiVersion=NISI_VERSION,
            elapsedMs=int((time.monotonic() - started) * 1000),
            **fields,
        )
    except Exception:  # noqa: BLE001 - the record is best effort and must never change the tool's answer
        pass


def _admit_and_run(args: dict, request: dict, ticket: dict | None, config: Config, temp_parent: str | None) -> dict:
    if not config.author_model or not config.reviewer_model:
        raise ToolError(
            "MODELS_NOT_CONFIGURED",
            "Configure author_model and reviewer_model first; agiw_readiness shows the exact fix.",
        )
    if config.author_model == config.reviewer_model:
        raise ToolError("MODELS_NOT_DISTINCT", "author_model and reviewer_model must be different models.")
    if ticket is None:
        return _run_validated(request, config, temp_parent)
    for path in request["allowed"]:
        canonical = tickets.canonical_scope(path)
        if not any(canonical == scope or canonical.startswith(scope + "/") for scope in ticket["writeScope"]):
            raise ToolError(
                "TICKET_SCOPE_MISMATCH",
                "Every proposed path must be within the ticket writeScope.",
                ticketId=ticket["ticketId"],
            )
    registry = tickets.registry()
    admission = registry.admit(args["ticket"])
    try:
        return _run_validated(request, config, temp_parent)
    finally:
        registry.release(admission["ticketId"])


def _remove_workdir(workspace: tempfile.TemporaryDirectory, primary_error: BaseException | None) -> None:
    try:
        workspace.cleanup()
    except OSError:
        if primary_error is None:
            raise ToolError("NISI_CLEANUP_FAILED", "The Nisi temporary workspace could not be removed.") from None
        if isinstance(primary_error, ToolError):
            primary_error.details["cleanupCode"] = "NISI_CLEANUP_FAILED"


def _run_validated(request: dict, config: Config, temp_parent: str | None) -> dict:
    node, version = find_node()
    if not node or not version or version < MIN_NODE:
        raise ToolError("NODE_UNAVAILABLE", "Node.js 22 or newer must be on the PATH of the MCP client.")
    # Probed here, after ticket admission (see run) and before the temporary directory exists.
    if permission_base(node) is None and not config.allow_unsandboxed_nisi:
        raise _sandbox_unavailable()
    python_version = (
        probe_check_python(config.check_python) if config.check_python is not None else sys.version.split()[0]
    )
    try:
        verify_vendor()
    except NisiError as exc:
        raise ToolError(
            "NISI_UNVERIFIED", "The vendored Nisi package failed verification; reinstall agiw-mcp.", nisiCode=exc.code
        ) from None
    criteria = [FRAMING_CRITERION] + request["criteria"]
    payload = {
        "schema": 1,
        "taskText": request["task"],
        "files": request["files"],
        "allowedFiles": request["allowed"],
        "acceptanceCriteria": criteria,
        "assertions": request["assertions"],
        "repairBudget": request["repair_budget"],
        "deadlineMs": request["timeout"] * 1000,
        "endpoint": config.chat_endpoint,
        "authorModel": config.author_model,
        "reviewerModel": config.reviewer_model,
        "reviewerEndpoint": config.reviewer_chat_endpoint,
        "maxOutputTokens": config.max_output_tokens,
        "reasoningEffort": config.reasoning_effort,
        "language": request["language"],
        "python": config.check_python if config.check_python is not None else (sys.executable or ""),
        "checkTimeoutMs": 15000,
        "pythonVersion": python_version,
    }
    workspace = tempfile.TemporaryDirectory(prefix="agiw-mcp-nisi-", dir=temp_parent)
    workdir = workspace.name
    runner_finished = False
    try:
        if os.name == "posix":
            os.chmod(workdir, 0o700)
        workdir = os.path.realpath(workdir)
        if "," in workdir:
            # Node reads --allow-fs-read/--allow-fs-write as comma-separated lists.
            raise ToolError(
                "TEMP_DIR_UNSAFE",
                "The temporary folder path contains a comma, which would widen "
                "Node's sandbox. Set TMPDIR to a folder without a comma.",
            )
        try:
            _copy_verified(VENDOR_DIR, Path(workdir))
        except NisiError as exc:
            raise ToolError(
                "NISI_UNVERIFIED", "The vendored Nisi package failed verification.", nisiCode=exc.code
            ) from None
        flags, sandbox = permission_flags(node, workdir)
        argv = [node, *flags, os.path.join(workdir, "runner.mjs")]
        confined = sandbox == "node-permission" and bool(_PERMISSION_SWITCHES.intersection(argv[1:-1]))
        if not confined and not config.allow_unsandboxed_nisi:
            # Backstop for the check above, read from the command line itself: never start a runner
            # whose argv has no permission-model switch.
            raise _sandbox_unavailable()
        try:
            code, raw, state = _bounded_run(
                argv,
                data=json.dumps(payload).encode("utf-8"),
                cwd=workdir,
                env=_child_env(node, workdir),
                timeout=request["timeout"] + 30,
            )
        except OSError:
            raise ToolError("NODE_UNAVAILABLE", "Node.js could not be started.") from None
        if state == "timeout":
            raise ToolError("NISI_TIMEOUT", "The Nisi run did not finish in time and was stopped.")
        if state == "overflow":
            raise ToolError("NISI_OUTPUT_TOO_LARGE", "The Nisi runner produced too much output and was stopped.")
        result = _parse_output(raw, payload)
        if code != 0:
            raise ToolError("NISI_RUNNER_FAILED", "The Nisi runner returned a report without a successful exit.")
        runner_finished = True
    finally:
        _remove_workdir(workspace, None if runner_finished else sys.exc_info()[1])
    return _summarize(result, request, sandbox, config.reasoning_effort, python_version)


def _summarize(
    result: dict,
    request: dict,
    sandbox: str,
    reasoning_effort: str | None = None,
    python_version: str | None = None,
) -> dict:
    report = result["report"]
    candidate = report.get("candidate") if isinstance(report.get("candidate"), dict) else None
    original = {item["path"]: item["content"] for item in request["files"]}
    proposed, total = [], 0
    for item in (candidate or {}).get("files", []):
        if (
            not isinstance(item, dict)
            or item.get("path") not in request["allowed"]
            or not isinstance(item.get("content"), str)
        ):
            raise ToolError("NISI_RUNNER_FAILED", "The Nisi report contained an out-of-scope file.")
        total += len(item["content"].encode("utf-8", "replace"))
        status = (
            "new"
            if item["path"] not in original
            else ("unchanged" if original[item["path"]] == item["content"] else "modified")
        )
        proposed.append({"path": item["path"], "status": status, "content": item["content"]})
    if total > MAX_OUTPUT_CONTENT_BYTES:
        raise ToolError("NISI_OUTPUT_TOO_LARGE", "The proposed files exceed 2 MiB and were not returned.")
    stages = []
    for stage in report.get("stages", []):
        if not isinstance(stage, dict):
            continue
        evidence = stage.get("evidence") if isinstance(stage.get("evidence"), dict) else {}
        row = {
            "stage": stage.get("stage"),
            "status": stage.get("status"),
            "code": stage.get("code"),
            "attempt": evidence.get("attempt"),
        }
        for key in (
            "reason",
            "note",
            "summary",
            "findings",
            "failures",
            "assertionsExecuted",
            "assertionsPassed",
            "reviewerId",
        ):
            if key in evidence and evidence[key] not in ("", None):
                row[key] = evidence[key]
        stages.append(row)
    completed = report["outcome"] == "COMPLETED"
    return {
        "outcome": report["outcome"],
        "code": report.get("code"),
        "completed": completed,
        "proposedFiles": proposed,
        "repairAttempts": report.get("repairAttempts"),
        "syntax": result.get("syntax") if isinstance(result.get("syntax"), dict) else {},
        "stages": stages,
        "runId": report.get("runId"),
        "taskFingerprint": report.get("taskFingerprint"),
        "inputFingerprint": result.get("inputFingerprint"),
        "pythonCheckerVersion": python_version if python_version is not None else sys.version.split()[0],
        "candidateFingerprint": report.get("candidateFingerprint"),
        "modelReceipts": result.get("receipts") if isinstance(result.get("receipts"), dict) else {},
        "nisiVersion": NISI_VERSION,
        "reasoningEffort": reasoning_effort,
        "sandbox": sandbox,
        "note": (
            "Proposed file contents only. agiw-mcp did not read or write any project file; review the "
            "contents and apply them yourself if you accept them. "
            + (
                "Nisi reported COMPLETED: every configured check, test and review passed. That is not a "
                "guarantee of correctness."
                if completed
                else "Nisi did not complete; treat any proposed contents as an unaccepted draft."
            )
        ),
    }
