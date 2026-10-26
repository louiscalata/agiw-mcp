# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""TypeSafe API key lookup: environment, then macOS Keychain, then Secret Service.

Presence checks verify that a key exists. On macOS, the Keychain query
omits ``-w`` so the value is not returned. On Linux, ``secret-tool lookup``
returns the value to agiw-mcp, which discards it and never logs or prints it.
``read_key`` is called only by the Jev tools after consent is checked.
Every failure raises KeyUnavailable with one fixed message; no key, path
fragment or tool output is ever included. Plaintext key files are not supported.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

from .config import Config, test_mode

ENV_VAR = "TYPESAFE_API_KEY"
SECURITY_BIN = "/usr/bin/security"
SECRET_TOOL_CANDIDATES = ("/usr/bin/secret-tool", "/usr/local/bin/secret-tool", "/bin/secret-tool")
PRESENCE_TIMEOUT = 3.0
READ_TIMEOUT = 10.0
KEY_UNAVAILABLE_MESSAGE = ("No usable TypeSafe API key is available. Store one as described under "
                           "'Jev key and consent' in the README's Setup details, then try again.")
_KEY_SHAPE = re.compile(r"[\x21-\x7e]{8,512}\Z")


class KeyUnavailable(Exception):
    def __init__(self):
        super().__init__(KEY_UNAVAILABLE_MESSAGE)


def _platform() -> str:
    # The test suite never touches a real Keychain or Secret Service unless a
    # test opts in and replaces the tool paths with fakes.
    if test_mode() and os.environ.get("AGIW_MCP_TEST_OS_KEYSTORE") != "1":
        return "other"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


def _secret_tool() -> str | None:
    for candidate in SECRET_TOOL_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _run_status(argv: list[str], timeout: float) -> int | None:
    """Run a lookup with all output discarded; return its exit code only."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return done.returncode


def key_presence(config: Config) -> dict:
    """Report where a key is present without reading its value."""
    if os.environ.get(ENV_VAR):
        return {"present": True, "source": "environment"}
    platform = _platform()
    if platform == "darwin" and os.path.isfile(SECURITY_BIN):
        code = _run_status([SECURITY_BIN, "find-generic-password", "-s", config.keychain_service,
                            "-a", config.keychain_account], PRESENCE_TIMEOUT)
        if code == 0:
            return {"present": True, "source": "macos-keychain"}
    if platform == "linux":
        tool = _secret_tool()
        if tool:
            code = _run_status([tool, "lookup", "service", config.keychain_service,
                                "account", config.keychain_account], PRESENCE_TIMEOUT)
            if code == 0:
                return {"present": True, "source": "secret-service"}
    return {"present": False, "source": None}


def _clean(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise KeyUnavailable() from None
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if not _KEY_SHAPE.fullmatch(text):
        raise KeyUnavailable()
    return text


def _run_read(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=READ_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise KeyUnavailable() from None
    if done.returncode != 0 or len(done.stdout) > 4096:
        raise KeyUnavailable()
    return _clean(done.stdout)


def read_key(config: Config) -> str:
    env_value = os.environ.get(ENV_VAR)
    if env_value:
        if not _KEY_SHAPE.fullmatch(env_value):
            raise KeyUnavailable()
        return env_value
    platform = _platform()
    if platform == "darwin" and os.path.isfile(SECURITY_BIN):
        return _run_read([SECURITY_BIN, "find-generic-password", "-s", config.keychain_service,
                          "-a", config.keychain_account, "-w"])
    if platform == "linux":
        tool = _secret_tool()
        if tool:
            return _run_read([tool, "lookup", "service", config.keychain_service,
                              "account", config.keychain_account])
    raise KeyUnavailable()
