#!/usr/bin/env python3
# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Fail if the repository contains private paths, hosts, addresses or secrets.

Usage: python3 scripts/scrub_check.py [ROOT]

Scans every file under ROOT (default: the repository root), except .git, local
virtual environments and caches. Only this file's own pattern definitions,
between the scrub-patterns markers, are allowlisted. Matches are reported as
path:line and rule name; the matched text itself is never printed.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache",
             ".pytest_cache", "build", "dist"}
SKIP_SUFFIXES = (".egg-info",)
MAX_FILE_BYTES = 8 * 1024 * 1024

# scrub-patterns-begin
RULES = [
    ("private-home-macos", re.compile(r"/Users/louiscalata")),
    ("private-home-linux", re.compile(r"/home/louiscalata")),
    ("private-user-at", re.compile(r"louiscalata@")),
    ("private-host", re.compile(r"louisaurorar12", re.IGNORECASE)),
    ("rfc1918-10", re.compile(r"(?<![\d.])10\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])")),
    ("rfc1918-172", re.compile(r"(?<![\d.])172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}(?![\d.])")),
    ("rfc1918-192", re.compile(r"(?<![\d.])192\.168\.\d{1,3}\.\d{1,3}(?![\d.])")),
    ("mdns-local-host", re.compile(r"(?<![\w/~.-])[a-z0-9][a-z0-9-]*\.local\b(?![(\w-])", re.IGNORECASE)),
    ("notion-url", re.compile(r"app\.notion\.com|notion\.so\b", re.IGNORECASE)),
    ("private-share", re.compile(r"sharedchami", re.IGNORECASE)),
    ("api-key-sk", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("bearer-token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("pem-private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
]
# scrub-patterns-end

SELF = Path(__file__).resolve()


def _allowlisted_lines(path: Path, lines: list[str]) -> set[int]:
    if path.resolve() != SELF:
        return set()
    allowed, inside = set(), False
    for number, line in enumerate(lines, start=1):
        if line.strip() == "# scrub-patterns-begin":
            inside = True
        elif line.strip() == "# scrub-patterns-end":
            inside = False
        if inside:
            allowed.add(number)
    return allowed


def scan(root: Path) -> list[tuple[str, int, str]]:
    findings = []
    root = root.resolve()
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(SKIP_SUFFIXES))
        for name in sorted(files):
            path = Path(base, name)
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                findings.append((str(path.relative_to(root)), 0, "file-too-large-to-scan"))
                continue
            text = path.read_bytes().decode("utf-8", errors="replace")
            lines = text.splitlines()
            allowed = _allowlisted_lines(path, lines)
            for number, line in enumerate(lines, start=1):
                if number in allowed:
                    continue
                for rule, pattern in RULES:
                    if pattern.search(line):
                        findings.append((str(path.relative_to(root)), number, rule))
    return findings


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1]
    findings = scan(root)
    for path, line, rule in findings:
        print(f"{path}:{line}: {rule}")
    if findings:
        print(f"scrub check FAILED: {len(findings)} finding(s)")
        return 1
    print("scrub check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
