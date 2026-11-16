# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Replay boundary-first observations captured from the original Jev analysis.

The corpus records complete results, exception identity and ordered calls. Its
expected values are generated from the pinned source, never computed here.
"""

from __future__ import annotations

import builtins
from contextlib import ExitStack
import copy
import json
import math
from pathlib import Path
import unittest
from unittest import mock

import support  # noqa: F401 - installs the existing test import path and socket guard

from agiw_mcp import jev
from agiw_mcp.config import Config

CORPUS = Path(__file__).parent / "test_support" / "jev_analyse_golden" / "cases.json"
FAKE_KEY = "fixture-key"


def decode(value):
    """Materialize JSON-safe nonfinite values, odd mappings and long text."""
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$float"}:
        return float(value["$float"])
    if set(value) == {"$pairs"}:
        return {decode(key): decode(item) for key, item in value["$pairs"]}
    if set(value) == {"$text"}:
        spec = value["$text"]
        return spec.get("prefix", "") + spec["unit"] * spec["repeat"] + spec.get("suffix", "")
    return {key: decode(item) for key, item in value.items()}


def snapshot(value):
    """Keep equality deterministic even for NaN and exception argument tuples."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"$float": "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"}
    if isinstance(value, (list, tuple)):
        return [snapshot(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            return {"$pairs": [[snapshot(key), snapshot(item)] for key, item in value.items()]}
        return {key: snapshot(item) for key, item in value.items()}
    return value


def serialize_observation(value):
    """Preserve numeric and boolean types, signed zero and mapping order."""
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _exception(spec, module):
    kind = spec["type"]
    if kind == "HttpFailure":
        return module.HttpFailure(spec["kind"], spec.get("status"))
    if kind == "NetworkRefused":
        return module.NetworkRefused("fixture upstream text")
    if kind == "KeyUnavailable":
        return module.keys.KeyUnavailable()
    return getattr(builtins, kind)(spec.get("message", "fixture upstream text"))


def _request_outcome(request, module):
    if "raise" in request:
        raise _exception(request["raise"], module)
    if "raw_return" in request:
        return decode(request["raw_return"])
    return decode(request.get("status", 200)), decode(request["response"])


def replay(case, module=jev):
    """Run one case without reading keys/files, opening sockets or subprocesses."""
    calls = []
    counts = {}
    case = copy.deepcopy(case)
    config = Config(jev_model=case.get("requested_model", "jev-1.14.0"))

    def observe(name, **values):
        calls.append({"call": name, **snapshot(copy.deepcopy(values))})
        counts[name] = counts.get(name, 0) + 1
        fault = case.get("faults", {}).get(name)
        if fault and counts[name] == fault.get("at", 1):
            raise _exception(fault, module)

    def consent_recorded():
        observe("consent_recorded")
        return case.get("consent", True)

    def read_key(actual_config):
        observe("read_key", model=actual_config.jev_model)
        return decode(case.get("key", FAKE_KEY))

    def endpoint():
        observe("endpoint")
        return "https://fixture.invalid/analysis"

    def monotonic():
        readings = case.get("clock", [10.0, 10.25])
        value = readings[min(counts.get("monotonic", 0), len(readings) - 1)]
        observe("monotonic", value=value)
        return value

    def record(**values):
        observe("record", **values)

    def request_json(url, **values):
        observe("request_json", url=url, **values)
        return _request_outcome(case["request"], module)

    with ExitStack() as stack:
        for owner, name, replacement in (
            (module.consent, "consent_recorded", consent_recorded),
            (module.keys, "read_key", read_key),
            (module, "endpoint", endpoint),
            (module.time, "monotonic", monotonic),
            (module.activity, "record", record),
            (module, "request_json", request_json),
        ):
            stack.enter_context(mock.patch.object(owner, name, side_effect=replacement))
        # Fail immediately if a future change bypasses the patched boundaries.
        for target in (
            "builtins.open",
            "io.open",
            "os.open",
            "socket.socket.connect",
            "socket.socket.connect_ex",
            "socket.socket.bind",
            "socket.getaddrinfo",
            "socket.create_connection",
            "subprocess.Popen",
        ):
            stack.enter_context(mock.patch(target, side_effect=AssertionError("unexpected external operation")))
        try:
            result = module.analyse(decode(case["phase"]), decode(case["text"]), config)
        except Exception as error:  # noqa: BLE001 - exception identity is part of the recorded behavior
            observed = {
                "exception": {
                    "type": type(error).__name__,
                    "args": snapshot(error.args),
                    "suppress_context": error.__suppress_context__,
                }
            }
            if isinstance(error, module.ToolError):
                observed["exception"]["payload"] = error.payload()
        else:
            observed = {"result": snapshot(result)}
    return {**observed, "calls": calls}


def corpus_cases(group=None):
    document = json.loads(CORPUS.read_text(encoding="utf-8"))
    if document["schema_version"] != 1:
        raise ValueError("Unknown Jev corpus schema")
    cases = document["cases"]
    names = [case["name"] for case in cases]
    if len(names) != len(set(names)) or not cases:
        raise ValueError("Empty corpus or duplicate Jev case name")
    return [case for case in cases if group is None or case["group"] == group]


def install_cases(test_class, group):
    cases = corpus_cases(group)
    if not cases:
        raise ValueError("Empty Jev corpus group")

    def golden_test(case):
        def test(self):
            self.maxDiff = None
            # Built-in exception wording can vary between supported Python
            # versions. Every accepted variant was captured from original source.
            self.assertIn(
                serialize_observation(replay(case)),
                [
                    serialize_observation(expected)
                    for expected in [case["expected"], *case.get("expected_variants", [])]
                ],
            )

        return test

    for case in cases:
        setattr(test_class, "test_" + case["name"], golden_test(case))


class JevAnalyseGoldenTests(unittest.TestCase):
    """Input limits, answer boundaries, precision and ordering."""


def load_tests(loader, standard_tests, pattern):
    install_cases(JevAnalyseGoldenTests, "boundaries")
    return loader.loadTestsFromTestCase(JevAnalyseGoldenTests)


if __name__ == "__main__":
    unittest.main()
