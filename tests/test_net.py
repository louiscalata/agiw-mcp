# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
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


if __name__ == "__main__":
    unittest.main()
