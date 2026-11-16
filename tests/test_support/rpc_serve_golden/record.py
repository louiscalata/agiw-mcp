# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Reproduce saved expectations using the corpus's pinned production revision.

Run from the repository root with PYTHONPATH=tests. Inputs and schedules come
from cases.json; expectations are captured from a temporary Git export of src.
The default is a byte-for-byte check. Use --write to replace expectations after
deliberately updating inputs or the pinned revision and source hash.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[3]
CORPUS = Path(__file__).with_name("cases.json")


def export_source(revision, destination):
    """Export only regular source files without consulting inherited Git overrides."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")}
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", revision, "src"],
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        timeout=30,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            path = destination / member.name
            if not path.resolve().is_relative_to(destination) or not (member.isfile() or member.isdir()):
                raise ValueError("unexpected source archive member")
            if member.isfile():
                path.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source:
                    path.write_bytes(source.read())


def capture(document, destination):
    """Load the shared replay driver against the exported package, never the checkout."""
    os.environ["AGIW_MCP_TEST_INSTALLED"] = "1"
    sys.path[:0] = [str(destination / "src"), str(ROOT / "tests")]
    from test_support.rpc_serve_golden import support

    if Path(support.rpc.__file__).resolve() != destination / "src/agiw_mcp/rpc.py":
        raise RuntimeError("RPC import does not match the pinned source export")
    names = [case["name"] for case in document["cases"]]
    if len(names) != len(set(names)):
        raise ValueError("duplicate case name")
    for case in document["cases"]:
        expected = support.replay(case)
        failures = [expected["raised"]] + [entry[2] for entry in expected["calls"] if entry[0] == "thread.failure"]
        if any(failure and failure["type"] == "ScheduleError" for failure in failures):
            raise RuntimeError(f"invalid schedule: {case['name']}")
        case["expected"] = expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="replace expectations with captured transcripts")
    args = parser.parse_args()
    original = CORPUS.read_bytes()
    document = json.loads(original)
    metadata = document["generated_from"]
    if not re.fullmatch(r"[0-9a-f]{40}", metadata["revision"]):
        raise ValueError("the corpus must pin a complete Git revision")
    with tempfile.TemporaryDirectory(prefix="rpc-golden-record-") as temp:
        destination = Path(temp).resolve()
        export_source(metadata["revision"], destination)
        source = (destination / "src/agiw_mcp/rpc.py").read_bytes()
        if hashlib.sha256(source).hexdigest() != metadata["rpc_sha256"]:
            raise ValueError("the pinned RPC source hash does not match")
        capture(document, destination)
    metadata["generator"] = "tests/test_support/rpc_serve_golden/record.py"
    recorded = (json.dumps(document, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    matches = original == recorded
    if args.write:
        CORPUS.write_bytes(recorded)
    print(  # noqa: T201 - this recorder reports a check result
        json.dumps(
            {
                "cases": len(document["cases"]),
                "bytes": len(recorded),
                "sha256": hashlib.sha256(recorded).hexdigest(),
                "matches": matches,
                "written": args.write,
            }
        )
    )
    return 0 if matches or args.write else 1


if __name__ == "__main__":
    raise SystemExit(main())
