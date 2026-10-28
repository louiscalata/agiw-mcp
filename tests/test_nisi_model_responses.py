# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path
import unittest
from unittest import mock

from support import IsolatedTestCase, node_available

from agiw_mcp import nisi, tools

REASONING = "private reasoning sentinel: never use this as candidate content"
FIXED = "def add(a, b):\n    return a + b\n"


@unittest.skipUnless(node_available(), "Node.js 22+ required for the real runner fixture")
class ModelResponseTests(IsolatedTestCase):
    FETCH_FIXTURE = r'''
import fs from 'node:fs';
const fixture = JSON.parse(fs.readFileSync(new URL('./fixture.json', import.meta.url), 'utf8'));
const calls = [];
const bodies = [];
globalThis.fetch = async (url, init) => {
  const body = JSON.parse(init.body);
  const operation = body.response_format.json_schema.name.replace('nisi_', '');
  calls.push([operation, body.model]);
  fs.writeFileSync(new URL('./calls.json', import.meta.url), JSON.stringify(calls));
  bodies.push(body);
  fs.writeFileSync(new URL('./bodies.json', import.meta.url), JSON.stringify(bodies));
  const content = operation === 'review' ? { findings: [], summary: 'Inspected the change.' }
    : operation === 'repair' ? { status: 'REPAIRED', candidate: { files: [
        { path: 'calc.py', content: fixture.fixed }] }, note: 'Repaired.' }
    : { candidate: { files: [{ path: 'calc.py', content: fixture.draft }] }, note: 'Use addition.' };
  const message = { role: 'assistant', content: JSON.stringify(content) };
  if (fixture.operation === operation) {
    message.reasoning_content = fixture.reasoning;
    if (fixture.missing) delete message.content;
    else if (fixture.content !== 'keep') message.content = fixture.content;
  }
  const raw = JSON.stringify({ model: body.model, choices: [{ message, finish_reason: 'stop' }] });
  if (fixture.chunked) {
    const bytes = new TextEncoder().encode(raw);
    return new Response(new ReadableStream({ start(controller) {
      for (let index = 0; index < bytes.length; index += 7) controller.enqueue(bytes.slice(index, index + 7));
      controller.close();
    } }), { status: 200 });
  }
  return new Response(raw, { status: 200 });
};
'''

    def setUp(self):
        super().setUp()
        self.fixture = {"draft": FIXED, "fixed": FIXED}
        self.calls = []
        self.bodies = []
        self.parent = self.tmp / "runs"
        self.parent.mkdir()
        self.write_config(model_base_url="http://127.0.0.1:1235/v1", author_model="author-model",
                          reviewer_model="reviewer-model")
        copy_verified = nisi._copy_verified
        bounded_run = nisi._bounded_run

        def copy_with_fixture(root, destination):
            copy_verified(root, destination)
            (destination / "fake-fetch.mjs").write_text(self.FETCH_FIXTURE, encoding="utf-8")
            (destination / "fixture.json").write_text(json.dumps(self.fixture), encoding="utf-8")

        def run_with_fixture(argv, **kwargs):
            # Load an in-memory fetch fake before the real runner. No socket is opened, and Nisi's
            # actual adapter, stream limits, engine and Node sandbox all still run.
            work = Path(kwargs["cwd"])
            # A backslash in --import is an escape on Windows, so Node never loads the hook.
            module = (work / "fake-fetch.mjs").resolve().as_uri()
            done = bounded_run([argv[0], *argv[1:-1], "--import=" + module, argv[-1]], **kwargs)
            calls_path = work / "calls.json"
            if not calls_path.is_file():
                raise AssertionError("the Node import hook did not run")
            self.calls = json.loads(calls_path.read_text())
            self.bodies = json.loads((work / "bodies.json").read_text())
            return done

        for patcher in (mock.patch.object(nisi, "_copy_verified", side_effect=copy_with_fixture),
                        mock.patch.object(nisi, "_bounded_run", side_effect=run_with_fixture)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_tool(self, **extra):
        with mock.patch.object(nisi.tempfile, "tempdir", str(self.parent)):
            result = tools.call_tool("agiw_nisi_run", {"task": "Fix addition", "repair_budget": 0,
                                     "files": [{"path": "calc.py", "content": "def add(a, b): return a - b\n"}],
                                     **extra})
        self.assertEqual(list(self.parent.iterdir()), [], "run directory was not removed")
        return result

    def response_shape(self, operation, *, content="", missing=False, reasoning=REASONING):
        self.fixture.update(operation=operation, content=content, missing=missing, reasoning=reasoning)

    def assert_reasoning_refusal(self, result, is_error, model):
        self.assertTrue(is_error, result)
        self.assertEqual(result["error"]["code"], "MODEL_ANSWER_IN_REASONING")
        message = result["error"]["message"]
        for setting in (model, "reasoning_effort", "none", "AGIW_MCP_REASONING_EFFORT"):
            self.assertIn(setting, message)
        self.assertNotIn(REASONING, json.dumps(result))
        self.assertNotIn("proposedFiles", result)

    def test_normal_content_with_reasoning_completes(self):
        self.response_shape("draft", content="keep")
        result, is_error = self.run_tool()
        self.assertFalse(is_error, result)
        self.assertEqual(result["outcome"], "COMPLETED", result)
        self.assertEqual(result["proposedFiles"][0]["content"], FIXED)
        self.assertNotIn(REASONING, json.dumps(result))
        self.assertEqual(result["sandbox"], "node-permission")

    def test_reasoning_effort_is_sent_only_when_configured(self):
        for effort in (None, "none", "high"):
            with self.subTest(effort=effort):
                self.write_config(model_base_url="http://127.0.0.1:1235/v1", author_model="author-model",
                                  reviewer_model="reviewer-model", reasoning_effort=effort)
                result, is_error = self.run_tool()
                self.assertFalse(is_error, result)
                self.assertTrue(result["completed"], result)
                self.assertEqual(len(self.bodies), 2)
                for body in self.bodies:
                    if effort is None:
                        self.assertNotIn("reasoning_effort", body)
                        self.assertNotIn("chat_template_kwargs", body)
                    else:
                        self.assertEqual(body["reasoning_effort"], effort)
                        self.assertEqual(body["chat_template_kwargs"], {"reasoning_effort": effort})

    def test_chunked_reasoning_only_response_is_refused(self):
        self.fixture["chunked"] = True
        self.response_shape("draft", reasoning=REASONING + " é")
        self.assert_reasoning_refusal(*self.run_tool(), "author-model")

    def test_reasoning_only_author_response_is_refused(self):
        for content, missing, effort in (("", False, None), (None, False, "high"),
                                         (" \n", False, "none"), ("", True, None)):
            with self.subTest(content=content, missing=missing, effort=effort):
                self.write_config(model_base_url="http://127.0.0.1:1235/v1", author_model="author-model",
                                  reviewer_model="reviewer-model", reasoning_effort=effort)
                self.response_shape("draft", content=content, missing=missing)
                self.assert_reasoning_refusal(*self.run_tool(), "author-model")
                self.assertEqual(self.calls, [["draft", "author-model"]])

    def test_reasoning_only_reviewer_response_is_refused(self):
        self.response_shape("review", missing=True)
        self.assert_reasoning_refusal(*self.run_tool(), "reviewer-model")
        self.assertEqual(self.calls, [["draft", "author-model"], ["review", "reviewer-model"]])

    def test_reasoning_only_repair_response_is_refused(self):
        self.fixture["draft"] = "def add(a, b)\n    return a + b\n"
        self.response_shape("repair")
        self.assert_reasoning_refusal(*self.run_tool(repair_budget=1), "author-model")
        self.assertEqual(self.calls, [["draft", "author-model"], ["repair", "author-model"]])

    def test_empty_reasoning_is_not_misclassified(self):
        for reasoning in ("", " \n", None):
            with self.subTest(reasoning=reasoning):
                self.response_shape("draft", reasoning=reasoning)
                result, is_error = self.run_tool()
                self.assertFalse(is_error, result)
                self.assertFalse(result["completed"])
                self.assertEqual(result["modelReceipts"]["author"][0]["code"], "LOCAL_CHAT_EMPTY_RESPONSE")

    def test_oversized_response_keeps_the_adapter_limit(self):
        self.response_shape("draft", reasoning="x" * (8 * 1024 * 1024))
        result, is_error = self.run_tool()
        self.assertFalse(is_error, result)
        self.assertFalse(result["completed"])
        self.assertEqual(result["modelReceipts"]["author"][0]["code"], "LOCAL_CHAT_RESPONSE_TOO_LARGE")
