# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Readiness for "initiate online code mode": check only, never start work.

Checks run in a fixed order and stop at the first missing prerequisite, which
is returned with an exact fix. Every probe is bounded (3 s or less) and every
exception is contained; nothing here reads the API key's value or contacts
TypeSafe.
"""
from __future__ import annotations

import sys

from . import consent, keys, nisi
from .config import Config, ConfigError, config_file_path, display_path, load_config
from .net import HttpFailure, NetworkRefused, request_json

PROBE_TIMEOUT = 3.0
MIN_PYTHON = (3, 10)


def _check(check_id: str, ok: bool, detail: str, fix: str | None = None) -> dict:
    row = {"id": check_id, "ok": ok, "detail": detail}
    if not ok:
        row["fix"] = fix
    return row


def check_python() -> dict:
    version = ".".join(str(part) for part in sys.version_info[:3])
    return _check("python", sys.version_info[:2] >= MIN_PYTHON, f"Python {version}",
                  "Install Python 3.10 or newer and reinstall agiw-mcp with it.")


def check_config() -> tuple[dict, Config | None]:
    try:
        config = load_config()
    except ConfigError as exc:
        return _check("config", False, exc.message,
                      f"Fix or remove {display_path(config_file_path())}; see "
                      "'Configuration file and environment variables' in the README's Setup details."), None
    return _check("config", True, f"Configuration from {config.source}"), config


def check_node() -> dict:
    path, version = nisi.find_node()
    if not path:
        return _check("node", False, "node was not found on PATH",
                      "Install Node.js 22 or newer (https://nodejs.org) and make sure `node` is on the PATH "
                      "of the MCP client that starts agiw-mcp.")
    if not version:
        return _check("node", False, f"{display_path(path)} did not report a version within 3 s",
                      "Check that `node --version` works in a terminal, then restart the MCP client.")
    text = ".".join(str(part) for part in version)
    if version < nisi.MIN_NODE:
        return _check("node", False, f"Node {text} at {display_path(path)} is older than 22",
                      "Upgrade Node.js to 22 or newer.")
    return _check("node", True, f"Node {text} at {display_path(path)}")


def check_nisi() -> dict:
    try:
        info = nisi.verify_vendor()
    except nisi.NisiError as exc:
        return _check("nisi", False, f"Vendored Nisi failed verification ({exc.code})",
                      "Reinstall agiw-mcp so the vendored Nisi v0.2.0 files match their manifest.")
    return _check("nisi", True, f"Nisi {info['version']}: {info['files']} files match the pinned manifest")


def check_models(config: Config) -> dict:
    author, reviewer = config.author_model, config.reviewer_model
    if not author or not reviewer:
        return _check("models", False, "author_model and reviewer_model are not both configured",
                      f"Set two different model IDs, for example in {display_path(config_file_path())}: "
                      '{"author_model": "<author model id>", "reviewer_model": "<reviewer model id>"}, '
                      "or with AGIW_MCP_AUTHOR_MODEL and AGIW_MCP_REVIEWER_MODEL.")
    if author == reviewer:
        return _check("models", False, "author_model and reviewer_model are the same model",
                      "Choose a different reviewer_model so the review is independent of the author.")
    try:
        _, body = request_json(config.models_endpoint, timeout=PROBE_TIMEOUT, max_bytes=1024 * 1024)
    except NetworkRefused:
        return _check("models", False, "The model endpoint URL was refused by policy",
                      "Use a loopback model_base_url such as http://127.0.0.1:1234/v1.")
    except HttpFailure as failure:
        detail = {"http": f"{config.models_endpoint} returned HTTP {failure.status}",
                  "invalid_json": f"{config.models_endpoint} did not return JSON",
                  "too_large": f"{config.models_endpoint} returned more than 1 MB",
                  "redirect": f"{config.models_endpoint} redirected"}.get(
            failure.kind, f"{config.models_endpoint} was not reachable within {PROBE_TIMEOUT:g} s")
        return _check("models", False, detail,
                      "Start your local OpenAI-compatible server (for example LM Studio's server on port 1234) "
                      "or set model_base_url to the address it listens on.")
    data = body.get("data") if isinstance(body, dict) else None
    listed = {item.get("id") for item in data if isinstance(item, dict)} if isinstance(data, list) else set()
    if author not in listed and reviewer not in listed:
        return _check("models", False,
                      f"{config.models_endpoint} lists neither author_model {author!r} nor reviewer_model "
                      f"{reviewer!r}; a different server may be on that port.",
                      "Set model_base_url in config.json or AGIW_MCP_BASE_URL in the MCP client's environment "
                      "to your model server's base URL (for example http://127.0.0.1:1235/v1), "
                      "then load the configured models there or choose model IDs it lists.")
    for role, model in (("author_model", author), ("reviewer_model", reviewer)):
        if model not in listed:
            return _check("models", False, f"{role} {model!r} is not listed by {config.models_endpoint}",
                          f"Download or load {model!r} in your model server, or change {role} to a listed model ID.")
    return _check("models", True, f"{config.models_endpoint} lists author {author!r} and reviewer {reviewer!r} "
                  "(JSON-schema output is not tested here)")


def check_key(config: Config) -> dict:
    try:
        presence = keys.key_presence(config)
    except Exception:  # noqa: BLE001 - a probe must never raise
        presence = {"present": False, "source": None}
    if presence["present"]:
        return _check("jev_key", True, f"TypeSafe API key present ({presence['source']}); value not read")
    if sys.platform == "darwin":
        fix = (f"Run `security add-generic-password -s {config.keychain_service} -a {config.keychain_account} -w` "
               "and type the key at the prompt, or set TYPESAFE_API_KEY in the MCP client's environment.")
    elif sys.platform.startswith("linux"):
        fix = (f"Run `secret-tool store --label='agiw-mcp TypeSafe' service {config.keychain_service} "
               f"account {config.keychain_account}` and type the key at the prompt, or set TYPESAFE_API_KEY.")
    else:
        fix = "Set TYPESAFE_API_KEY in the environment of the MCP client that starts agiw-mcp."
    return _check("jev_key", False, "No TypeSafe API key found", fix)


def check_consent() -> dict:
    if consent.consent_recorded():
        return _check("consent", True, "Consent to send task and result text to TypeSafe is recorded")
    return _check("consent", False, "Consent to send task and result text to TypeSafe is not recorded",
                  "Call agiw_consent with action \"show\" and let the user read it; if they agree, they run "
                  "`agiw-mcp consent grant` in a terminal.")


def evaluate(stop_at_first: bool = True) -> dict:
    checks: list[dict] = []
    holder: dict = {}

    def config_step() -> dict:
        row, holder["config"] = check_config()
        return row

    def models_step() -> dict:
        if holder.get("config") is None:
            return _check("models", False, "Not checked because the configuration is invalid",
                          "Fix the configuration first.")
        return check_models(holder["config"])

    def key_step() -> dict:
        # Looking for the key comes after consent: on Linux the Secret Service lookup returns the secret.
        if not consent.consent_recorded():
            return _check("jev_key", False, "Not checked until consent is recorded",
                          "Grant consent first (`agiw-mcp consent grant`), then run agiw_readiness again.")
        return check_key(holder.get("config") or Config())

    steps = [("python", check_python), ("config", config_step), ("node", check_node), ("nisi", check_nisi),
             ("models", models_step), ("consent", check_consent), ("jev_key", key_step)]
    for name, step in steps:
        try:
            row = step()
        except Exception:  # noqa: BLE001 - readiness never raises
            row = _check(name, False, "The check failed unexpectedly", "Run agiw_status for details.")
        checks.append(row)
        if not row["ok"] and stop_at_first:
            break
    failed = [row for row in checks if not row["ok"]]
    ready = not failed and len(checks) == len(steps)
    result = {"status": "READY" if ready else "NOT_READY", "checks": checks}
    if failed:
        first = failed[0]
        result["missing"] = {"id": first["id"], "detail": first["detail"], "fix": first["fix"]}
    result["next"] = ("Ready. Give a task: run agiw_jev_pre on it, then agiw_nisi_run with the relevant file "
                      "contents, then agiw_jev_post on the result." if ready else
                      "Fix the missing prerequisite, then run agiw_readiness again. No work was started.")
    return result
