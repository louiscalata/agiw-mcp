# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
import os
import shutil
import socket
import time
import unittest
from unittest import mock

from support import FakeModelServer, IsolatedTestCase

from agiw_mcp import nisi, readiness, tools

GOOD_NODE = ("/usr/local/bin/node", (22, 13, 0))


class ReadinessTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(nisi, "find_node", return_value=GOOD_NODE)
        self.find_node = patcher.start()
        self.addCleanup(patcher.stop)
        self.models = FakeModelServer()
        self.addCleanup(self.models.close)

    def configure(self, author="author-model", reviewer="reviewer-model"):
        self.write_config(model_base_url=self.models.base + "/v1", author_model=author, reviewer_model=reviewer)

    def missing(self):
        result = readiness.evaluate()
        self.assertEqual(result["status"], "NOT_READY")
        self.assertIn("fix", result["missing"])
        self.assertTrue(result["missing"]["fix"])
        return result["missing"]["id"]

    def make_ready(self):
        self.configure()
        os.environ["TYPESAFE_API_KEY"] = "tsk_fake_" + "0123456789"
        self.grant_consent()

    def test_ready_when_everything_is_present(self):
        self.make_ready()
        result = readiness.evaluate()
        self.assertEqual(result["status"], "READY", result)
        self.assertEqual([row["id"] for row in result["checks"]],
                         ["python", "config", "node", "nisi", "models", "consent", "jev_key"])

    def test_key_is_never_looked_up_before_consent(self):
        # On Linux the Secret Service lookup returns the secret, so nothing may probe it before consent.
        self.configure()
        # readiness turns exceptions into rows, so count calls instead of raising from the mock.
        with mock.patch.object(readiness.keys, "key_presence", return_value={"present": True, "source": "x"}) as probe:
            for stop_at_first in (True, False):
                result = readiness.evaluate(stop_at_first=stop_at_first)
                self.assertEqual(result["missing"]["id"], "consent", result)
            status, is_error = tools.call_tool("agiw_status", {})
            self.assertFalse(is_error, status)
            self.assertEqual(probe.call_count, 0)
            self.grant_consent()
            readiness.evaluate(stop_at_first=False)
            self.assertEqual(probe.call_count, 1)

    def test_order_and_first_missing(self):
        self.make_ready()
        del os.environ["TYPESAFE_API_KEY"]
        self.assertEqual(self.missing(), "jev_key")
        self.find_node.return_value = (None, None)
        self.assertEqual(self.missing(), "node")

    def test_old_node(self):
        self.make_ready()
        self.find_node.return_value = ("/usr/bin/node", (20, 11, 1))
        self.assertEqual(self.missing(), "node")

    def test_consent_missing(self):
        self.configure()
        os.environ["TYPESAFE_API_KEY"] = "tsk_fake_" + "0123456789"
        self.assertEqual(self.missing(), "consent")

    def test_models_not_configured_or_identical(self):
        self.assertEqual(self.missing(), "models")
        self.configure(author="same", reviewer="same")
        result = readiness.evaluate()
        self.assertIn("same model", result["missing"]["detail"])

    def test_model_not_listed(self):
        self.configure(reviewer="absent-model")
        result = readiness.evaluate()
        self.assertEqual(result["missing"]["id"], "models")
        self.assertIn("absent-model", result["missing"]["fix"])

    def test_endpoint_down(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.write_config(model_base_url=f"http://127.0.0.1:{port}/v1", author_model="a", reviewer_model="b")
        result = readiness.evaluate()
        self.assertEqual(result["missing"]["id"], "models")
        self.assertIn("Start your local", result["missing"]["fix"])

    def test_hanging_endpoint_is_bounded(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        self.write_config(model_base_url=f"http://127.0.0.1:{port}/v1", author_model="a", reviewer_model="b")
        started = time.monotonic()
        result = readiness.evaluate()
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(result["missing"]["id"], "models")

    def test_tampered_vendor_is_detected(self):
        copy = self.tmp / "vendor"
        shutil.copytree(nisi.VENDOR_DIR, copy)
        target = copy / "package" / "workflow" / "engine.mjs"
        target.write_bytes(target.read_bytes() + b"\n// changed\n")
        with mock.patch.object(nisi, "VENDOR_DIR", copy):
            self.assertEqual(self.missing(), "nisi")
        extra = self.tmp / "vendor2"
        shutil.copytree(nisi.VENDOR_DIR, extra)
        (extra / "package" / "extra.mjs").write_text("export {};\n")
        with self.assertRaises(nisi.NisiError):
            nisi.verify_vendor(extra)

    def test_no_exception_escapes(self):
        with mock.patch.object(readiness, "check_node", side_effect=RuntimeError("boom /secret/path")):
            result = readiness.evaluate()
        self.assertEqual(result["missing"]["id"], "node")
        self.assertNotIn("/secret/path", str(result))

    def test_tool_takes_no_arguments_and_starts_nothing(self):
        result, error = tools.call_tool("agiw_readiness", {})
        self.assertFalse(error)
        self.assertEqual(self.models.calls, [])
        result, error = tools.call_tool("agiw_readiness", {"task": "x"})
        self.assertTrue(error)

    def test_status_has_no_secrets_and_uses_tilde(self):
        self.make_ready()
        key = os.environ["TYPESAFE_API_KEY"]
        result, error = tools.call_tool("agiw_status", {})
        self.assertFalse(error)
        rendered = str(result)
        self.assertNotIn(key, rendered)
        self.assertTrue(result["prerequisites"]["consent"])
        self.assertEqual(result["nisi"]["version"], "0.2.0")
        home = os.path.expanduser("~")
        if str(self.tmp).startswith(home):
            self.assertNotIn(home + os.sep, result["configDir"])


if __name__ == "__main__":
    unittest.main()
