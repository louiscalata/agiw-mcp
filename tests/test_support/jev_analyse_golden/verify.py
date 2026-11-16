# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Run focused in-memory mutation and pinned-source differential checks.

Run from the repository root with --worktree . and optionally --report FILE.
No production files are changed, and every replay blocks external operations.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
from pathlib import Path
import random
import sys
import types
import unittest
from unittest import mock

import generate


def source_module(source, name):
    module = types.ModuleType("agiw_mcp." + name)
    module.__package__ = "agiw_mcp"
    exec(compile(source, generate.SOURCE, "exec"), module.__dict__)
    return module


def mutations():
    return [
        ("answer_integer", "round(float(value), 4)", "round(value, 4)"),
        (
            "flag_integer",
            '"flagged": is_flagged(question, answer),',
            '"flagged": int(is_flagged(question, answer)),',
        ),
        ("confidence_integer", 'round(float(answer["confidence"]), 4)', 'round(answer["confidence"], 4)'),
        (
            "row_key_order",
            '"id": qid,\n            "type": question["type"],',
            '"type": question["type"],\n            "id": qid,',
        ),
        (
            "result_key_order",
            '"advisory": ADVISORY,\n        "phase": phase,',
            '"phase": phase,\n        "advisory": ADVISORY,',
        ),
        (
            "truthy_text_accepted",
            "if not isinstance(text, str) or not text.strip():",
            "if not text or (isinstance(text, str) and not text.strip()):",
        ),
        (
            "truthy_text_stringified",
            "if not isinstance(text, str) or not text.strip():",
            "if not text or not str(text).strip():",
        ),
        ("served_model_prefix", "SERVED_MODEL.match(served)", "SERVED_MODEL.search(served)"),
        ("served_model_separator", r"jev-[0-9]{1,3}\.", r"jev-[0-9]{1,3}."),
        ("question_count_only", "set(answers) != set(questions)", "len(answers) != len(questions)"),
    ]


def flattened(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flattened(item)
        else:
            yield item


def mutation_checks(driver, source):
    failures = importlib.import_module("test_jev_analyse_golden_failures")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([loader.loadTestsFromModule(driver), loader.loadTestsFromModule(failures)])
    tests = [(type(test), test._testMethodName) for test in flattened(suite)]
    baseline = unittest.TestResult()
    suite.run(baseline)
    if not baseline.wasSuccessful():
        raise RuntimeError("Golden baseline failed")
    replay = driver.replay
    records = []
    for name, old, new in mutations():
        if source.count(old) != 1:
            raise ValueError("Mutation anchor is not unique: " + name)
        module = source_module(source.replace(old, new, 1), name)
        result = unittest.TestResult()
        first_failure = None
        with mock.patch.object(driver, "replay", side_effect=lambda case, module=module: replay(case, module)):
            for test_class, method in tests:
                test = test_class(method)
                test.run(result)
                if not result.wasSuccessful():
                    first_failure = test.id()
                    break
        records.append(
            {
                "name": name,
                "killed_by_assertion": bool(result.failures),
                "errors": len(result.errors),
                "tests_executed": result.testsRun,
                "first_failing_test": first_failure,
            }
        )
    return {
        "baseline_tests": baseline.testsRun,
        "attempted": len(records),
        "killed_by_assertion": sum(record["killed_by_assertion"] for record in records),
        "survived": sum(record["first_failing_test"] is None for record in records),
        "errors": sum(record["errors"] for record in records),
        "mutants": records,
    }


def extra_inputs(module, count, seed):
    rng = random.Random(seed)
    values = [None, False, True, -1, 0, 0.5, 1, 10**30, "0", "", [], ["x"], {}, {"x": 1}, {"$float": "nan"}]
    cases = []
    for index in range(count):
        phase = rng.choice(tuple(module.QUESTIONS))
        questions = module.QUESTIONS[phase]
        response = {
            "model": "jev-1.14.0",
            "answers": {
                qid: {"type": question["type"], question["type"]: 0, "confidence": 0.8}
                for qid, question in questions.items()
            },
        }
        case = {
            "name": f"extra_{index}",
            "phase": phase,
            "text": "Inspect this change.",
            "request": {"response": response},
        }
        qid = rng.choice(tuple(questions))
        field = rng.choice(("type", "confidence", questions[qid]["type"]))
        mode = index % 8
        if mode == 0:
            case["text"] = copy.deepcopy(rng.choice([*values, "\x00", "\ud800", "x" * 12289]))
        elif mode == 1:
            case["requested_model"] = "jev-latest"
            response["model"] = rng.choice([*values, "note jev-1.2.3", "jev-1x2.3", "jev-1.2x3", "jev-1.2.3"])
        elif mode == 2:
            response["answers"]["renamed_" + qid] = response["answers"].pop(qid)
        elif mode == 3:
            response["answers"][qid][field] = copy.deepcopy(rng.choice(values))
        elif mode == 4:
            case["phase"] = copy.deepcopy(rng.choice([*values, "unknown", "post", "pre"]))
        elif mode == 5:
            case["request"] = {
                "raise": {"type": "HttpFailure", "kind": rng.choice(("busy", "http", "timeout")), "status": 429}
            }
        elif mode == 6:
            response["usage"] = {"tokens": rng.choice(values), "other": rng.randrange(10**9 + 1)}
        else:
            case["faults"] = {rng.choice(("consent_recorded", "read_key", "endpoint", "record")): {"type": "OSError"}}
        cases.append(case)
    return cases


def differential_checks(driver, original, cases, extras):
    differences = []
    golden_mismatches = []
    nondeterministic = []
    serialize = driver.serialize_observation
    for case in [*cases, *extras]:
        reference = serialize(driver.replay(case, original))
        if serialize(driver.replay(case)) != reference:
            differences.append(case["name"])
        if serialize(driver.replay(case, original)) != reference:
            nondeterministic.append(case["name"])
        if "expected" in case and reference not in [
            serialize(expected) for expected in [case["expected"], *case.get("expected_variants", [])]
        ]:
            golden_mismatches.append(case["name"])
    return {
        "cases": len(cases) + len(extras),
        "corpus_cases": len(cases),
        "extra_cases": len(extras),
        "differences": differences,
        "golden_mismatches": golden_mismatches,
        "nondeterministic": nondeterministic,
        "extra_inputs_sha256": hashlib.sha256(serialize(extras).encode()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--extra-cases", type=int, default=512)
    parser.add_argument("--seed", type=int, default=0x4A4556)
    args = parser.parse_args()
    if args.extra_cases < 0:
        parser.error("--extra-cases must be nonnegative")
    worktree = args.worktree.resolve()
    sys.path.insert(0, str(worktree / "tests"))
    driver = importlib.import_module("test_jev_analyse_golden")
    source_path = worktree / generate.SOURCE
    source = source_path.read_bytes()
    original = generate.git(worktree, "show", generate.BASE + ":" + generate.SOURCE)
    document = json.loads(driver.CORPUS.read_text(encoding="utf-8"))
    original_hash = hashlib.sha256(original).hexdigest()
    if original_hash != document["generated_from"]["jev_sha256"]:
        raise RuntimeError("Pinned source does not match the corpus")
    reference = source_module(original, "jev_reference")
    differential = differential_checks(
        driver, reference, driver.corpus_cases(), extra_inputs(reference, args.extra_cases, args.seed)
    )
    mutation = mutation_checks(driver, source.decode("utf-8"))
    after_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    report = {
        "python": sys.version.split()[0],
        "revision": generate.BASE,
        "original_source_sha256": original_hash,
        "source_sha256_before": hashlib.sha256(source).hexdigest(),
        "source_sha256_after": after_hash,
        "corpus_sha256": hashlib.sha256(driver.CORPUS.read_bytes()).hexdigest(),
        "seed": args.seed,
        "differential": differential,
        "mutation": mutation,
    }
    content = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.write_text(content, encoding="utf-8")
    sys.stdout.write(content)
    if (
        differential["differences"]
        or differential["golden_mismatches"]
        or differential["nondeterministic"]
        or mutation["killed_by_assertion"] != mutation["attempted"]
        or mutation["errors"]
        or hashlib.sha256(source).hexdigest() != after_hash
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
