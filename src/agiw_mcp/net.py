# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Bounded HTTP helpers. All outbound requests in this package go through here.

Redirects are refused, proxy settings are ignored, every request has one total
deadline, response bodies are size-capped, and in test mode any URL whose host
is not a loopback literal is refused before a socket opens.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .config import test_mode

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class NetworkRefused(Exception):
    """A URL was refused by policy before any connection was attempted."""


class HttpFailure(Exception):
    """A request failed. ``kind`` is a fixed word; no response text is kept."""

    def __init__(self, kind: str, status: int | None = None):
        super().__init__(kind)
        self.kind = kind
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HttpFailure("redirect", code)


def is_loopback_url(url: str) -> bool:
    try:
        return urlsplit(url).hostname in LOOPBACK_HOSTS
    except ValueError:
        return False


def guard(url: str) -> None:
    if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https"):
        raise NetworkRefused("unsupported URL")
    if test_mode() and not is_loopback_url(url):
        raise NetworkRefused("test mode refuses non-loopback URLs")


class _SocketBox:
    """Sockets for the in-flight exchange, so a deadline can close them from the caller."""

    def __init__(self) -> None:
        self._socks: list = []
        self._lock = threading.Lock()

    def remember(self, sock) -> None:
        if sock is None:
            return
        with self._lock:
            if sock not in self._socks:
                self._socks.append(sock)

    def close(self) -> None:
        with self._lock:
            socks = list(self._socks)
        for sock in socks:
            shutdown = getattr(sock, "shutdown", None)
            if shutdown is not None:
                try:
                    shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            try:
                sock.close()
            except OSError:
                pass


def _connection_class(capture: _SocketBox, base):
    class Connection(base):
        def connect(self) -> None:
            super().connect()
            capture.remember(self.sock)

    return Connection


class _CapturingHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, capture: _SocketBox):
        super().__init__()
        self._capture = capture

    def http_open(self, req):
        return self.do_open(_connection_class(self._capture, http.client.HTTPConnection), req)


class _CapturingHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, capture: _SocketBox):
        super().__init__()
        self._capture = capture

    def https_open(self, req):
        return self.do_open(_connection_class(self._capture, http.client.HTTPSConnection), req)


def _opener(capture: _SocketBox | None = None):
    # Never use HTTP(S)_PROXY or system proxies: loopback traffic stays local, and the Jev request goes
    # straight to the endpoint named in the consent disclosure.
    handlers = [_NoRedirect(), urllib.request.ProxyHandler({})]
    if capture is not None:
        handlers.extend((_CapturingHTTPHandler(capture), _CapturingHTTPSHandler(capture)))
    return urllib.request.build_opener(*handlers)


def request_json(url: str, *, method: str = "GET", body: dict | None = None, headers: dict | None = None,
                 timeout: float, max_bytes: int) -> tuple[int, object]:
    """Send one request and parse a JSON response. Never retries.

    ``timeout`` bounds the whole exchange (connect, headers and body), not only each socket read, so a
    server that trickles bytes cannot hold the caller past it."""
    guard(url)
    data = None
    all_headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        all_headers["Content-Type"] = "application/json"
    all_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    outcome: dict = {}
    capture = _SocketBox()

    def exchange() -> None:
        try:
            with _opener(capture).open(req, timeout=timeout) as resp:
                capture.remember(getattr(getattr(resp, "fp", None), "raw", None))
                outcome["raw"] = resp.read(max_bytes + 1)
                outcome["status"] = resp.status
        except BaseException as exc:  # noqa: B036 - handed to the caller's thread below
            outcome["error"] = exc

    worker = threading.Thread(target=exchange, name="agiw-mcp-http", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        # Closing the socket unblocks a read that a trickling server would otherwise keep open, and the
        # join collects the worker so neither the thread nor the socket is left behind.
        capture.close()
        worker.join(1.0)
        # The deadline passed. Closing the socket can end the read with a clean EOF instead of an error
        # (macOS does this), leaving a truncated body in ``outcome``; it must not be parsed as a reply.
        raise HttpFailure("unreachable")
    try:
        if "error" in outcome:
            raise outcome["error"]
        raw, status = outcome["raw"], outcome["status"]
    except HttpFailure:
        raise
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            exc.close()
        except Exception:
            pass
        raise HttpFailure("http", status) from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        raise HttpFailure("unreachable") from None
    if len(raw) > max_bytes:
        raise HttpFailure("too_large", status)
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                            parse_constant=_reject_constant)
    except (UnicodeError, ValueError):
        raise HttpFailure("invalid_json", status) from None
    return status, parsed


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _reject_constant(_value):
    raise ValueError("non-finite number")
