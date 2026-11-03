# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
"""Run with the installed environment's python -I, outside the checkout."""
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile

import agiw_mcp
from agiw_mcp import nisi, rpc

assert Path(agiw_mcp.__file__).resolve().is_relative_to(Path(sysconfig.get_path("purelib")).resolve())
assert nisi.verify_vendor()["version"] == "0.2.0"
assert nisi.RUNNER.is_file()
initialized = rpc.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
assert initialized["result"]["serverInfo"]["version"] == agiw_mcp.__version__
listed = rpc.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
assert len(listed["result"]["tools"]) == 6
with tempfile.TemporaryDirectory() as directory:
    env = {key: value for key, value in os.environ.items()
           if key in ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP")}
    env.update(AGIW_MCP_CONFIG_DIR=directory, AGIW_MCP_TEST_MODE="1", PYTHONDONTWRITEBYTECODE="1")
    command = str(Path(sys.executable).parent / "agiw-mcp")
    version = subprocess.run([command, "--version"], env=env, capture_output=True, check=True, timeout=10)
    assert version.stdout.decode().strip() == f"agiw-mcp {agiw_mcp.__version__}"
    status = subprocess.run([command, "status"], env=env, capture_output=True, check=True, timeout=15)
    assert json.loads(status.stdout)["nisi"]["verified"] is True
print("installed distribution smoke passed")
