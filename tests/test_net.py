# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
import io
import os
import socket
import threading
import time
import unittest
import urllib.request
from unittest import mock

from support import FakeServer, IsolatedTestCase

from agiw_mcp import net


class NetTests(IsolatedTestCase):
    def request_fixture(self, raw, max_bytes):
        response = io.BytesIO(raw)
        response.status = 200
        with mock.patch.object(net, "_opener") as opener:
            opener.return_value.open.return_value = response
            return net.request_json("http://127.0.0.1:1235/x", timeout=3.0, max_bytes=max_bytes)

    def test_invalid_json_responses_are_refused(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b"{broken", b"\xff"):
            with self.subTest(raw=raw):
                with self.assertRaises(net.HttpFailure) as ctx:
                    self.request_fixture(raw, 1024)
                self.assertEqual(ctx.exception.kind, "invalid_json")
                self.assertEqual(ctx.exception.status, 200)

    def test_response_size_limit(self):
        self.assertEqual(self.request_fixture(b'{"x":1}', 7), (200, {"x": 1}))
        with self.assertRaises(net.HttpFailure) as ctx:
            self.request_fixture(b'{"x":1}', 6)
        self.assertEqual(ctx.exception.kind, "too_large")
        self.assertEqual(ctx.exception.status, 200)

    def test_proxy_variables_are_ignored(self):
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
            os.environ[name] = "http://127.0.0.1:9"
        # urllib's default ProxyHandler reads the proxy variables when it is built; ours must never build it.
        with mock.patch.object(urllib.request, "getproxies", side_effect=AssertionError("proxy settings read")):
            opener = net._opener()
            server = FakeServer(lambda record: (200, {"ok": True}))
            self.addCleanup(server.close)
            status, body = net.request_json(server.base + "/x", timeout=3.0, max_bytes=1024)
        self.assertFalse([h for h in opener.handlers if getattr(h, "proxies", None)])
        self.assertEqual((status, body), (200, {"ok": True}))

    def test_total_deadline_against_a_trickling_server(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        stop = threading.Event()

        def trickle():
            conn, _ = listener.accept()
            with conn:
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 4096\r\n\r\n")
                while not stop.is_set():
                    try:
                        conn.sendall(b" ")
                    except OSError:
                        return
                    time.sleep(0.2)

        thread = threading.Thread(target=trickle, daemon=True)
        thread.start()
        self.addCleanup(listener.close)
        self.addCleanup(stop.set)
        started = time.monotonic()
        with self.assertRaises(net.HttpFailure) as ctx:
            net.request_json(f"http://127.0.0.1:{port}/v1/models", timeout=1.0, max_bytes=8192)
        elapsed = time.monotonic() - started
        self.assertEqual(ctx.exception.kind, "unreachable")
        self.assertLess(elapsed, 2.0)

    def test_timeout_closes_the_worker_and_the_socket(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        released = threading.Event()

        def trickle():
            try:
                conn, _ = listener.accept()
            except OSError:
                released.set()
                return
            with conn:
                try:
                    conn.recv(65536)
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                 b"Content-Length: 4096\r\n\r\n")
                    while True:
                        conn.sendall(b" ")
                        time.sleep(0.05)
                except OSError:
                    pass
            released.set()

        thread = threading.Thread(target=trickle, daemon=True)
        thread.start()
        self.addCleanup(listener.close)
        before = [item for item in threading.enumerate() if item.name == "agiw-mcp-http" and item.is_alive()]
        with self.assertRaises(net.HttpFailure) as ctx:
            net.request_json(f"http://127.0.0.1:{port}/v1/models", timeout=0.4, max_bytes=8192)
        self.assertEqual(ctx.exception.kind, "unreachable")
        self.assertEqual(before, [])
        self.assertFalse(any(item.name == "agiw-mcp-http" and item.is_alive() for item in threading.enumerate()))
        self.assertTrue(released.wait(2), "timed-out request left the server socket open")


    def test_clean_eof_after_the_deadline_is_not_parsed(self):
        # On macOS, closing the socket at the deadline can end the worker's read with a clean EOF, so
        # the read returns the truncated body instead of raising.
        closed = threading.Event()

        class Sock:
            def close(self):
                closed.set()

        class Response:
            status = 200
            fp = None

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, _amt):
                closed.wait(5)
                return b"  "

        def opener(capture=None):
            capture.remember(Sock())
            return mock.Mock(open=mock.Mock(return_value=Response()))

        with mock.patch.object(net, "_opener", side_effect=opener):
            with self.assertRaises(net.HttpFailure) as ctx:
                net.request_json("http://127.0.0.1:1235/x", timeout=0.2, max_bytes=8192)
        self.assertEqual(ctx.exception.kind, "unreachable")


if __name__ == "__main__":
    unittest.main()
