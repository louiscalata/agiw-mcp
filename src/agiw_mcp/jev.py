# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Advisory Jev pre-analysis (task text) and post-analysis (result text).

Order of checks: input bounds, consent, key, then exactly one HTTPS request.
There is no automatic retry. Errors carry fixed messages only.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time

from . import activity, consent, keys
from .config import Config
from .errors import ToolError
from .net import HttpFailure, NetworkRefused, request_json
from .questions import QUESTIONS, STATE_KEY, is_flagged

TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 256 * 1024
ADVISORY = "advisory, not a verdict"
TRUNCATION_MARK = "\n[truncated by agiw-mcp]"
SERVED_MODEL = re.compile(r"jev-[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\Z")
USAGE_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")


def endpoint() -> str:
    """The fixed TypeSafe endpoint named in the consent disclosure. No setting or variable changes it."""
    return consent.JEV_ENDPOINT


def _usage(value) -> dict | None:
    """Only small non-negative integer counters, so a response cannot echo text or a key back out."""
    if not isinstance(value, dict):
        return None
    kept = {key: count for key, count in value.items()
            if isinstance(key, str) and USAGE_KEY.match(key) and type(count) is int and 0 <= count < 10**9}
    return dict(sorted(kept.items())[:8]) or None


def _prepare(phase: str, text) -> tuple[str, int, bool]:
    if not isinstance(text, str) or not text.strip():
        raise ToolError("INPUT_INVALID", "Provide non-empty text.")
    if "\x00" in text:
        raise ToolError("INPUT_INVALID", "The text must not contain NUL characters.")
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        raise ToolError("INPUT_INVALID", "The text must be valid Unicode.") from None
    if phase == "pre":
        if len(raw) > consent.MAX_PRE_BYTES:
            raise ToolError("INPUT_TOO_LARGE",
                            f"The task text is {len(raw)} bytes; the limit is {consent.MAX_PRE_BYTES}. "
                            "Summarize the task and try again.")
        return text, len(raw), False
    if len(raw) <= consent.MAX_POST_BYTES:
        return text, len(raw), False
    budget = consent.MAX_POST_BYTES - len(TRUNCATION_MARK.encode("utf-8"))
    clipped = raw[:budget].decode("utf-8", errors="ignore") + TRUNCATION_MARK
    return clipped, len(raw), True


def _finite(value) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _answer_valid(question: dict, answer) -> bool:
    if not isinstance(answer, dict) or answer.get("type") != question["type"]:
        return False
    confidence = answer.get("confidence")
    if confidence is not None and (not _finite(confidence) or not 0 <= confidence <= 1):
        return False
    if question["type"] == "noul":
        value = answer.get("noul")
        return _finite(value) and 0 <= value <= 1
    value = answer.get("score")
    return _finite(value) and 0 <= value <= len(question["criteria"]) - 1


def analyse(phase: str, text, config: Config) -> dict:
    tool = f"agiw_jev_{phase}"
    sent_text, input_bytes, truncated = _prepare(phase, text)
    input_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if not consent.consent_recorded():
        raise ToolError("CONSENT_REQUIRED",
                        "Consent to send text to TypeSafe is not recorded. Call agiw_consent with action \"show\" so "
                        "the user can read the disclosure; if they agree, they run `agiw-mcp consent grant` "
                        "in a terminal.")
    try:
        key = keys.read_key(config)
    except keys.KeyUnavailable:
        raise ToolError("KEY_UNAVAILABLE", keys.KEY_UNAVAILABLE_MESSAGE) from None
    questions = QUESTIONS[phase]
    body = {"model": config.jev_model, "state": {STATE_KEY[phase]: sent_text}, "questions": questions}
    request_sha = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    sent_bytes = len(sent_text.encode("utf-8"))
    started = time.monotonic()

    def log(outcome: str, status=None, flagged=None):
        activity.record(tool=tool, inputSha256=input_sha, requestSha256=request_sha, inputBytes=input_bytes,
                        sentBytes=sent_bytes, truncated=truncated, outcome=outcome, httpStatus=status,
                        flaggedCount=flagged, elapsedMs=int((time.monotonic() - started) * 1000))

    try:
        status, response = request_json(endpoint(), method="POST", body=body,
                                        headers={"Authorization": "Bearer " + key},
                                        timeout=TIMEOUT, max_bytes=MAX_RESPONSE_BYTES)
    except NetworkRefused:
        log("ENDPOINT_REFUSED")
        raise ToolError("JEV_ENDPOINT_REFUSED", "The Jev endpoint was refused by policy; nothing was sent.") from None
    except HttpFailure as failure:
        if failure.kind == "http":
            log("HTTP_ERROR", failure.status)
            hint = {401: " The API key was rejected.", 403: " The API key is not allowed to use this model.",
                    429: " TypeSafe is rate limiting; wait before trying again."}.get(failure.status, "")
            raise ToolError("JEV_HTTP_ERROR", f"TypeSafe returned HTTP {failure.status}.{hint} Not retried.",
                            httpStatus=failure.status) from None
        if failure.kind == "redirect":
            log("REDIRECT_REFUSED", failure.status)
            raise ToolError("JEV_REDIRECT_REFUSED", "TypeSafe answered with a redirect; it was not followed.") from None
        if failure.kind in ("invalid_json", "too_large"):
            log("BAD_RESPONSE", failure.status)
            raise ToolError("JEV_BAD_RESPONSE", "TypeSafe's response was not a valid Jev answer.") from None
        log("UNREACHABLE")
        raise ToolError("JEV_UNREACHABLE", "TypeSafe could not be reached (connection error or timeout). "
                        "Not retried.") from None
    finally:
        key = None  # noqa: F841 - drop the reference as early as possible

    answers = response.get("answers") if isinstance(response, dict) else None
    if (not isinstance(answers, dict) or set(answers) != set(questions)
            or not all(_answer_valid(questions[qid], answers[qid]) for qid in questions)):
        log("BAD_RESPONSE", status)
        raise ToolError("JEV_BAD_RESPONSE", "TypeSafe's response did not answer the fixed questions.")
    served = response.get("model")
    if config.jev_model != "jev-latest" and served != config.jev_model:
        log("MODEL_MISMATCH", status)
        raise ToolError("JEV_MODEL_MISMATCH", "TypeSafe served a different Jev model than the one configured.",
                        requestedModel=config.jev_model)
    rows = []
    for qid, question in questions.items():
        answer = answers[qid]
        value = answer.get("noul") if question["type"] == "noul" else answer.get("score")
        row = {"id": qid, "type": question["type"], "question": question["instructions"],
               "criteria": question["criteria"], "answer": round(float(value), 4),
               "flagged": is_flagged(question, answer)}
        if question["type"] == "score":
            row["scaleMax"] = len(question["criteria"]) - 1
        if _finite(answer.get("confidence")):
            row["confidence"] = round(float(answer["confidence"]), 4)
        rows.append(row)
    flagged = [row["id"] for row in rows if row["flagged"]]
    log("ANSWERED", status, len(flagged))
    return {
        "advisory": ADVISORY,
        "phase": phase,
        "model": served if isinstance(served, str) and SERVED_MODEL.match(served) else None,
        "inputBytes": input_bytes,
        "sentBytes": sent_bytes,
        "truncated": truncated,
        "flagged": flagged,
        "flagRule": "noul >= 0.5, or score >= half of the scale maximum",
        "questions": rows,
        "usage": _usage(response.get("usage")),
        "note": ("These are Jev's probabilities for fixed questions. They are " + ADVISORY +
                 "; check the work itself before acting."),
    }
