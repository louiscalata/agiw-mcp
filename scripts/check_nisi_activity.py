# Copyright 2026 AGIW MCP contributors
# SPDX-License-Identifier: Apache-2.0
"""Deterministic, local-only checks for the optional Nisi activity record.

Run from any directory. The baseline comes from a local git ref; no network
access or installation is performed. All disposable files live under TMPDIR.
The full repository test suite is never selected.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile
import types
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
GOOD = {
    "outcome": "COMPLETED",
    "inputFingerprint": "a" * 64,
    "candidateFingerprint": "b" * 64,
    "repairAttempts": 0,
    "sandbox": "node-permission",
    "ticketed": False,
    "nisiVersion": "0.2.0",
    "elapsedMs": 12,
}
STAMP = "2026-01-02T03:04:05Z"


class FixedDatetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 3, 4, 5, 987654, tzinfo=dt.timezone.utc).astimezone(tz)


def require(condition: bool, description: str) -> None:
    if not condition:
        raise AssertionError(description)


def clean_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")}


def load_baseline(ref: str):
    source = subprocess.check_output(
        ["git", "-C", str(ROOT), "show", f"{ref}:src/agiw_mcp/activity.py"],
        env=clean_environment(),
        text=True,
    )
    module = types.ModuleType("agiw_mcp._activity_baseline")
    module.__package__ = "agiw_mcp"
    exec(compile(source, "<baseline activity>", "exec"), module.__dict__)
    return module


def expected_line(fields: dict) -> bytes:
    return (json.dumps({"at": STAMP, **fields}, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def good_fields(rng: random.Random) -> dict:
    return {
        "outcome": rng.choice(["COMPLETED", "REPAIR_LIMIT", "REFUSED", "A", "A" * 64]),
        "inputFingerprint": rng.choice([None, "0" * 64, "a1" * 32, "f" * 64]),
        "candidateFingerprint": rng.choice([None, "1" * 64, "b2" * 32, "e" * 64]),
        "repairAttempts": rng.choice([None, 0, 1, 999, 1000]),
        "sandbox": rng.choice([None, "none", "node-permission"]),
        "ticketed": rng.choice([False, True]),
        "nisiVersion": rng.choice(["0.2.0", "1.0.0", "999.999.999", "001.002.003"]),
        "elapsedMs": rng.choice([0, 1, 12, 10**12]),
    }


def oracle_accepts(fields: dict) -> bool:
    if set(fields) != set(GOOD):
        return False
    for key in ("inputFingerprint", "candidateFingerprint"):
        value = fields[key]
        if value is not None and not (type(value) is str and re.fullmatch("[a-f0-9]{64}", value)):
            return False
    repairs = fields["repairAttempts"]
    sandbox = fields["sandbox"]
    return bool(
        type(fields["outcome"]) is str
        and re.fullmatch("[A-Z0-9_]{1,64}", fields["outcome"])
        and (repairs is None or (type(repairs) is int and repairs >= 0 and repairs <= 1000))
        and (sandbox is None or (type(sandbox) is str and sandbox in ("none", "node-permission")))
        and type(fields["ticketed"]) is bool
        and type(fields["nisiVersion"]) is str
        and re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", fields["nisiVersion"])
        and type(fields["elapsedMs"]) is int
        and fields["elapsedMs"] >= 0
    )


def generated_fields(rng: random.Random, index: int) -> dict:
    fields = good_fields(rng)
    if index % 4 == 0:
        return fields
    key = rng.choice(list(GOOD))
    if index % 11 == 0:
        fields.pop(key)
    elif index % 13 == 0:
        fields["unknown"] = "not stored"
    else:
        fields[key] = rng.choice(
            [None, True, False, -1, 1001, 0.5, "", "text\n", "a" * 63, "A" * 65, [], {}, "none", 0]
        )
    return fields


def golden_checks(activity, baseline, directory: Path) -> dict:
    current = directory / "golden-current"
    previous = directory / "golden-baseline"
    variants = [GOOD, {**GOOD, "repairAttempts": None, "sandbox": None}, {**GOOD, "repairAttempts": 1000}]
    with (
        mock.patch.object(activity, "config_dir", return_value=current),
        mock.patch.object(baseline, "config_dir", return_value=previous),
    ):
        for fields in variants:
            require(activity.record_nisi(**fields), "golden current append refused")
            require(baseline.record_nisi(**fields), "golden baseline append refused")
    expected = b"".join(expected_line(fields) for fields in variants)
    require((current / activity.NISI_LOG_FILE).read_bytes() == expected, "golden serialization differs")
    require((previous / baseline.NISI_LOG_FILE).read_bytes() == expected, "baseline serialization differs")
    return {"records": len(variants), "bytes": len(expected), "mismatches": 0}


def differential_checks(activity, baseline, directory: Path, count: int) -> dict:
    current = directory / "differential-current"
    previous = directory / "differential-baseline"
    expected = bytearray()
    accepted = 0
    rng = random.Random(20261116)
    with (
        mock.patch.object(activity, "config_dir", return_value=current),
        mock.patch.object(baseline, "config_dir", return_value=previous),
    ):
        for index in range(count):
            fields = generated_fields(rng, index)
            want = oracle_accepts(fields)
            now = activity.record_nisi(**fields)
            old = baseline.record_nisi(**fields)
            require(now is want, f"current acceptance differs at case {index}")
            require(old is want, f"baseline acceptance differs at case {index}")
            if want:
                accepted += 1
                expected.extend(expected_line(fields))
    require((current / activity.NISI_LOG_FILE).read_bytes() == expected, "differential current bytes differ")
    require((previous / baseline.NISI_LOG_FILE).read_bytes() == expected, "differential baseline bytes differ")
    return {"cases": count, "accepted": accepted, "refused": count - accepted, "mismatches": 0}


def normalized_fields(summary: dict, ticketed: bool) -> dict:
    code = summary.get("outcome")
    if type(code) is not str or not re.fullmatch("[A-Z0-9_]{1,64}", code):
        code = "REFUSED"
    result = {**GOOD, "outcome": code, "ticketed": ticketed, "elapsedMs": 125}
    for key in ("inputFingerprint", "candidateFingerprint"):
        value = summary.get(key)
        result[key] = value if type(value) is str and re.fullmatch("[a-f0-9]{64}", value) else None
    value = summary.get("repairAttempts")
    result["repairAttempts"] = value if type(value) is int and value >= 0 and value <= 1000 else None
    value = summary.get("sandbox")
    result["sandbox"] = value if type(value) is str and value in ("node-permission", "none") else None
    return result


def normalization_checks(activity, nisi, directory: Path, count: int) -> dict:
    current = directory / "normalization"
    rng = random.Random(16002611)
    expected = bytearray()
    repaired = 0
    with (
        mock.patch.object(activity, "config_dir", return_value=current),
        mock.patch.object(nisi.time, "monotonic", return_value=1.125),
    ):
        for index in range(count):
            summary = generated_fields(rng, index)
            summary.pop("elapsedMs", None)
            ticketed = index % 2 == 0
            want = normalized_fields(summary, ticketed)
            repaired += sum(summary.get(key) != want[key] for key in ("repairAttempts", "sandbox"))
            nisi._log_run(1.0, ticketed, summary)
            expected.extend(expected_line(want))
    require((current / activity.NISI_LOG_FILE).read_bytes() == expected, "normalization oracle differs")
    return {"cases": count, "safe_defaults": repaired, "mismatches": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="origin/main", help="local reference used for the differential")
    parser.add_argument("--cases", type=int, default=4000, help="generated direct-record differential cases")
    parser.add_argument("--normalization-cases", type=int, default=2000)
    args = parser.parse_args()
    require(args.cases > 0 and args.normalization_cases > 0, "case counts must be positive")
    sys.path.insert(0, str(ROOT / "src"))
    from agiw_mcp import activity, nisi

    baseline = load_baseline(args.baseline_ref)
    with (
        tempfile.TemporaryDirectory(prefix="nisi-activity-check-") as raw,
        mock.patch.object(activity.dt, "datetime", FixedDatetime),
    ):
        directory = Path(raw)
        results = {
            "golden": golden_checks(activity, baseline, directory),
            "differential": differential_checks(activity, baseline, directory, args.cases),
            "normalization": normalization_checks(activity, nisi, directory, args.normalization_cases),
        }
    print(json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    main()
