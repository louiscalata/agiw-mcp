# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Real-Node progress events without a network or model server."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest

from support import IsolatedTestCase, node_available

from agiw_mcp import nisi

EVENT_KIND = "agiw-mcp.nisi-event.v1"
RESULT_KIND = "agiw-mcp.nisi-run.v1"


@unittest.skipUnless(node_available(), "Node.js 22+ required for the actual host runner")
class RunnerProgressTests(IsolatedTestCase):
    def runner(self, mode="success", **changes):
        work = self.tmp / f"runner-{len(list(self.tmp.iterdir()))}"
        work.mkdir()
        nisi._copy_verified(nisi.VENDOR_DIR, work)
        request = {
            "schema": 1,
            "taskText": "Change the private task text.",
            "files": [{"path": "answer.py", "content": "answer = 0\n"}],
            "allowedFiles": ["answer.py"],
            "acceptanceCriteria": ["Use the requested value."],
            "assertions": [],
            "repairBudget": 1,
            "deadlineMs": 5000,
            "endpoint": "http://127.0.0.1:1234/v1/chat/completions",
            "authorModel": "private-author-model",
            "reviewerModel": "private-reviewer-model",
            "maxOutputTokens": 1000,
            "reasoningEffort": "none",
            "language": "python",
            "python": sys.executable,
            "pythonVersion": "3.12.13",
            "checkTimeoutMs": 1000,
        }
        request.update(changes)
        harness = """
const mode = MODE;
globalThis.agiwMcpFetch = async (_url, init) => {
  const body = JSON.parse(init.body);
  const operation = body.response_format.json_schema.name;
  if (mode === 'error') throw new Error('private provider failure');
  if (mode === 'late') await new Promise(resolve => setTimeout(resolve, 100));
  const candidate = {files: [{path: 'answer.py', content:
    mode === 'repair' && operation === 'nisi_draft' ? 'return 1\\n' : 'answer = 42\\n'}]};
  const answer = operation === 'nisi_draft' ? {candidate, note: 'edit'}
    : operation === 'nisi_repair' ? {status: 'REPAIRED', candidate, note: 'repair'}
    : {findings: [], summary: 'reviewed'};
  return new Response(JSON.stringify({model: body.model,
    choices: [{finish_reason: 'stop', message: {role: 'assistant', content: JSON.stringify(answer)}}],
    usage: {prompt_tokens: 10, completion_tokens: 5, total_tokens: 15}}));
};
await import(RUNNER);
""".replace("MODE", json.dumps(mode)).replace("RUNNER", json.dumps((work / "runner.mjs").as_uri()))
        node = nisi.find_node()[0]
        done = subprocess.run(
            [node, "--input-type=module", "-e", harness],
            input=json.dumps(request).encode("utf-8"),
            capture_output=True,
            check=False,
            cwd=work,
            env=nisi._child_env(node, str(work)),
            timeout=10,
        )
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8"))
        records = [json.loads(line) for line in done.stdout.splitlines()]
        self.assertEqual(records[-1]["kind"], RESULT_KIND)
        events = records[:-1]
        for event in events:
            self.assertEqual(set(event), {"kind", "stage", "attempt", "status"})
            self.assertEqual(event["kind"], EVENT_KIND)
            self.assertIn(event["stage"], {"author", "static", "tests", "review"})
            self.assertIs(type(event["attempt"]), int)
            self.assertIn(event["status"], {"started", "finished"})
        return events, records[-1], request

    def assert_stages(self, events, stages):
        expected = [
            {"kind": EVENT_KIND, "stage": stage, "attempt": attempt, "status": status}
            for stage, attempt in stages
            for status in ("started", "finished")
        ]
        self.assertEqual(events, expected)

    def test_full_run_emits_each_stage_before_the_final_result(self):
        events, result, _ = self.runner()
        self.assert_stages(events, [("author", 0), ("static", 0), ("tests", 0), ("review", 0)])
        self.assertEqual(result["report"]["outcome"], "COMPLETED")
        self.assertEqual(result["syntax"], {"answer.py": "PASS"})

    def test_repair_uses_the_engine_attempt_number(self):
        events, result, _ = self.runner("repair")
        self.assert_stages(
            events,
            [("author", 0), ("static", 0), ("author", 1), ("static", 1), ("tests", 1), ("review", 1)],
        )
        self.assertEqual(result["report"]["outcome"], "COMPLETED")
        self.assertEqual(result["report"]["repairAttempts"], 1)

    def test_adapter_exception_still_finishes_its_progress_pair(self):
        events, result, _ = self.runner("error")
        self.assert_stages(events, [("author", 0)])
        self.assertEqual(result["report"]["outcome"], "BLOCKED")
        self.assertNotIn("private provider failure", json.dumps(events))

    def test_late_adapter_settlement_cannot_follow_the_final_result(self):
        events, result, _ = self.runner("late", deadlineMs=30)
        self.assertEqual(result["report"]["outcome"], "TIMED_OUT")
        self.assertEqual(events, [{"kind": EVENT_KIND, "stage": "author", "attempt": 0, "status": "started"}])

    def test_progress_preserves_the_complete_input_fingerprint_rule(self):
        events, first, request = self.runner()
        self.assertTrue(events)
        inputs = {key: value for key, value in request.items() if key != "python"}
        expected = hashlib.sha256(json.dumps(inputs, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(first["inputFingerprint"], expected)
        _, same, _ = self.runner(python=sys.executable + ".missing")
        self.assertEqual(same["inputFingerprint"], expected)
        _, changed, _ = self.runner(pythonVersion="3.15.0")
        self.assertNotEqual(changed["inputFingerprint"], expected)
        for receipts in first["receipts"].values():
            for receipt in receipts:
                self.assertEqual(receipt["inputFingerprint"], expected)


if __name__ == "__main__":
    unittest.main()
