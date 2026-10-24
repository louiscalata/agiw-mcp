# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
import json
import os
import unittest
from unittest import mock

from support import FakeServer, IsolatedTestCase, jev_answers

from agiw_mcp import activity, consent, jev, tools
from agiw_mcp.config import load_config
from agiw_mcp.errors import ToolError

FAKE_KEY = "tsk_fake_" + "a1b2c3d4e5f6a7b8c9d0"


class JevTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.responses = []
        self.server = FakeServer(self._respond)
        self.addCleanup(self.server.close)
        # The production endpoint is fixed; tests patch the function instead of using any variable.
        self.endpoint = mock.patch.object(jev, "endpoint", return_value=self.server.base + "/v1/systemone")
        self.endpoint.start()
        self.addCleanup(self.endpoint.stop)

    def _respond(self, record):
        if self.responses:
            return self.responses.pop(0)
        body = json.loads(record["body"])
        phase = "pre" if "task" in body["state"] else "post"
        return 200, jev_answers(phase)

    def call(self, phase, text):
        return jev.analyse(phase, text, load_config())

    def assert_refused(self, code, phase="pre", text="Add a retry to the uploader."):
        with self.assertRaises(ToolError) as ctx:
            self.call(phase, text)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_refuses_without_consent_and_sends_nothing(self):
        os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
        self.assert_refused("CONSENT_REQUIRED")
        self.assertEqual(self.server.requests, [])

    def test_refuses_without_key_with_fixed_message(self):
        self.grant_consent()
        error = self.assert_refused("KEY_UNAVAILABLE")
        from agiw_mcp.keys import KEY_UNAVAILABLE_MESSAGE
        self.assertEqual(error.message, KEY_UNAVAILABLE_MESSAGE)
        self.assertEqual(self.server.requests, [])

    def test_pre_success_flags_and_payload(self):
        self.grant_consent()
        os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
        self.responses.append((200, jev_answers("pre", {"underspecified": 0.9, "harm_if_wrong": 2})))
        result = self.call("pre", "Add a retry to the uploader.")
        self.assertEqual(result["advisory"], "advisory, not a verdict")
        self.assertEqual(sorted(result["flagged"]), ["harm_if_wrong", "underspecified"])
        self.assertEqual(len(self.server.requests), 1)
        request = self.server.requests[0]
        self.assertEqual(request["headers"]["Authorization"], "Bearer " + FAKE_KEY)
        body = json.loads(request["body"])
        self.assertEqual(set(body), {"model", "state", "questions"})
        self.assertEqual(body["state"], {"task": "Add a retry to the uploader."})
        self.assertEqual(body["model"], "jev-1.14.0")

    def test_score_flag_threshold_is_half_scale(self):
        self.grant_consent()
        os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
        self.responses.append((200, jev_answers("pre", {"harm_if_wrong": 1.4, "blocked": 0.49})))
        result = self.call("pre", "Rename a variable.")
        self.assertEqual(result["flagged"], [])
        self.responses.append((200, jev_answers("pre", {"harm_if_wrong": 1.5, "blocked": 0.5})))
        result = self.call("pre", "Rename a variable.")
        self.assertEqual(sorted(result["flagged"]), ["blocked", "harm_if_wrong"])

    def test_post_truncates_long_reports(self):
        self.grant_consent()
        os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
        report = "All tests pass. " * 2000
        result = self.call("post", report)
        self.assertTrue(result["truncated"])
        sent = json.loads(self.server.requests[0]["body"])["state"]["report"]
        self.assertLessEqual(len(sent.encode("utf-8")), 12 * 1024)
        self.assertTrue(sent.endswith("[truncated by agiw-mcp]"))

    def test_pre_refuses_oversized_task(self):
        self.grant_consent()
        os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
        self.assert_refused("INPUT_TOO_LARGE", text="x" * (12 * 1024 + 1))
        self.assertEqual(self.server.requests, [])

    def test_http_error_is_not_retried_and_hides_key(self):
        self.grant_consent()
        os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
        self.responses.append((429, {"error": "slow down " + FAKE_KEY}))
        error = self.assert_refused("JEV_HTTP_ERROR")
        self.assertEqual(len(self.server.requests), 1)
        rendered = json.dumps(error.payload())
        self.assertNotIn(FAKE_KEY, rendered)
        self.assertNotIn("slow down", rendered)

    def test_malformed_answers_are_refused(self):
        self.grant_consent()
        os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
        bad = jev_answers("pre")
        bad["answers"]["blocked"]["noul"] = 1.7
        self.responses.append((200, bad))
        self.assert_refused("JEV_BAD_RESPONSE")
        missing = jev_answers("pre")
        del missing["answers"]["blocked"]
        self.responses.append((200, missing))
        self.assert_refused("JEV_BAD_RESPONSE")

    def test_model_mismatch_is_refused(self):
        self.grant_consent()
        os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
        self.responses.append((200, jev_answers("pre", model="jev-9.9.9")))
        self.assert_refused("JEV_MODEL_MISMATCH")

    def test_activity_log_keeps_hashes_and_sizes_only(self):
        self.grant_consent()
        os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
        secret_task = "Rotate the uploader credentials marker-7f3c"
        self.call("pre", secret_task)
        log = (self.config_dir / activity.LOG_FILE).read_text(encoding="utf-8")
        self.assertNotIn("marker-7f3c", log)
        self.assertNotIn(FAKE_KEY, log)
        entry = json.loads(log.splitlines()[-1])
        self.assertEqual(entry["outcome"], "ANSWERED")
        self.assertEqual(entry["inputBytes"], len(secret_task.encode()))
        if os.name == "posix":
            self.assertEqual((self.config_dir / activity.LOG_FILE).stat().st_mode & 0o777, 0o600)

    def test_key_never_appears_in_any_tool_output(self):
        self.grant_consent()
        os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
        outputs = [tools.call_tool(name, args) for name, args in (
            ("agiw_status", {}), ("agiw_consent", {"action": "show"}),
            ("agiw_jev_pre", {"task": "Add a retry to the uploader."}),
            ("agiw_jev_post", {"report": "Added the retry. Verdict: READY"}))]
        # Upstream errors that echo the key or the request headers must not carry it back out.
        self.responses.append((401, {"error": "invalid key " + FAKE_KEY}))
        outputs.append(tools.call_tool("agiw_jev_pre", {"task": "Add a retry to the uploader."}))
        self.responses.append((500, b"Authorization: Bearer " + FAKE_KEY.encode()))
        outputs.append(tools.call_tool("agiw_jev_post", {"report": "Done"}))
        self.assertEqual(len(self.server.requests), 4)
        seen = json.dumps(outputs) + (self.config_dir / activity.LOG_FILE).read_text(encoding="utf-8")
        self.assertNotIn(FAKE_KEY, seen)
        self.assertNotIn(FAKE_KEY[-12:], seen)

    def test_successful_response_cannot_echo_key_or_text(self):
        self.grant_consent()
        os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
        task = "Rotate the uploader credentials marker-9d2e"
        body = jev_answers("pre")
        body["usage"] = {"input_tokens": 12, "output_tokens": 3, "debug": "Bearer " + FAKE_KEY, "echo": task,
                         "nested": {"k": FAKE_KEY}, "Bad Key": 5, "negative": -1, "flag": True}
        self.responses.append((200, body))
        result = self.call("pre", task)
        self.assertEqual(result["usage"], {"input_tokens": 12, "output_tokens": 3})
        self.write_config(jev_model="jev-latest")
        for served in ("jev-latest " + FAKE_KEY[:40], "marker-9d2e", "jev-2.0.0"):
            self.responses.append((200, jev_answers("pre", model=served)))
            result = self.call("pre", task)
            self.assertEqual(result["model"], "jev-2.0.0" if served == "jev-2.0.0" else None)
            self.assertNotIn(FAKE_KEY[:20], json.dumps(result))
            self.assertNotIn("marker-9d2e", json.dumps(result))

    def test_variables_cannot_redirect_jev(self):
        self.endpoint.stop()
        self.grant_consent()
        os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
        os.environ["AGIW_MCP_JEV_URL"] = self.server.base + "/collect"
        self.assertEqual(jev.endpoint(), consent.JEV_ENDPOINT)
        # Test mode refuses the real (non-loopback) endpoint before any socket opens, so nothing is sent.
        self.assert_refused("JEV_ENDPOINT_REFUSED")
        self.assertEqual(self.server.requests, [])
        self.endpoint.start()

    def test_non_loopback_endpoint_refused_in_test_mode(self):
        self.grant_consent()
        os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
        with mock.patch.object(jev, "endpoint", return_value="https://jev.example.invalid/v1/systemone"):
            self.assert_refused("JEV_ENDPOINT_REFUSED")
        self.assertEqual(self.server.requests, [])

    def test_tool_layer_wraps_errors(self):
        result, is_error = tools.call_tool("agiw_jev_pre", {"task": "Do it"})
        self.assertTrue(is_error)
        self.assertEqual(result["error"]["code"], "CONSENT_REQUIRED")
        result, is_error = tools.call_tool("agiw_jev_post", {"report": "Done", "extra": 1})
        self.assertTrue(is_error)
        self.assertEqual(result["error"]["code"], "INPUT_INVALID")


if __name__ == "__main__":
    unittest.main()
