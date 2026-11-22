import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


class RunUnittestTests(unittest.TestCase):
    def test_exit_status_for_uncaught_failures(self):
        runner = Path(__file__).resolve().parents[1] / "scripts" / "run_unittest.py"
        cases = (
            ("pass", 0, ""),
            (
                "worker = threading.Thread(target=sys.exit)\nworker.start()\nworker.join()",
                0,
                "",
            ),
            ('warnings.warn("ordinary warning", UserWarning)', 1, "UserWarning: ordinary warning"),
            (
                'class Finalizer:\n    def __del__(self):\n        warnings.warn("finalizer warning", ResourceWarning)'
                "\nvalue = Finalizer()\ndel value",
                1,
                "ResourceWarning: finalizer warning",
            ),
            (
                'worker = threading.Thread(target=warnings.warn, args=("thread warning", ResourceWarning))'
                "\nworker.start()\nworker.join()",
                1,
                "ResourceWarning: thread warning",
            ),
            (
                'atexit.register(warnings.warn, "shutdown warning", ResourceWarning)',
                1,
                "ResourceWarning: shutdown warning",
            ),
        )
        header = (
            "import atexit, sys, threading, unittest, warnings\n\n"
            "class Fixture(unittest.TestCase):\n"
            "    def test_boundary(self):\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "test_warning_boundary.py"
            for source, status, diagnostic in cases:
                with self.subTest(source=source):
                    fixture.write_text(header + textwrap.indent(source, "        ") + "\n", encoding="utf-8")
                    result = subprocess.run(
                        [sys.executable, "-B", str(runner), "discover", "-s", directory, "-q"],
                        cwd=directory,
                        env=dict(os.environ, PYTHONWARNINGS="error"),
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(result.returncode, status, result.stderr)
                    if diagnostic:
                        self.assertIn(diagnostic, result.stderr)
                    else:
                        self.assertNotIn("Error", result.stderr)
                        self.assertNotIn("Warning", result.stderr)
