# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Capture golden observations from an authenticated, unmodified source export.

Run with --worktree, --scratch and optionally --output. No candidate source is
used to compute expectations. The input set is boundary-first and deterministic.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

BASE = "c8691268c5d1ce894ca8612a07ae2bf1fa6b1375"
SOURCE = "src/agiw_mcp/jev.py"
ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], env=ENV, check=True, capture_output=True, timeout=30).stdout


def inputs(module):  # noqa: C901 - authored fixture enumeration, not production policy
    cases = []

    def reply(phase):
        return {
            "model": "jev-1.14.0",
            "answers": {
                qid: {"type": question["type"], "score" if question["type"] == "score" else "noul": 0}
                for qid, question in module.QUESTIONS[phase].items()
            },
        }

    def add(name, phase="pre", group="boundaries", response=None, **changes):
        case = {
            "name": name,
            "group": group,
            "phase": phase,
            "text": "Check the supplied change.\n",
            "request": {"response": copy.deepcopy(reply(phase) if response is None else response)},
        }
        case.update(copy.deepcopy(changes))
        cases.append(case)

    # These boundary inputs are authored before any observed output is captured.
    for phase, limit in (("pre", module.consent.MAX_PRE_BYTES), ("post", module.consent.MAX_POST_BYTES)):
        for delta in (-1, 0, 1):
            add(f"{phase}_ascii_limit_{delta + 1}", phase, text={"$text": {"unit": "x", "repeat": limit + delta}})
            # The byte boundary splits a four-byte character in the oversized cases.
            add(
                f"{phase}_utf8_limit_{delta + 1}",
                phase,
                text={"$text": {"prefix": "x" * (4 + delta), "unit": "\U0001f30d", "repeat": (limit - 3) // 4}},
            )
        budget = limit - len(module.TRUNCATION_MARK.encode("utf-8"))
        for offset in range(4):
            add(
                f"{phase}_utf8_clip_offset_{offset}",
                phase,
                text={"$text": {"prefix": "a" * (budget % 4 + offset), "unit": "\U0001f30d", "repeat": limit // 4 + 1}},
            )
        for index, value in enumerate((None, "", " \t\n", False, 0, [], {}, "\x00", "x\x00y", "\ud800")):
            add(f"{phase}_invalid_text_{index:02d}", phase, text=value)
        add(f"{phase}_trailing_space_preserved", phase, text="  inspect \r\n\t ")
        add(f"{phase}_hostile_text", phase, text="Bearer fixture-key <tag>\u202e\n/relative-only\x1b[31m")

    for phase, questions in module.QUESTIONS.items():
        for qid, question in questions.items():
            field = "score" if question["type"] == "score" else "noul"
            maximum = len(question["criteria"]) - 1 if field == "score" else 1
            midpoint = maximum / 2
            values = [
                -0.000001,
                0,
                0.000001,
                midpoint - 0.000001,
                midpoint,
                midpoint + 0.000001,
                maximum - 0.000001,
                maximum,
                maximum + 0.000001,
                0.123449,
                0.12345,
                0.123451,
                -0.0,
            ]
            if field == "score":
                values.extend(level + delta for level in range(maximum + 1) for delta in (-0.000001, 0, 0.000001))
            for index, value in enumerate(values):
                response = reply(phase)
                response["answers"][qid][field] = value
                add(f"{phase}_{qid}_value_boundary_{index:02d}", phase, response=response)
            for index, value in enumerate(
                (-0.000001, 0, 0.000001, 0.123449, 0.12345, 0.123451, 0.999999, 1, 1.000001, None)
            ):
                response = reply(phase)
                response["answers"][qid]["confidence"] = value
                add(f"{phase}_{qid}_confidence_boundary_{index:02d}", phase, response=response)

    for phase in module.QUESTIONS:
        for count in (0, 1, 7, 8, 9, 10):
            response = reply(phase)
            response["usage"] = {f"counter_{index:02d}": index for index in reversed(range(count))}
            add(f"{phase}_usage_count_{count}", phase, response=response)
        for index, value in enumerate((-1, 0, 1, 10**9 - 1, 10**9, 10**9 + 1)):
            response = reply(phase)
            response["usage"] = {"tokens": value}
            add(f"{phase}_usage_value_boundary_{index}", phase, response=response)
        for length in (1, 39, 40, 41):
            response = reply(phase)
            response["usage"] = {"a" * length: 1}
            add(f"{phase}_usage_key_length_{length}", phase, response=response)
        for index, elapsed in enumerate((0, 0.000999, 0.001, 0.001999, 0.002, 0.999999, 1)):
            add(f"{phase}_elapsed_boundary_{index}", phase, clock=[0.0, elapsed])
        response = reply(phase)
        response["answers"] = dict(reversed(list(response["answers"].items())))
        for answer in response["answers"].values():
            answer["score" if answer["type"] == "score" else "noul"] = 1.5 if answer["type"] == "score" else 0.5
            answer["confidence"] = 0.567891
        add(f"{phase}_reversed_answers_all_flagged", phase, response=response)

    # Shape and failure inputs follow the boundary tranche.
    odd_values = [None, False, True, "0", [], {}, {"$float": "nan"}, {"$float": "inf"}, {"$float": "-inf"}]
    for phase, questions in module.QUESTIONS.items():
        for qid, question in questions.items():
            for index, value in enumerate(odd_values):
                for field in ("confidence", "score" if question["type"] == "score" else "noul"):
                    response = reply(phase)
                    response["answers"][qid][field] = value
                    add(f"{phase}_{qid}_{field}_odd_{index}", phase, "failures", response=response)
            for index, value in enumerate((None, [], "answer", False, 1, {})):
                response = reply(phase)
                response["answers"][qid] = value
                add(f"{phase}_{qid}_answer_shape_{index}", phase, "failures", response=response)
            for field in ("type", "score" if question["type"] == "score" else "noul"):
                response = reply(phase)
                response["answers"][qid].pop(field)
                add(f"{phase}_{qid}_missing_{field}", phase, "failures", response=response)
            for index, value in enumerate((None, False, "unknown", "score" if question["type"] == "noul" else "noul")):
                response = reply(phase)
                response["answers"][qid]["type"] = value
                add(f"{phase}_{qid}_type_wrong_{index}", phase, "failures", response=response)
            response = reply(phase)
            response["answers"].pop(qid)
            add(f"{phase}_missing_question_{qid}", phase, "failures", response=response)
        for field in ("noul", "score"):
            response = reply(phase)
            for answer in response["answers"].values():
                answer["confidence"] = 0.25
                answer["extra"] = "fixture-key-with-no-authority"
                answer["score" if field == "noul" else "noul"] = "ignored hostile field"
            add(f"{phase}_extra_answer_fields_{field}", phase, "failures", response=response)
        response = reply(phase)
        for answer in response["answers"].values():
            answer["confidence"] = 0.25
            answer["extra"] = "fixture-key-with-no-authority"
            answer["score" if answer["type"] == "noul" else "noul"] = "ignored hostile field"
        add(f"{phase}_extra_answer_fields_ignored", phase, "failures", response=response)
        # The transport owns byte enforcement. These replies deliberately bypass
        # that enforcement and pin analyse's behavior at its injected seam.
        for delta in (-1, 0, 1):
            response = reply(phase)
            response["ignored_padding"] = {"$text": {"unit": "x", "repeat": module.MAX_RESPONSE_BYTES + delta}}
            add(f"{phase}_reply_padding_limit_{delta + 1}", phase, "failures", response=response)
        for index, response in enumerate(
            (
                {},
                [],
                None,
                False,
                1,
                "upstream text",
                {"answers": None},
                {"answers": []},
                {"answers": {}},
                {"answers": True},
            )
        ):
            add(f"{phase}_response_shape_{index}", phase, "failures", request={"response": response})
        response = reply(phase)
        response["answers"]["unexpected"] = {"type": "noul", "noul": 0}
        add(f"{phase}_extra_question", phase, "failures", response=response)
        response = reply(phase)
        response.pop("model")
        add(f"{phase}_missing_model_pinned", phase, "failures", response=response)
        add(f"{phase}_missing_model_latest", phase, "failures", response=response, requested_model="jev-latest")
        for index, model in enumerate(
            (
                None,
                False,
                1,
                [],
                {},
                "jev-9.9.9",
                "jev-latest",
                "jev-0.0.0",
                "jev-999.999.999",
                "jev-1000.1.1",
                "jev-1.1000.1",
                "jev-1.1.1000",
                "jev-1.2.3\n",
                "JEV-1.2.3",
                "fixture-key-with-no-authority",
                "jev-1.2",
                "jev-01.002.003",
                "jev-1.2.3\x00",
            )
        ):
            for requested in ("jev-1.14.0", "jev-latest"):
                response = reply(phase)
                response["model"] = model
                add(
                    f"{phase}_served_model_{index:02d}_{requested}",
                    phase,
                    "failures",
                    response=response,
                    requested_model=requested,
                )
        usage_values = [
            None,
            False,
            1,
            [],
            "upstream text",
            {},
            {"flag": True, "float": 1.0, "nested": {}, "text": "fixture-key-with-no-authority"},
            {"$pairs": [[7, 1], ["valid", 2]]},
        ]
        for index, usage in enumerate(usage_values):
            response = reply(phase)
            response["usage"] = usage
            add(f"{phase}_usage_shape_{index}", phase, "failures", response=response)
        response = reply(phase)
        response["usage"] = {key: 1 for key in ("a", "a0_", "", "A", "0a", "a-b", "a\n", "a\x00", "é", "_a", "z")}
        add(f"{phase}_usage_key_grammar", phase, "failures", response=response)
        for status in (0, 204, 299, 500, None, "unusual"):
            add(
                f"{phase}_returned_status_{str(status).lower()}",
                phase,
                "failures",
                request={"status": status, "response": reply(phase)},
            )
        add(f"{phase}_consent_required", phase, "failures", consent=False)
        add(f"{phase}_key_unavailable", phase, "failures", faults={"read_key": {"type": "KeyUnavailable"}})
        for seam in ("consent_recorded", "read_key", "endpoint", "request_json", "record", "monotonic"):
            add(f"{phase}_{seam}_unexpected_error", phase, "failures", faults={seam: {"type": "OSError"}})
        add(
            f"{phase}_clock_log_unexpected_error",
            phase,
            "failures",
            faults={"monotonic": {"type": "ValueError", "at": 2}},
        )
        for index, key in enumerate((None, 1, [], "")):
            add(f"{phase}_key_shape_{index}", phase, "failures", key=key)
        errors = [
            {"type": "NetworkRefused"},
            *({"type": "HttpFailure", "kind": "http", "status": status} for status in (401, 403, 429, 400, 500, None)),
            *(
                {"type": "HttpFailure", "kind": kind, "status": status}
                for kind, status in (
                    ("busy", None),
                    ("redirect", 302),
                    ("invalid_json", 200),
                    ("too_large", 200),
                    ("timeout", None),
                    ("unreachable", None),
                    ("other", 503),
                )
            ),
            *({"type": kind} for kind in ("TimeoutError", "ValueError", "OSError", "RuntimeError")),
        ]
        for index, error in enumerate(errors):
            add(f"{phase}_request_exception_{index:02d}", phase, "failures", request={"raise": error})
        for index, raw in enumerate((None, [], [200], [200, {}, "extra"], "ok")):
            add(f"{phase}_request_return_shape_{index}", phase, "failures", request={"raw_return": raw})
        for kind in ("busy", "http", "redirect", "invalid_json", "too_large", "timeout"):
            add(
                f"{phase}_log_failure_masks_{kind}",
                phase,
                "failures",
                request={"raise": {"type": "HttpFailure", "kind": kind, "status": 429}},
                faults={"record": {"type": "OSError"}},
            )

    for index, phase in enumerate(("unknown", "", None, 0, False, [], {})):
        add(
            f"invalid_phase_{index}",
            group="failures",
            phase=phase if isinstance(phase, str) and phase in module.QUESTIONS else "pre",
        )
        cases[-1]["phase"] = phase
    add(
        "input_refusal_before_consent_and_key",
        group="failures",
        text=None,
        consent=False,
        faults={"read_key": {"type": "KeyUnavailable"}},
    )
    add("consent_refusal_before_key", group="failures", consent=False, faults={"read_key": {"type": "KeyUnavailable"}})
    for phase, questions in module.QUESTIONS.items():
        for index, value in enumerate((True, 1, -1, 0.5, ["x"], {"x": 1})):
            add(f"{phase}_truthy_nonstring_text_{index}", phase, "failures", text=value)
        for index, model in enumerate(("note jev-9.9.9", "xjev-1.2.3", "jev-1x2.3", "jev-1.2x3")):
            response = reply(phase)
            response["model"] = model
            add(
                f"{phase}_served_model_grammar_{index}",
                phase,
                "failures",
                response=response,
                requested_model="jev-latest",
            )
        for qid in questions:
            response = reply(phase)
            response["answers"]["renamed_" + qid] = response["answers"].pop(qid)
            add(f"{phase}_renamed_question_{qid}", phase, "failures", response=response)
    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", type=Path)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime", action="append")
    parser.add_argument("--capture-root", type=Path)
    parser.add_argument("--driver", type=Path)
    parser.add_argument("--case-inputs", type=Path)
    parser.add_argument("--capture-output", type=Path)
    args = parser.parse_args()
    if args.capture_root:
        module, helper = load_export(args.capture_root, args.driver)
        observations = [helper.replay(case, module) for case in json.loads(args.case_inputs.read_text())]
        args.capture_output.write_text(
            json.dumps(
                {"python": sys.version.split()[0], "observations": observations}, ensure_ascii=True, allow_nan=False
            )
        )
        return
    if args.worktree is None or args.scratch is None:
        parser.error("--worktree and --scratch are required for generation")
    args.scratch.mkdir(parents=True, exist_ok=True)
    source = git(args.worktree, "show", BASE + ":" + SOURCE)
    if git(args.worktree, "rev-parse", "HEAD").decode().strip() != BASE:
        raise RuntimeError("Worktree base changed")
    if (args.worktree / SOURCE).read_bytes() != source:
        raise RuntimeError("Candidate source differs from the pinned blob")
    with tempfile.TemporaryDirectory(prefix="source-", dir=args.scratch) as temporary:
        exported = Path(temporary)
        paths = git(args.worktree, "ls-tree", "-r", "--name-only", BASE, "src/agiw_mcp").decode().splitlines()
        for relative in paths:
            if not relative.endswith(".py"):
                continue
            target = exported / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git(args.worktree, "show", BASE + ":" + relative))
        helper_path = args.worktree / "tests/test_jev_analyse_golden.py"
        module, helper = load_export(exported, helper_path)
        cases = inputs(module)
        raw_inputs = exported / "case-inputs.json"
        raw_inputs.write_text(json.dumps(cases, ensure_ascii=True, allow_nan=False))
        for case in cases:
            case["expected"] = helper.replay(case, module)
        runtimes = [sys.version.split()[0]]
        for index, runtime in enumerate(args.runtime or ("python3.12", "python3.14")):
            captured = exported / f"capture-{index}.json"
            subprocess.run(
                [
                    runtime,
                    "-B",
                    str(Path(__file__).resolve()),
                    "--capture-root",
                    str(exported),
                    "--driver",
                    str(helper_path),
                    "--case-inputs",
                    str(raw_inputs),
                    "--capture-output",
                    str(captured),
                ],
                env=ENV,
                check=True,
                timeout=120,
            )
            observation = json.loads(captured.read_text())
            runtimes.append(observation["python"])
            if len(observation["observations"]) != len(cases):
                raise RuntimeError("Secondary capture omitted cases")
            for case, actual in zip(cases, observation["observations"], strict=True):
                if helper.serialize_observation(actual) not in [
                    helper.serialize_observation(expected)
                    for expected in [case["expected"], *case.get("expected_variants", [])]
                ]:
                    case.setdefault("expected_variants", []).append(actual)
        document = {
            "schema_version": 1,
            "generated_from": {
                "revision": BASE,
                "jev_sha256": hashlib.sha256(source).hexdigest(),
                "generator": "tests/test_support/jev_analyse_golden/generate.py",
                "capture_runtimes": runtimes,
            },
            "cases": cases,
        }
        content = (json.dumps(document, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        output = args.output or args.worktree / "tests/test_support/jev_analyse_golden/cases.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
        print(  # noqa: T201 - capture CLI reports corpus counts and hashes
            json.dumps(
                {
                    "cases": len(cases),
                    "groups": {
                        group: sum(case["group"] == group for case in cases) for group in ("boundaries", "failures")
                    },
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content),
                }
            )
        )


def load_export(exported, helper_path):
    exported = exported.resolve()
    helper_path = helper_path.resolve()
    sys.path.insert(0, str(exported / "src"))
    module = importlib.import_module("agiw_mcp.jev")
    if Path(module.__file__).resolve() != exported / SOURCE:
        raise RuntimeError("Imported an unexpected source module")
    sys.path.insert(1, str(helper_path.parent))
    helper_spec = importlib.util.spec_from_file_location("golden_driver", helper_path)
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    return module, helper


if __name__ == "__main__":
    main()
