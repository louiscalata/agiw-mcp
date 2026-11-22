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
assert nisi.verify_vendor()["version"] == "0.3.2"
assert nisi.RUNNER.is_file()
initialized = rpc.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
assert initialized["result"]["serverInfo"]["version"] == agiw_mcp.__version__
listed = rpc.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
assert len(listed["result"]["tools"]) == 6
with tempfile.TemporaryDirectory() as directory:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP")
    }
    env.update(
        AGIW_MCP_CONFIG_DIR=directory,
        AGIW_MCP_TEST_MODE="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONWARNINGS="error",
    )
    command = str(Path(sys.executable).parent / "agiw-mcp")
    version = subprocess.run([command, "--version"], env=env, capture_output=True, check=True, timeout=10)
    assert version.stdout.decode().strip() == f"agiw-mcp {agiw_mcp.__version__}"
    status = subprocess.run([command, "status"], env=env, capture_output=True, check=True, timeout=15)
    assert json.loads(status.stdout)["nisi"]["verified"] is True

    # Use a fresh process and only modern requests. No initialize or tool call is needed.
    protocol = "2026-07-28"
    meta = {
        "io.modelcontextprotocol/protocolVersion": protocol,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": meta}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta}},
    ]
    payload = "".join(json.dumps(request) + "\n" for request in requests).encode()
    modern = subprocess.run([command], input=payload, env=env, capture_output=True, check=True, timeout=15)
    assert not modern.stderr.strip()
    discovered, modern_listed = [json.loads(line) for line in modern.stdout.splitlines()]
    server_info = {"name": "agiw-mcp", "version": agiw_mcp.__version__}
    for identifier, reply in enumerate((discovered, modern_listed), start=1):
        assert reply["jsonrpc"] == "2.0"
        assert reply["id"] == identifier
        assert "error" not in reply
        assert reply["result"]["resultType"] == "complete"
        assert reply["result"]["cacheScope"] == "public"
        ttl = reply["result"]["ttlMs"]
        assert type(ttl) is int and ttl > 0
        assert reply["result"]["_meta"]["io.modelcontextprotocol/serverInfo"] == server_info
    assert protocol in discovered["result"]["supportedVersions"]
    assert discovered["result"]["capabilities"] == {"tools": {}}
    assert modern_listed["result"]["tools"] == listed["result"]["tools"]
print("installed distribution smoke passed")
