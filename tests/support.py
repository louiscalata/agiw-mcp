# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Shared test helpers. Importing this module installs a loopback-only socket guard.

The suite never reaches the network: every server is a fake bound to 127.0.0.1,
the code under test runs with AGIW_MCP_TEST_MODE=1 (which refuses non-loopback
URLs), and the guard below refuses any socket connection to a non-loopback
address from inside the test process.
"""
from __future__ import annotations

import hashlib
import http.server
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class NetworkBlocked(AssertionError):
    pass


def _is_loopback(address) -> bool:
    if isinstance(address, tuple) and address:
        host = address[0]
    else:
        return True  # Unix sockets and other families are local
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _guarded_connect(self, address):
    if not _is_loopback(address):
        raise NetworkBlocked(f"test attempted a non-loopback connection to {address!r}")
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if not _is_loopback(address):
        raise NetworkBlocked(f"test attempted a non-loopback connection to {address!r}")
    return _real_connect_ex(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if host not in (None, "localhost") and not _is_loopback((host,)):
        raise NetworkBlocked(f"test attempted to resolve {host!r}")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo


CLEARED_ENV = ("TYPESAFE_API_KEY", "AGIW_MCP_MODEL_BASE_URL", "AGIW_MCP_AUTHOR_MODEL", "AGIW_MCP_REVIEWER_MODEL",
               "AGIW_MCP_JEV_URL", "AGIW_MCP_TEST_OS_KEYSTORE", "XDG_CONFIG_HOME",
               "AGIW_MCP_REASONING_EFFORT", "AGIW_MCP_BASE_URL", "AGIW_MCP_TICKET_DIR",
               "AGIW_MCP_REVIEWER_BASE_URL")

class IsolatedTestCase(unittest.TestCase):
    """Test mode on, a private config dir, and no inherited keys or model settings."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agiw-mcp-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config_dir = self.tmp / "config"
        env = {key: value for key, value in os.environ.items() if key not in CLEARED_ENV}
        env.update({"AGIW_MCP_TEST_MODE": "1", "AGIW_MCP_CONFIG_DIR": str(self.config_dir)})
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_config(self, **values) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "config.json").write_text(json.dumps(values), encoding="utf-8")

    def grant_consent(self) -> None:
        from agiw_mcp import consent
        consent.grant(consent.disclosure_sha256())


def subprocess_env(config_dir: Path, **extra) -> dict:
    env = {key: value for key, value in os.environ.items() if key not in CLEARED_ENV}
    env.update({"AGIW_MCP_TEST_MODE": "1", "AGIW_MCP_CONFIG_DIR": str(config_dir), "PYTHONPATH": str(SRC),
                "PYTHONDONTWRITEBYTECODE": "1"})
    env.update(extra)
    return env


def tree_snapshot(root: Path) -> dict:
    """Map every path under root to (kind, size, sha256) for before/after comparison."""
    result = {}
    for base, dirs, files in os.walk(root):
        for name in dirs:
            result[str(Path(base, name).relative_to(root))] = ("dir",)
        for name in files:
            path = Path(base, name)
            data = path.read_bytes()
            result[str(path.relative_to(root))] = ("file", len(data), hashlib.sha256(data).hexdigest(),
                                                   path.stat().st_mtime_ns)
    return result


# --- fake servers ----------------------------------------------------------

class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeServer:
    """A loopback HTTP server whose handler is a Python callable."""

    def __init__(self, handler):
        outer = self
        self.requests = []
        self.handler = handler

        class Handler(http.server.BaseHTTPRequestHandler):
            def _dispatch(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                record = {"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body}
                outer.requests.append(record)
                status, payload = outer.handler(record)
                if status is None:
                    return  # simulate a hang handled by the caller's timeout
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = _dispatch
            do_POST = _dispatch

            def log_message(self, *args):
                pass

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def chat_envelope(model: str, content: dict | str) -> dict:
    text = content if isinstance(content, str) else json.dumps(content)
    return {"id": "chatcmpl-test", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


class FakeModelServer(FakeServer):
    """OpenAI-compatible fake: GET /v1/models and POST /v1/chat/completions.

    ``draft``, ``repair`` and ``review`` are callables taking the decoded user
    payload (Nisi's prompt object) and returning the model's JSON answer.
    """

    def __init__(self, models=("author-model", "reviewer-model"), *, draft=None, repair=None, review=None):
        self.models = list(models)
        self.calls = []
        self.draft = draft or (lambda prompt: {"candidate": {"files": []}, "note": "nothing"})
        self.repair = repair or (lambda prompt: {"status": "NO_CHANGE", "candidate": None, "note": "no change"})
        self.review = review or (lambda prompt: {"findings": [], "summary": "Inspected the candidate; no issues."})
        super().__init__(self._handle)

    def _handle(self, record):
        if record["method"] == "GET" and record["path"] == "/v1/models":
            return 200, {"object": "list", "data": [{"id": model, "object": "model"} for model in self.models]}
        if record["method"] == "POST" and record["path"] == "/v1/chat/completions":
            body = json.loads(record["body"])
            operation = body["response_format"]["json_schema"]["name"].replace("nisi_", "")
            prompt = json.loads(body["messages"][1]["content"])
            self.calls.append((operation, body["model"]))
            answer = {"draft": self.draft, "repair": self.repair, "review": self.review}[operation](prompt)
            return 200, chat_envelope(body["model"], answer)
        return 404, {"error": "not found"}


def jev_answers(phase: str, values: dict | None = None, model: str = "jev-1.14.0") -> dict:
    """A well-formed Jev response for the fixed questions; values maps id -> number."""
    from agiw_mcp.questions import QUESTIONS
    answers = {}
    for qid, question in QUESTIONS[phase].items():
        value = (values or {}).get(qid, 0.1 if question["type"] == "noul" else 0)
        answers[qid] = {"type": question["type"], question["type"]: value, "confidence": 0.8}
    return {"model": model, "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 10}}


def node_available() -> bool:
    from agiw_mcp import nisi
    path, version = nisi.find_node()
    return bool(path and version and version >= nisi.MIN_NODE)


def write_fake_node(directory: Path, body: str) -> Path:
    """Write an executable Python script that stands in for node."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "node"
    path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path
