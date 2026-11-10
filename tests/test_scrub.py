# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
import hashlib
import importlib.util
import unittest
from unittest import mock

from support import ROOT, IsolatedTestCase

_spec = importlib.util.spec_from_file_location("scrub_check", ROOT / "scripts" / "scrub_check.py")
scrub_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scrub_check)

# Samples are assembled at run time so this file never contains them literally.
OWNER = "louis" + "calata"
SAMPLES = {
    "private-home-macos": "/Users/" + OWNER + "/project",
    "private-home-linux": "/home/" + OWNER + "/project",
    "private-user-at": OWNER + "@" + "example",
    "rfc1918-10": "10.0" + ".4.7",
    "rfc1918-172": "172.20" + ".1.1",
    "rfc1918-192": "192.168" + ".0.10",
    "mdns-local-host": "studio-mac" + ".local",
    "notion-url": "https://app." + "notion.com/p/abc",
    "private-share": "Shared" + "Chami",
    "api-key-sk": "sk" + "-" + "A" * 24,
    "bearer-token": "Bearer" + " " + "b" * 32,
    "pem-private-key": "-----BEGIN " + "RSA PRIVATE KEY-----",
}


class ScrubTests(IsolatedTestCase):
    def test_repository_is_clean(self):
        findings = scrub_check.scan(ROOT)
        self.assertEqual(findings, [], "\n".join(f"{p}:{n}: {r}" for p, n, r in findings))

    def test_a_worktree_git_pointer_is_skipped_only_at_the_root(self):
        pointer = "gitdir: " + SAMPLES["private-home-macos"] + "/.git/worktrees/x\n"
        (self.tmp / ".git").write_text(pointer)
        self.assertEqual(scrub_check.scan(self.tmp), [])
        nested = self.tmp / "docs"
        nested.mkdir()
        (nested / ".git").write_text(pointer)
        found = [(p.replace("\\", "/"), r) for p, _, r in scrub_check.scan(self.tmp)]
        self.assertEqual(found, [("docs/.git", "private-home-macos")])

    def test_each_rule_fires(self):
        for rule, sample in SAMPLES.items():
            folder = self.tmp / rule
            folder.mkdir()
            (folder / "file.txt").write_text("ok line\nvalue = " + sample + "\n", encoding="utf-8")
            findings = scrub_check.scan(folder)
            self.assertIn(("file.txt", 2, rule), findings, rule)

    def test_private_host_matches_a_whole_token_by_digest(self):
        # The shipped digests name real machines, so a stand-in digest is patched in.
        digest = hashlib.sha256(b"example-studio-pc").hexdigest()
        folder = self.tmp / "host"
        folder.mkdir()
        (folder / "a.txt").write_text("ok line\nhost EXAMPLE-Studio-PC.json\nexample-studio-pcx\n", encoding="utf-8")
        with mock.patch.object(scrub_check, "PRIVATE_HOST_SHA256", frozenset({digest})):
            self.assertEqual(scrub_check.scan(folder), [("a.txt", 2, "private-host")])

    def test_private_host_digests_are_sha256(self):
        self.assertTrue(scrub_check.PRIVATE_HOST_SHA256)
        for digest in scrub_check.PRIVATE_HOST_SHA256:
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_safe_lookalikes_pass(self):
        folder = self.tmp / "safe"
        folder.mkdir()
        (folder / "a.txt").write_text(
            "http://127.0.0.1:1234/v1\n~/.local/share\nversion 110.0.1.2\nthreading.local()\n"
            "github.com/" + OWNER + "/agiw-mcp@v0.1.0\nBearer <key>\n", encoding="utf-8")
        self.assertEqual(scrub_check.scan(folder), [])


if __name__ == "__main__":
    unittest.main()
