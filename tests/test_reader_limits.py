# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Local and remote JSON readers refuse deep nesting and non-regular files with their usual codes.

Python's json decoder can raise RecursionError, not ValueError, on deeply nested input: on 3.12 and 3.13
from about 30,000 levels. A FIFO at config.json or consent.json used to block a plain open() forever, which
froze the stdio server.
"""

import json
import os
import threading
import unittest
from unittest import mock

from support import FakeServer, IsolatedTestCase

from agiw_mcp import activity, config, consent, jev, net
from agiw_mcp.config import ConfigError, load_config
from agiw_mcp.errors import ToolError

FAKE_KEY = "tsk_fake_" + "a1b2c3d4e5f6a7b8c9d0"


def nested(depth: int) -> bytes:
    return b"[" * depth + b"]" * depth


def within(seconds: float, fn):
    """Run fn in a thread; fail instead of hanging the suite if it blocks."""
    outcome = {}

    def run():
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed back to the test thread
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise AssertionError(f"call still blocked after {seconds} s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


class DeepNestingTests(IsolatedTestCase):
    def test_net_reports_deep_response_as_invalid_json(self):
        # 200,000 levels raise RecursionError on every interpreter tried (3.12 to 3.15).
        server = FakeServer(lambda record: (200, nested(200_000)))
        self.addCleanup(server.close)
        with self.assertRaises(net.HttpFailure) as ctx:
            net.request_json(server.base + "/v1/models", timeout=10, max_bytes=1024 * 1024)
        self.assertEqual(ctx.exception.kind, "invalid_json")
        self.assertEqual(ctx.exception.status, 200)

    def test_net_maps_recursion_error_on_any_interpreter(self):
        server = FakeServer(lambda record: (200, {"ok": True}))
        self.addCleanup(server.close)
        with mock.patch.object(net.json, "loads", side_effect=RecursionError("deep")):
            with self.assertRaises(net.HttpFailure) as ctx:
                net.request_json(server.base + "/v1/models", timeout=10, max_bytes=1024)
        self.assertEqual(ctx.exception.kind, "invalid_json")

    def test_deep_jev_answer_is_a_bad_response_and_is_logged(self):
        # 120,000 levels fit the 256 KiB Jev cap. 3.12 to 3.14 raise RecursionError on it; 3.15 parses it
        # into a list, which is also a bad answer. Either way the call is refused and logged.
        server = FakeServer(lambda record: (200, nested(120_000)))
        self.addCleanup(server.close)
        patcher = mock.patch.object(jev, "endpoint", return_value=server.base + "/v1/systemone")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.grant_consent()
        os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
        with self.assertRaises(ToolError) as ctx:
            jev.analyse("pre", "Add a retry to the uploader.", load_config())
        self.assertEqual(ctx.exception.code, "JEV_BAD_RESPONSE")
        self.assertEqual(len(server.requests), 1)
        lines = (self.config_dir / activity.LOG_FILE).read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[-1])["outcome"], "BAD_RESPONSE")

    def test_deep_config_is_invalid(self):
        # 30,000 levels fit the 64 KB config cap and raise RecursionError on 3.12 and 3.13.
        self.config_dir.mkdir(parents=True)
        (self.config_dir / "config.json").write_bytes(b'{"model_base_url": ' + nested(30_000) + b"}")
        with self.assertRaises(ConfigError) as ctx:
            load_config()
        self.assertEqual(ctx.exception.code, "CONFIG_INVALID")

    def test_config_maps_recursion_error_on_any_interpreter(self):
        self.write_config(require_ticket=True)
        with mock.patch.object(config.json, "loads", side_effect=RecursionError("deep")):
            with self.assertRaises(ConfigError) as ctx:
                load_config()
        self.assertEqual(ctx.exception.code, "CONFIG_INVALID")

    def test_consent_maps_recursion_error_to_not_recorded(self):
        self.grant_consent()
        self.assertTrue(consent.consent_recorded())
        with mock.patch.object(consent.json, "loads", side_effect=RecursionError("deep")):
            self.assertFalse(consent.consent_recorded())


@unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs need POSIX")
class NonRegularFileTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.config_dir.mkdir(parents=True)

    def test_fifo_config_is_unreadable_without_blocking(self):
        os.mkfifo(self.config_dir / "config.json")
        with self.assertRaises(ConfigError) as ctx:
            within(5, load_config)
        self.assertEqual(ctx.exception.code, "CONFIG_UNREADABLE")

    def test_fifo_consent_is_not_recorded_without_blocking(self):
        os.mkfifo(self.config_dir / consent.CONSENT_FILE)
        self.assertFalse(within(5, consent.consent_recorded))

    def test_directory_config_is_still_unreadable(self):
        (self.config_dir / "config.json").mkdir()
        with self.assertRaises(ConfigError) as ctx:
            load_config()
        self.assertEqual(ctx.exception.code, "CONFIG_UNREADABLE")

    def test_symlinked_config_is_still_followed(self):
        target = self.tmp / "elsewhere.json"
        target.write_text(json.dumps({"require_ticket": True}), encoding="utf-8")
        (self.config_dir / "config.json").symlink_to(target)
        self.assertTrue(load_config().require_ticket)

    def test_regular_consent_still_reads(self):
        self.grant_consent()
        self.assertTrue(consent.consent_recorded())


if __name__ == "__main__":
    unittest.main()
