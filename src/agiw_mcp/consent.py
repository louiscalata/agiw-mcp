# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Consent for sending text to TypeSafe, enforced by this server.

Consent is bound to a SHA-256 of the exact disclosure below. If a later
version changes what is sent, the stored consent no longer matches and the Jev
tools refuse until consent is granted again.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from .config import config_dir, display_path, write_private_json
from .errors import ToolError
from .questions import QUESTIONS

CONSENT_FILE = "consent.json"
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MAX_PRE_BYTES = 12 * 1024
MAX_POST_BYTES = 12 * 1024

DISCLOSURE = f"""\
agiw_jev_pre and agiw_jev_post are optional, advisory checks run by Jev, a model
hosted by TypeSafe. Each call sends one HTTPS POST request to
{JEV_ENDPOINT} containing exactly:

1. Your text. agiw_jev_pre sends the task text you pass in (refused above
   {MAX_PRE_BYTES} bytes). agiw_jev_post sends the result or report text you pass
   in, truncated to its first {MAX_POST_BYTES} bytes. The text is sent as you
   supply it; agiw-mcp does not remove secrets from it, so do not include any.
2. The fixed question text and answer scales listed below.
3. The Jev model name from your configuration.
4. Your TypeSafe API key, in the Authorization header.

TypeSafe also receives your IP address and standard request metadata. Nothing
else is sent: no files, file paths, model names, configuration or environment.
agiw_readiness, agiw_status, agiw_consent and agiw_nisi_run never contact
TypeSafe. A failed call is not retried automatically. TypeSafe's own terms and
any usage charges apply. Jev's answers are advisory, not a verdict.

You can revoke consent at any time with agiw_consent action "revoke" or
`agiw-mcp consent revoke`.
"""


def _question_lines() -> str:
    lines = []
    for phase in ("pre", "post"):
        lines.append(f"Questions sent by agiw_jev_{phase}:")
        for qid, question in QUESTIONS[phase].items():
            lines.append(f"- {qid} ({question['type']}): {question['instructions']}")
            criteria = question["criteria"]
            if question["type"] == "noul":
                lines.append(f"    true: {criteria['true']} / false: {criteria['false']}")
            else:
                lines.append("    levels: " + " | ".join(criteria))
    return "\n".join(lines) + "\n"


def disclosure_text() -> str:
    return DISCLOSURE + "\n" + _question_lines()


def disclosure_sha256() -> str:
    return hashlib.sha256(disclosure_text().encode("utf-8")).hexdigest()


def _path():
    return config_dir() / CONSENT_FILE


def _read() -> dict | None:
    try:
        with open(_path(), "rb") as handle:
            raw = handle.read(16385)
    except OSError:
        return None
    if len(raw) > 16384:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def consent_recorded() -> bool:
    record = _read()
    return bool(record and record.get("schema") == 1 and record.get("granted") is True
                and record.get("disclosureSha256") == disclosure_sha256())


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def show() -> dict:
    record = _read() or {}
    stale = bool(record.get("granted") is True and record.get("disclosureSha256") != disclosure_sha256())
    return {"granted": consent_recorded(), "staleConsent": stale, "endpoint": JEV_ENDPOINT,
            "disclosure": disclosure_text(), "disclosureSha256": disclosure_sha256(),
            "consentFile": display_path(_path()),
            "howToGrant": ("Only the user can grant consent: if they agree to this disclosure, they run "
                           "`agiw-mcp consent grant` in a terminal. This tool cannot grant it.")}


def grant(disclosure_hash) -> dict:
    if disclosure_hash != disclosure_sha256():
        raise ToolError("DISCLOSURE_MISMATCH",
                        "Consent was not recorded. Call agiw_consent with action \"show\", let the user read the "
                        "disclosure, and pass its current disclosure_sha256 when they agree.")
    try:
        write_private_json(_path(), {"schema": 1, "granted": True, "grantedAt": _now(),
                                     "disclosureSha256": disclosure_sha256()})
    except OSError:
        raise ToolError("CONSENT_WRITE_FAILED", "Consent could not be saved in the config directory.") from None
    return {"granted": True, "disclosureSha256": disclosure_sha256(), "consentFile": display_path(_path())}


def revoke() -> dict:
    try:
        write_private_json(_path(), {"schema": 1, "granted": False, "revokedAt": _now()})
    except OSError:
        raise ToolError("CONSENT_WRITE_FAILED", "Consent could not be updated in the config directory.") from None
    return {"granted": False, "consentFile": display_path(_path())}
