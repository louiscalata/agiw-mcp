# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
import os
import shutil
import signal
import time
import unittest
from unittest import mock

from support import IsolatedTestCase, write_fake_node

from agiw_mcp import nisi
from agiw_mcp.config import load_config
from agiw_mcp.errors import ToolError


@unittest.skipIf(os.name != "posix", "fake runners use Python shebangs")
class BoundedRunTests(IsolatedTestCase):
    def test_bounded_run_overflow_stops_the_child(self):
        fake = write_fake_node(self.tmp / "bin", "import os, time\nos.write(1, b'x' * 200000)\ntime.sleep(30)\n")
        started = time.monotonic()
        with mock.patch.object(nisi, "STDOUT_CAP", 1024):
            code, out, state = nisi._bounded_run([str(fake)], data=b"", cwd=str(self.tmp),
                                                 env=dict(os.environ), timeout=10)
        self.assertEqual(state, "overflow")
        self.assertLess(time.monotonic() - started, 5)
        self.assertLessEqual(len(out), 1024)
        self.assertEqual(code, -signal.SIGKILL)

    def test_bounded_run_timeout_kills_grandchildren(self):
        pid_file = self.tmp / "grandchild.pid"
        fake = write_fake_node(self.tmp / "bin", "import pathlib, subprocess, time\n"
                               "child = subprocess.Popen(['sleep', '30'])\n"
                               f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
                               "time.sleep(30)\n")
        _, _, state = nisi._bounded_run([str(fake)], data=b"", cwd=str(self.tmp),
                                        env=dict(os.environ), timeout=0.5)
        self.assertEqual(state, "timeout")
        pid = int(pid_file.read_text())

        def cleanup():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        self.addCleanup(cleanup)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail("runner's grandchild survived the timeout")


class VendorCopyTests(IsolatedTestCase):
    def tampered_vendor(self):
        root = self.tmp / "vendor"
        shutil.copytree(nisi.VENDOR_DIR, root)
        nisi.verify_vendor(root)
        target = root / "package" / "workflow" / "engine.mjs"
        target.write_bytes(target.read_bytes() + b"\n// changed after verification\n")
        return root, target.relative_to(root)

    def test_copy_verified_refuses_a_file_changed_after_verify(self):
        root, changed = self.tampered_vendor()
        destination = self.tmp / "copy"
        with self.assertRaises(nisi.NisiError) as ctx:
            nisi._copy_verified(root, destination)
        self.assertEqual(ctx.exception.code, "NISI_HASH_MISMATCH")
        self.assertFalse((destination / "nisi" / changed).exists())

    def test_run_reports_nisi_unverified_when_copy_fails(self):
        root, _ = self.tampered_vendor()
        self.write_config(author_model="author-model", reviewer_model="reviewer-model")
        parent = self.tmp / "runs"
        parent.mkdir()
        with mock.patch.object(nisi, "verify_vendor", return_value={}), \
                mock.patch.object(nisi, "VENDOR_DIR", root), \
                mock.patch.object(nisi, "find_node", return_value=("unused-node", (24, 0, 0))), \
                mock.patch.object(nisi, "_bounded_run") as runner:
            with self.assertRaises(ToolError) as ctx:
                nisi.run({"task": "Add a module", "new_files": ["a.py"]}, load_config(),
                         temp_parent=str(parent))
        self.assertEqual(ctx.exception.code, "NISI_UNVERIFIED")
        self.assertEqual(ctx.exception.details["nisiCode"], "NISI_HASH_MISMATCH")
        runner.assert_not_called()
        self.assertEqual(list(parent.iterdir()), [])

    @unittest.skipIf(os.name != "posix", "POSIX symlinks")
    def test_write_new_refuses_an_existing_or_symlinked_target(self):
        outside = self.tmp / "outside"
        link = self.tmp / "link"
        link.symlink_to(outside)
        with self.assertRaises(FileExistsError):
            nisi._write_new(link, b"replacement")
        self.assertFalse(outside.exists())
        plain = self.tmp / "plain"
        plain.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            nisi._write_new(plain, b"replacement")
        self.assertEqual(plain.read_bytes(), b"original")


class RunBoundaryTests(IsolatedTestCase):
    def test_run_maps_overflow_to_output_too_large(self):
        self.write_config(author_model="author-model", reviewer_model="reviewer-model")
        parent = self.tmp / "runs"
        parent.mkdir()
        with mock.patch.object(nisi, "find_node", return_value=("unused-node", (24, 0, 0))), \
                mock.patch.object(nisi, "permission_flags", return_value=([], "none")), \
                mock.patch.object(nisi, "_bounded_run", return_value=(None, b"", "overflow")):
            with self.assertRaises(ToolError) as ctx:
                nisi.run({"task": "Add a module", "new_files": ["a.py"]}, load_config(),
                         temp_parent=str(parent))
        self.assertEqual(ctx.exception.code, "NISI_OUTPUT_TOO_LARGE")
        self.assertEqual(list(parent.iterdir()), [])


class SummarizeTests(IsolatedTestCase):
    def summarize(self, files, *, outcome="COMPLETED", original="original"):
        request = nisi.validate_request({"task": "Change files", "files": [{"path": "a.py", "content": original}],
                                         "new_files": ["b.py"]})
        return nisi._summarize({"report": {"outcome": outcome, "candidate": {"files": files}}}, request, "none")

    def test_unchanged_status(self):
        result = self.summarize([{"path": "a.py", "content": "original"}])
        self.assertEqual(result["proposedFiles"], [{"path": "a.py", "status": "unchanged", "content": "original"}])

    def test_proposed_files_over_the_cap_are_refused(self):
        with mock.patch.object(nisi, "MAX_OUTPUT_CONTENT_BYTES", 10):
            for files in ([{"path": "a.py", "content": "x" * 11}],
                          [{"path": "a.py", "content": "x" * 6}, {"path": "b.py", "content": "x" * 5}],
                          [{"path": "a.py", "content": "é" * 6}]):
                with self.subTest(files=files), self.assertRaises(ToolError) as ctx:
                    self.summarize(files)
                self.assertEqual(ctx.exception.code, "NISI_OUTPUT_TOO_LARGE")
            result = self.summarize([{"path": "a.py", "content": "é" * 5}])
        self.assertEqual(result["proposedFiles"][0]["content"], "é" * 5)

    def test_non_completed_outcome_note(self):
        result = self.summarize([], outcome="FAILED")
        self.assertFalse(result["completed"])
        self.assertIn("did not complete", result["note"])
