# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Configuration, the per-OS config directory and private file writes.

The config file is optional and closed: unknown keys are refused. Three values
can also come from the environment so an MCP client entry can carry them:
AGIW_MCP_MODEL_BASE_URL, AGIW_MCP_AUTHOR_MODEL and AGIW_MCP_REVIEWER_MODEL.
The Jev endpoint is not configurable outside test mode.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit

from .errors import ToolError

APP_DIR_NAME = "agiw-mcp"
CONFIG_FILE = "config.json"
MAX_CONFIG_BYTES = 65536

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_SERVICE = "agiw-mcp"
DEFAULT_ACCOUNT = "typesafe"
DEFAULT_JEV_MODEL = "jev-1.14.0"
DEFAULT_MAX_OUTPUT_TOKENS = 4096

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}\Z")
_ITEM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_JEV_MODEL = re.compile(r"jev-(?:latest|[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})\Z")
_KEYS = frozenset({"model_base_url", "author_model", "reviewer_model", "keychain_service",
                   "keychain_account", "jev_model", "max_output_tokens"})
_ENV = {"model_base_url": "AGIW_MCP_MODEL_BASE_URL", "author_model": "AGIW_MCP_AUTHOR_MODEL",
        "reviewer_model": "AGIW_MCP_REVIEWER_MODEL"}


def test_mode() -> bool:
    """True only when the test suite set AGIW_MCP_TEST_MODE=1."""
    return os.environ.get("AGIW_MCP_TEST_MODE") == "1"


def config_dir() -> Path:
    override = os.environ.get("AGIW_MCP_CONFIG_DIR")
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ToolError("CONFIG_DIR_INVALID", "AGIW_MCP_CONFIG_DIR must be an absolute path.")
        return path
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Roaming") / APP_DIR_NAME
    base = os.environ.get("XDG_CONFIG_HOME")
    if base and Path(base).is_absolute():
        return Path(base) / APP_DIR_NAME
    return Path.home() / ".config" / APP_DIR_NAME


def display_path(path) -> str:
    """Show a path with the home directory replaced by ~."""
    text = str(path)
    try:
        home = str(Path.home())
    except (RuntimeError, KeyError):
        return text
    if home and home not in ("/", "\\") and (text == home or text.startswith(home + os.sep)):
        return "~" + text[len(home):]
    return text


def validate_loopback_base_url(value) -> str:
    """Accept only http://127.0.0.1[:port][/path] or http://[::1][:port][/path]."""
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ValueError("model_base_url must be a string")
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("model_base_url has an invalid port") from exc
    if (parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "::1")
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or "\\" in value or any(ord(c) < 33 for c in value)):
        raise ValueError("model_base_url must be http://127.0.0.1 or http://[::1] without credentials or query")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("model_base_url has an invalid port")
    return value.rstrip("/")


@dataclasses.dataclass(frozen=True)
class Config:
    model_base_url: str = DEFAULT_BASE_URL
    author_model: str | None = None
    reviewer_model: str | None = None
    keychain_service: str = DEFAULT_SERVICE
    keychain_account: str = DEFAULT_ACCOUNT
    jev_model: str = DEFAULT_JEV_MODEL
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    source: str = "defaults"

    @property
    def chat_endpoint(self) -> str:
        return self.model_base_url + "/chat/completions"

    @property
    def models_endpoint(self) -> str:
        return self.model_base_url + "/models"

    def public(self) -> dict:
        return {"model_base_url": self.model_base_url, "author_model": self.author_model,
                "reviewer_model": self.reviewer_model, "keychain_service": self.keychain_service,
                "keychain_account": self.keychain_account, "jev_model": self.jev_model,
                "max_output_tokens": self.max_output_tokens, "source": self.source}


class ConfigError(ToolError):
    pass


def _validate(values: dict) -> dict:
    out = {}
    for key, value in values.items():
        if key == "model_base_url":
            out[key] = validate_loopback_base_url(value)
        elif key in ("author_model", "reviewer_model"):
            if value is not None and (not isinstance(value, str) or not _MODEL_ID.fullmatch(value)):
                raise ValueError(f"{key} must be a model ID such as qwen3-coder-30b")
            out[key] = value
        elif key in ("keychain_service", "keychain_account"):
            if not isinstance(value, str) or not _ITEM_NAME.fullmatch(value):
                raise ValueError(f"{key} must use letters, digits, dot, underscore or hyphen")
            out[key] = value
        elif key == "jev_model":
            if not isinstance(value, str) or not _JEV_MODEL.fullmatch(value):
                raise ValueError("jev_model must look like jev-1.14.0 or jev-latest")
            out[key] = value
        elif key == "max_output_tokens":
            if type(value) is not int or not 256 <= value <= 32768:
                raise ValueError("max_output_tokens must be an integer from 256 to 32768")
            out[key] = value
    return out


def _read_file(path: Path) -> dict:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError("CONFIG_UNREADABLE", "The config file could not be read.") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError("CONFIG_INVALID", "The config file is larger than 64 KB.")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, ValueError) as exc:
        raise ConfigError("CONFIG_INVALID", "The config file is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise ConfigError("CONFIG_INVALID", "The config file must contain a JSON object.")
    unknown = sorted(set(value) - _KEYS)
    if unknown:
        raise ConfigError("CONFIG_INVALID", "The config file has unknown keys: " + ", ".join(unknown[:5]) + ".")
    return value


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def load_config() -> Config:
    path = config_dir() / CONFIG_FILE
    values = _read_file(path)
    source = "file" if values else "defaults"
    for key, env_name in _ENV.items():
        env_value = os.environ.get(env_name)
        if env_value:
            values[key] = env_value
            source = "file+env" if source == "file" else "env"
    try:
        checked = _validate(values)
    except ValueError as exc:
        raise ConfigError("CONFIG_INVALID", "Configuration value refused: " + str(exc) + ".") from None
    return Config(source=source, **checked)


def config_file_path() -> Path:
    return config_dir() / CONFIG_FILE


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def write_private_json(path: Path, value: dict) -> None:
    """Write JSON atomically with mode 0600 where the platform supports it."""
    ensure_private_dir(path.parent)
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
