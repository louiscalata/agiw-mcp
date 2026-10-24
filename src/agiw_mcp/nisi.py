# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Run one vendored Nisi v0.2.0 workflow on caller-supplied file contents.

The caller passes file contents, not paths. agiw-mcp never reads or writes the
user's project. Each run gets a private temporary directory (mode 0700) that
receives a hash-verified copy of Nisi and the runner, and is removed afterwards.
Node runs there with its permission model limiting file access to that
directory where the installed Node supports it.
"""
from __future__ import annotations

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

from .config import Config
from .errors import ToolError

PACKAGE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = PACKAGE_DIR / "vendor" / "nisi"
RUNNER = PACKAGE_DIR / "nisi_runner.mjs"
MANIFEST_SHA256 = "d7cd9f31365ad47f01ccfbfaf2cf29923830abf642c60518cbeb1b9776d8cd23"
NISI_VERSION = "0.2.0"
MIN_NODE = (22, 0, 0)
RUNNER_KIND = "agiw-mcp.nisi-run.v1"
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
_LANGUAGES = {"py": "python", "js": "javascript", "mjs": "javascript", "cjs": "javascript", "ts": "typescript",
              "tsx": "typescript", "json": "json", "md": "markdown", "swift": "swift", "go": "go", "rs": "rust",
              "sh": "shell", "toml": "toml", "yaml": "yaml", "yml": "yaml", "html": "html", "css": "css"}
FRAMING_CRITERION = ("Carry out the request in task.request, starting from the contents in task.currentFiles and "
                     "proposing complete contents only for paths in task.allowedFiles.")


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
        if (not isinstance(name, str) or not _SAFE_NAME.fullmatch(name)
                or any(part in (".", "..") for part in name.split("/"))
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
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
    except NisiError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise NisiError("NISI_UNREADABLE") from None
    return {"version": manifest["version"], "files": len(manifest["files"]),
            "sourceCommit": manifest.get("source_commit"), "license": manifest.get("license")}


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

def find_node() -> tuple[str | None, tuple[int, int, int] | None]:
    path = shutil.which("node")
    if not path:
        return None, None
    try:
        done = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return path, None
    match = re.fullmatch(rb"v(\d+)\.(\d+)\.(\d+)\s*", done.stdout or b"")
    if done.returncode != 0 or not match:
        return path, None
    return path, tuple(int(part) for part in match.groups())


_FLAG_CACHE: dict[str, list[str] | None] = {}


def permission_flags(node: str, workdir: str) -> tuple[list[str], str]:
    """Return Node permission-model flags for this Node, or none if unsupported."""
    if node not in _FLAG_CACHE:
        try:
            done = subprocess.run([node, "--help"], stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=5, check=False)
            text = (done.stdout or b"").decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            text = ""
        # Node 22.13+ lists "--experimental-permission, --permission" on one line; prefer the stable name.
        if re.search(r"(?<![\w-])--permission\b", text):
            base = ["--permission"]
        elif "--experimental-permission" in text:
            base = ["--experimental-permission"]
        else:
            base = None
        if base is not None and "--allow-net" in text:
            base = base + ["--allow-net"]
        _FLAG_CACHE[node] = base
    base = _FLAG_CACHE[node]
    if base is None:
        return [], "none"
    return base + [f"--allow-fs-read={workdir}", f"--allow-fs-write={workdir}", "--allow-child-process"], \
        "node-permission"


# --- input validation ------------------------------------------------------

def _path_ok(path) -> bool:
    if not isinstance(path, str) or not 1 <= len(path) <= 256 or "\\" in path or path.startswith("/"):
        return False
    parts = path.split("/")
    return all(_PATH_PART.fullmatch(part) and part not in (".", "..") for part in parts)


def _text(value, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ToolError("INPUT_INVALID", f"{name} must be non-empty text of at most {limit} characters.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ToolError("INPUT_INVALID", f"{name} must be valid Unicode.") from None
    return value


def validate_request(args: dict) -> dict:
    allowed_keys = {"task", "files", "new_files", "acceptance_criteria", "assertions", "repair_budget",
                    "timeout_seconds"}
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
            raise ToolError("INPUT_INVALID", "File paths must be relative, use '/', and have no '.' or '..' parts.")
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
    criteria = args.get("acceptance_criteria", [])
    if not isinstance(criteria, list) or len(criteria) > MAX_CRITERIA:
        raise ToolError("INPUT_INVALID", f"acceptance_criteria must be a list of at most {MAX_CRITERIA} items.")
    criteria = [_text(c, "Each acceptance criterion", MAX_CRITERION_CHARS) for c in criteria]
    assertions = args.get("assertions", [])
    if not isinstance(assertions, list) or len(assertions) > MAX_ASSERTIONS:
        raise ToolError("INPUT_INVALID", f"assertions must be a list of at most {MAX_ASSERTIONS} items.")
    clean_assertions = []
    for item in assertions:
        if (not isinstance(item, dict) or len(item) != 2 or "path" not in item
                or not ({"contains", "not_contains"} & set(item))):
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
    return {"task": task, "files": clean_files, "allowed": allowed, "criteria": criteria,
            "assertions": clean_assertions, "repair_budget": budget, "timeout": timeout,
            "language": languages.pop() if len(languages) == 1 else "mixed"}


# --- bounded process -------------------------------------------------------

def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass


def _bounded_run(argv: list[str], *, data: bytes, cwd: str, env: dict, timeout: float) -> tuple[int | None, bytes, str]:
    """Run a child with capped output and a wall-clock limit. Returns (code, stdout, state)."""
    extra = {"start_new_session": True} if os.name == "posix" else {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=cwd, env=env, **extra)
    out = bytearray()
    overflow = threading.Event()

    def pump(stream, keep: bool, cap: int):
        size = 0
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > cap:
                    overflow.set()
                    _kill_tree(proc)
                    break
                if keep:
                    out.extend(chunk)
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

    threads = [threading.Thread(target=pump, args=(proc.stdout, True, STDOUT_CAP), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, False, STDERR_CAP * 16), daemon=True),
               threading.Thread(target=feed, daemon=True)]
    for thread in threads:
        thread.start()
    state = "exited"
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        state, code = "timeout", None
    finally:
        _kill_tree(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for thread in threads:
            thread.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
    if overflow.is_set():
        state = "overflow"
    return code, bytes(out), state


# --- run --------------------------------------------------------------------

def _child_env(node: str, workdir: str) -> dict:
    path_dirs = [os.path.dirname(node)] + (["/usr/bin", "/bin"] if os.name == "posix" else [])
    env = {"PATH": os.pathsep.join(path_dirs), "HOME": workdir, "TMPDIR": workdir, "TMP": workdir,
           "TEMP": workdir, "LANG": "C.UTF-8", "NODE_NO_WARNINGS": "1"}
    for name in ("SYSTEMROOT", "WINDIR"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _parse_output(raw: bytes, request: dict) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise ToolError("NISI_RUNNER_FAILED", "The Nisi runner did not return a valid result.") from None
    if not isinstance(value, dict) or value.get("kind") != RUNNER_KIND:
        raise ToolError("NISI_RUNNER_FAILED", "The Nisi runner did not return a valid result.")
    if "error" in value:
        code = value["error"] if isinstance(value["error"], str) and len(value["error"]) <= 64 else "RUNNER_FAILED"
        raise ToolError("NISI_RUNNER_FAILED", "The Nisi runner refused the request.", runnerCode=code)
    report = value.get("report")
    if not isinstance(report, dict) or report.get("outcome") not in OUTCOMES:
        raise ToolError("NISI_RUNNER_FAILED", "The Nisi runner did not return a valid report.")
    return value


def run(args: dict, config: Config, *, temp_parent: str | None = None) -> dict:
    request = validate_request(args)
    if not config.author_model or not config.reviewer_model:
        raise ToolError("MODELS_NOT_CONFIGURED", "Configure author_model and reviewer_model first; "
                        "agiw_readiness shows the exact fix.")
    if config.author_model == config.reviewer_model:
        raise ToolError("MODELS_NOT_DISTINCT", "author_model and reviewer_model must be different models.")
    node, version = find_node()
    if not node or not version or version < MIN_NODE:
        raise ToolError("NODE_UNAVAILABLE", "Node.js 22 or newer must be on the PATH of the MCP client.")
    try:
        verify_vendor()
    except NisiError as exc:
        raise ToolError("NISI_UNVERIFIED", "The vendored Nisi package failed verification; reinstall agiw-mcp.",
                        nisiCode=exc.code) from None
    criteria = [FRAMING_CRITERION] + request["criteria"]
    payload = {
        "schema": 1, "taskText": request["task"], "files": request["files"], "allowedFiles": request["allowed"],
        "acceptanceCriteria": criteria, "assertions": request["assertions"],
        "repairBudget": request["repair_budget"], "deadlineMs": request["timeout"] * 1000,
        "endpoint": config.chat_endpoint, "authorModel": config.author_model,
        "reviewerModel": config.reviewer_model, "maxOutputTokens": config.max_output_tokens,
        "language": request["language"], "python": sys.executable or "", "checkTimeoutMs": 15000,
    }
    workdir = tempfile.mkdtemp(prefix="agiw-mcp-nisi-", dir=temp_parent)
    try:
        if os.name == "posix":
            os.chmod(workdir, 0o700)
        workdir = os.path.realpath(workdir)
        try:
            _copy_verified(VENDOR_DIR, Path(workdir))
        except NisiError as exc:
            raise ToolError("NISI_UNVERIFIED", "The vendored Nisi package failed verification.",
                            nisiCode=exc.code) from None
        flags, sandbox = permission_flags(node, workdir)
        argv = [node, *flags, os.path.join(workdir, "runner.mjs")]
        try:
            code, raw, state = _bounded_run(argv, data=json.dumps(payload).encode("utf-8"), cwd=workdir,
                                            env=_child_env(node, workdir), timeout=request["timeout"] + 30)
        except OSError:
            raise ToolError("NODE_UNAVAILABLE", "Node.js could not be started.") from None
        if state == "timeout":
            raise ToolError("NISI_TIMEOUT", "The Nisi run did not finish in time and was stopped.")
        if state == "overflow":
            raise ToolError("NISI_OUTPUT_TOO_LARGE", "The Nisi runner produced too much output and was stopped.")
        result = _parse_output(raw, request)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return _summarize(result, request, sandbox)


def _summarize(result: dict, request: dict, sandbox: str) -> dict:
    report = result["report"]
    candidate = report.get("candidate") if isinstance(report.get("candidate"), dict) else None
    original = {item["path"]: item["content"] for item in request["files"]}
    proposed, total = [], 0
    for item in (candidate or {}).get("files", []):
        if (not isinstance(item, dict) or item.get("path") not in request["allowed"]
                or not isinstance(item.get("content"), str)):
            raise ToolError("NISI_RUNNER_FAILED", "The Nisi report contained an out-of-scope file.")
        total += len(item["content"].encode("utf-8", "replace"))
        status = "new" if item["path"] not in original else (
            "unchanged" if original[item["path"]] == item["content"] else "modified")
        proposed.append({"path": item["path"], "status": status, "content": item["content"]})
    if total > MAX_OUTPUT_CONTENT_BYTES:
        raise ToolError("NISI_OUTPUT_TOO_LARGE", "The proposed files exceed 2 MB and were not returned.")
    stages = []
    for stage in report.get("stages", []):
        if not isinstance(stage, dict):
            continue
        evidence = stage.get("evidence") if isinstance(stage.get("evidence"), dict) else {}
        row = {"stage": stage.get("stage"), "status": stage.get("status"), "code": stage.get("code"),
               "attempt": evidence.get("attempt")}
        for key in ("reason", "note", "summary", "findings", "failures", "assertionsExecuted", "assertionsPassed",
                    "reviewerId"):
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
        "candidateFingerprint": report.get("candidateFingerprint"),
        "modelReceipts": result.get("receipts") if isinstance(result.get("receipts"), dict) else {},
        "nisiVersion": NISI_VERSION,
        "sandbox": sandbox,
        "note": ("Proposed file contents only. agiw-mcp did not read or write any project file; review the "
                 "contents and apply them yourself if you accept them. "
                 + ("Nisi reported COMPLETED: every configured check, test and review passed. That is not a "
                    "guarantee of correctness." if completed else
                    "Nisi did not complete; treat any proposed contents as an unaccepted draft.")),
    }
