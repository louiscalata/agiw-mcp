#!/usr/bin/env python3
# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Check this checkout against a local clone of AGIW Suite.

Usage: PYTHONPATH=src python3 scripts/suite_compat.py /path/to/agiw-suite

AGIW Suite does not start agiw-mcp. The shared pieces are the vendored Nisi
v0.2.0 tree, the LICENSE bytes, and the Jev model and endpoint pin.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

from agiw_mcp.config import DEFAULT_JEV_MODEL
from agiw_mcp.consent import JEV_ENDPOINT
from agiw_mcp.nisi import NisiError, verify_vendor

REPO = Path(__file__).resolve().parents[1]
_REQUIRES = re.compile(r'(?m)^requires-python\s*=\s*"([^"]+)"\s*$')


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _requires(path: Path) -> str:
    match = _REQUIRES.search(path.read_text(encoding="utf-8"))
    if match is None:
        _fail(f"no requires-python in {path}")
    return match.group(1)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        _fail("usage: suite_compat.py SUITE_CHECKOUT")
    suite = Path(args[0]).resolve()
    if not (suite / "vendor" / "nisi" / "manifest.json").is_file():
        _fail(f"not an agiw-suite checkout: {suite}")
    try:
        done = subprocess.run(["git", "-C", str(suite), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.CalledProcessError):
        _fail("could not read the suite commit")
    sha = done.stdout.strip()
    mcp_license = _sha256(REPO / "LICENSE")
    suite_license = _sha256(suite / "LICENSE")
    if mcp_license != suite_license:
        _fail("LICENSE bytes differ from agiw-suite")
    try:
        nisi = verify_vendor(suite / "vendor" / "nisi")
    except NisiError as exc:
        _fail("suite Nisi bundle failed agiw-mcp verification: " + exc.code)
    swift = (suite / "JevKeychain.swift").read_text(encoding="utf-8")
    if f'"{DEFAULT_JEV_MODEL}"' not in swift:
        _fail(f"JevKeychain.swift does not pin {DEFAULT_JEV_MODEL}")
    if JEV_ENDPOINT not in swift:
        _fail(f"JevKeychain.swift does not use {JEV_ENDPOINT}")
    print(f"agiw-suite {sha}")
    print(f"nisi {nisi['version']} files={nisi['files']} source={nisi['sourceCommit']}")
    print(f"license {mcp_license}")
    print(f"jev model {DEFAULT_JEV_MODEL}")
    print(f"jev endpoint {JEV_ENDPOINT}")
    print(f"requires-python agiw-mcp {_requires(REPO / 'pyproject.toml')}; "
          f"agiw-suite {_requires(suite / 'pyproject.toml')}")
    print("suite compatibility passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
