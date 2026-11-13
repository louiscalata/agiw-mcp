# AGIW MCP

[![CI](https://github.com/louiscalata/agiw-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/louiscalata/agiw-mcp/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-informational)

AGIW MCP is a local [Model Context Protocol](https://modelcontextprotocol.io) server: a readiness check for this machine, optional consent-gated questions to TypeSafe's Jev model, and one Nisi run on file contents you supply.

**Status:** the `v0.1.0` tag and current main both report version `0.1.0`. Python 3.10 or newer. Node.js 22 or newer when a Nisi run needs it. CI runs on macOS, Linux, and Windows. Apache-2.0. Not published to PyPI.

## Quick start

```bash
git clone https://github.com/louiscalata/agiw-mcp.git
cd agiw-mcp
pipx install .
agiw-mcp --version
```

`agiw-mcp --version` prints `agiw-mcp 0.1.0`.

The same checkout can be tried without installing it, or installed with uv:

```bash
uvx --from . agiw-mcp --version
uv tool install .
uv tool install "git+https://github.com/louiscalata/agiw-mcp"
uv tool install "git+https://github.com/louiscalata/agiw-mcp@main"
uv tool install "git+https://github.com/louiscalata/agiw-mcp@v0.1.0"
```

`uvx --from . agiw-mcp --version` prints `agiw-mcp 0.1.0`. Each `uv tool install` installs that same version, and `agiw-mcp --version` prints `agiw-mcp 0.1.0` afterwards. `@main` and the URL with no ref follow the default branch. `@v0.1.0` is the tag (`f69630a`) and does not include later commits. The version string is the same either way; the revision uv prints is the one that identifies the code.

With no model ids configured, readiness exits 1 and does not start a server:

```bash
agiw-mcp readiness
```

The JSON has `"status": "NOT_READY"` and `"missing": {"id": "models", ...}`. With Node 22 on `PATH` and no model ids, that is the first failed check. `READY` also needs two different model IDs. By default, both must appear in `GET <model_base_url>/models`. If `reviewer_base_url` is set, the author must appear in the author server's list and the reviewer in the reviewer server's list. See [Configuration file and environment variables](#configuration-file-and-environment-variables).

With no arguments, `agiw-mcp` speaks MCP on stdin and stdout. An initialize request for protocol `2025-03-26`, then `tools/list`, returned server `agiw-mcp` `0.1.0` and these tools: `agiw_readiness`, `agiw_status`, `agiw_consent`, `agiw_jev_pre`, `agiw_jev_post`, `agiw_nisi_run`.

The legacy `initialize` handshake accepts `2025-11-25`, `2025-06-18`, `2025-03-26`, and `2024-11-05`.

Config files for Claude Desktop, Cursor, Claude Code, Factory and OpenCode are in [Client setup](#client-setup).

## How it works

Say "initiate online code mode" with no task. The assistant should call `agiw_readiness` and report `READY`, or the first missing local prerequisite and its fix. That call starts nothing.

1. **Readiness** checks, in order: Python 3.10 or newer, configuration, Node.js 22 or newer on `PATH`, the vendored Nisi v0.2.0 hashes, then the configured local OpenAI-compatible model servers. Configure two different model IDs. By default, both must appear in `GET <model_base_url>/models`. If `reviewer_base_url` is set, the author must appear in the author server's list and the reviewer in the reviewer server's list. Nisi runs currently require an IPv4 loopback URL (`http://127.0.0.1:<port>/...`). Configuration and readiness accept `http://[::1]:<port>/...`, but the current Nisi HTTP transport rejects it. The check stops at the first failure. Hostnames, HTTPS and other addresses are refused.
2. **Nisi** (`agiw_nisi_run`) runs one Nisi v0.2.0 workflow on the file contents in the request. A local author model drafts, static checks and your assertions run, a different local reviewer model reviews, and a short repair loop follows (`repair_budget` 0 to 3, default 1). You get proposed file contents and a report. The server never reads or writes your project and never runs the generated code. `COMPLETED` means those checks passed. It is not a verdict that the proposal is right.
3. **Jev** is optional and is not required for a Nisi run. `agiw_jev_pre` sends a task (at most 12 KiB) and `agiw_jev_post` sends a report (truncated to 12 KiB) to `https://api.typesafe.ai/v1/systemone`. The default model is `jev-1.14.0`. Both refuse until consent is recorded and a TypeSafe API key is available. The MCP tool can show or revoke consent. It cannot grant it. `agiw-mcp consent grant` asks in an interactive terminal. The answers are advisory.

After the local checks pass, readiness reports Jev on its own as `jev.status`: `READY` or `NOT_READY`. The key lookup waits until consent is recorded. On Linux that lookup returns the key and the server discards it.

One server process handles one tool call at a time. During a Nisi run, ping and cancellation still get a response.

## What works, and what does not

**Works**

- The six tools above, on Python 3.10+. CI runs the tests on macOS and Linux with Python 3.10, 3.12 and 3.13, and Node 22 and 24. Windows CI runs Python 3.12 and Node 22, and skips the ticket-admission tests.
- Vendored Nisi v0.2.0, checked against its manifest before a run. The same bytes are in [AGIW Suite](https://github.com/louiscalata/agiw-suite) main. Suite does not launch this server. Both use Jev model `jev-1.14.0` and `https://api.typesafe.ai/v1/systemone`. Suite's command-line package allows Python 3.9; this server does not.
- No telemetry. The Jev activity log stores hashes, sizes and outcome codes, not your text.

**Does not, yet**

- A release newer than `v0.1.0`. Main has later fixes and still reports `0.1.0`.
- PyPI. Install from git or from a checkout.
- Ticket admission on Windows. CI runs the other unit tests there. Admission is still refused (`TICKET_UNSUPPORTED_PLATFORM`) before the ticket directory is touched. Runs without a ticket still work there unless `require_ticket` is true.
- Readiness does not download models, start LM Studio, or open an MCP client. Claude Desktop, Cursor, Claude Code, Factory and OpenCode are not launched by these tests.
- OpenCode protocol `2026-07-28` is not implemented. OpenCode's default `legacy` handshake, through protocol `2025-11-25`, is the one this server answers.
- A live request to TypeSafe is not part of the tests. `agiw_jev_pre` and `agiw_jev_post` refuse until consent is recorded and a key is available. CI uses a local fake for the success path.
- Granting consent is not an OS user-presence check. Any same-user process that can write the config directory can change `consent.json`. This limit also applies to `v0.1.0`, whose README overstates the consent guarantee.
- `COMPLETED` does not apply files. You do that yourself, after reading the proposal.

## Tools

| Tool | What it does |
|---|---|
| `agiw_readiness` | The check above. Returns `READY` or the first missing prerequisite and its fix. |
| `agiw_status` | Versions, configuration and which prerequisites pass. No secrets. |
| `agiw_consent` | Show what Jev would receive, or revoke consent. Cannot grant it. |
| `agiw_jev_pre` | Advisory questions about a task before you start. |
| `agiw_jev_post` | Advisory questions about a result or report. |
| `agiw_nisi_run` | One local Nisi run on the file contents you pass in. |

## Setup details

Readiness and key errors refer to the headings in this section. The blocks below are the file formats and the program's own commands.

### Requirements and the local model server

- Python 3.10 or newer.
- Node.js 22 or newer on the `PATH` of whatever starts `agiw-mcp`, when you use Nisi.
- A local chat server that supports `response_format: {"type": "json_schema"}`, listening on loopback HTTP only.
- Two different model IDs. By default, both must appear in `GET <model_base_url>/models`. If `reviewer_base_url` is set, the author must appear in the author server's list and the reviewer in the reviewer server's list. Nisi refuses a response whose `model` field differs.

When configured, `reasoning_effort` (`none`, `low`, `medium` or `high`) is sent on Nisi chat requests. It is omitted by default. If a model returns an answer only in `reasoning_content` with empty or missing `content`, agiw-mcp refuses it with `MODEL_ANSWER_IN_REASONING`; try setting `reasoning_effort` to `"none"` or `AGIW_MCP_REASONING_EFFORT=none`, then retry.

### Configuration file and environment variables

| OS | Config directory |
|---|---|
| macOS | `~/Library/Application Support/agiw-mcp` |
| Linux | `$XDG_CONFIG_HOME/agiw-mcp`, or `~/.config/agiw-mcp` |
| Windows | `%APPDATA%\agiw-mcp` |

`AGIW_MCP_CONFIG_DIR` overrides that directory and must be an absolute path.

`config.json` is optional. Unknown keys are refused. Omit the file or use an empty JSON object (`{}`) for defaults. A zero-byte or whitespace-only file is invalid.

```json
{
  "model_base_url": "http://127.0.0.1:1234/v1",
  "author_model": "your-author-model-id",
  "reviewer_model": "your-reviewer-model-id"
}
```

Other keys: `reviewer_base_url` (optional, same loopback rule; otherwise the reviewer uses `model_base_url`), `keychain_service` (default `agiw-mcp`), `keychain_account` (default `typesafe`), `jev_model` (default `jev-1.14.0`), `max_output_tokens` (default 4096), `reasoning_effort` (unset) and `require_ticket` (default false). `require_ticket: true` makes `agiw_nisi_run` refuse a request that has no claim ticket.

Environment variables override the file: `AGIW_MCP_BASE_URL` (or `AGIW_MCP_MODEL_BASE_URL` when `AGIW_MCP_BASE_URL` is unset or empty), `AGIW_MCP_AUTHOR_MODEL`, `AGIW_MCP_REVIEWER_MODEL`, `AGIW_MCP_REVIEWER_BASE_URL`, `AGIW_MCP_REASONING_EFFORT`. The default model URL is `http://127.0.0.1:1234/v1`.

### Jev key and consent

The key is never stored in a plain file. Lookup order:

1. `TYPESAFE_API_KEY` in the environment of the process that starts the server.
2. macOS Keychain. The server looks up service `agiw-mcp` and account `typesafe` (or the configured names). Type the key at the prompt:

   ```bash
   security add-generic-password -s agiw-mcp -a typesafe -w
   ```

3. Linux Secret Service. Type the key at the prompt:

   ```bash
   secret-tool store --label='agiw-mcp TypeSafe' service agiw-mcp account typesafe
   ```

   On Windows, use `TYPESAFE_API_KEY`.

```bash
agiw-mcp consent show
agiw-mcp consent grant
agiw-mcp consent revoke
```

`show` prints the disclosure and whether consent is recorded. `grant` requires a terminal. The confirmation is the two words "I agree": outer whitespace and one layer of straight quotes are stripped, then trailing periods, then it is compared case-insensitively. Extra words are refused. A non-interactive stdin is refused and records nothing. Consent is tied to a hash of the disclosure, so a changed disclosure asks again. Details: [SECURITY.md](SECURITY.md).

### Client setup

Point the client at the `agiw-mcp` executable, with no arguments. If the client cannot find it, use the absolute path from `command -v agiw-mcp`. A desktop app often does not inherit a shell `PATH`, so that absolute path is the reliable `command` there. `node` has to be on the same `PATH` the client gives the server.

Model ids can live in `config.json` or in the client's environment. Do not put a TypeSafe API key in a config file that you commit. For Jev, set `TYPESAFE_API_KEY` in that client's environment, or use the keychain commands above.

**Claude Code**

```bash
claude mcp add agiw -- agiw-mcp
```

With the model settings in the environment (`-e` and `--env` are the same flag; put them after the name and before `--`):

```bash
claude mcp add agiw -e AGIW_MCP_AUTHOR_MODEL=your-author-model-id \
  -e AGIW_MCP_REVIEWER_MODEL=your-reviewer-model-id -- agiw-mcp
```

Remove it with `claude mcp remove agiw`.

**Claude Desktop**

Settings, Developer, Edit Config opens `claude_desktop_config.json`. The current [MCP local-server guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers) lists these paths:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

The 2025-11-25 snapshot of that guide lists macOS and Windows only. Edit Config still opens the file.

Quit Claude Desktop completely and start it again after saving. Closing the window leaves the process running.

```json
{
  "mcpServers": {
    "agiw": {
      "command": "agiw-mcp",
      "args": [],
      "env": {
        "AGIW_MCP_AUTHOR_MODEL": "your-author-model-id",
        "AGIW_MCP_REVIEWER_MODEL": "your-reviewer-model-id"
      }
    }
  }
}
```

**Cursor**

Project config is `.cursor/mcp.json`. Config for every project on the machine is `~/.cursor/mcp.json`. When both name `agiw`, Cursor uses the project entry. The stdio fields in Cursor's MCP docs include `type`; this snippet sets it to `stdio`. Save the file and restart Cursor.

```json
{
  "mcpServers": {
    "agiw": {
      "type": "stdio",
      "command": "agiw-mcp",
      "args": [],
      "env": {
        "AGIW_MCP_AUTHOR_MODEL": "your-author-model-id",
        "AGIW_MCP_REVIEWER_MODEL": "your-reviewer-model-id"
      }
    }
  }
}
```

**Factory**

`droid mcp add` writes `~/.factory/mcp.json`. `--type` defaults to `stdio`. A project copy is `.factory/mcp.json`.

```bash
droid mcp add agiw agiw-mcp
```

```json
{
  "mcpServers": {
    "agiw": { "type": "stdio", "command": "agiw-mcp", "args": [] }
  }
}
```

**OpenCode.** Current config puts each server under `mcp.servers` in `opencode.json` or `opencode.jsonc`. `type` is `local` and `command` is an array. `codemode` defaults to true; `false` leaves these six tools on the native tool list. `protocol` defaults to `legacy`, which uses `initialize` through `2025-11-25`.

```bash
opencode mcp add agiw -- agiw-mcp
```

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "agiw": {
        "type": "local",
        "command": ["agiw-mcp"],
        "codemode": false
      }
    }
  }
}
```

Model ids from the process environment use `environment` and `{env:NAME}`:

```json
"environment": {
  "AGIW_MCP_AUTHOR_MODEL": "{env:AGIW_MCP_AUTHOR_MODEL}",
  "AGIW_MCP_REVIEWER_MODEL": "{env:AGIW_MCP_REVIEWER_MODEL}"
}
```

This repository's tests do not start Claude Desktop, Cursor, Claude Code, Factory or OpenCode.

### Nisi arguments and limits

`agiw_nisi_run` takes `task` (required), `files` (`{path, content}`), `new_files`, `acceptance_criteria`, `assertions` (`{path, contains}` or `{path, not_contains}`), `repair_budget` (0 to 3, default 1), `timeout_seconds` (30 to 1800, default 600) and an optional `ticket`.

Paths are relative, at most 256 characters, with parts of at most 128. A part starts with a letter, digit or `_@+-`, optionally after one dot, and may then contain letters, digits or `._@+-`. Empty, `.` and `..` parts, trailing-dot parts, and Windows device names are refused.

`files` and `new_files` each allow 64 entries. Supplied contents are limited to 256 KiB per file and 512 KiB in total, as UTF-8. Proposed contents have a separate 2 MiB total. Static checks compile Python with `compile` (no execution), parse JavaScript with `node --check`, and parse JSON. Other extensions report `NOT_SUPPORTED` and do not fail that stage. Python syntax uses the interpreter running agiw-mcp.

A `ticket` is `{ticketId, claimState, writeScope, owner}`. Only `Granted` or `Active` is admitted. Every path must lie inside `writeScope`. Overlapping scopes with an already admitted ticket are refused before Node starts. Scopes compare case-insensitively and under Unicode normalization. The ticket is released when the run ends. On Windows every ticket is refused and the ticket directory is not touched. Admission state lives in `tickets/` under the config directory, or in `AGIW_MCP_TICKET_DIR` (an absolute path). Lock recovery after a crash is described in [SECURITY.md](SECURITY.md).

During a Nisi run, ping and cancellation are handled while up to 16 other messages wait. Cancelling the active request or closing stdin stops the runner and suppresses its reply. SIGTERM and SIGHUP also clean up.

`taskFingerprint` is Nisi's task-record hash and excludes the supplied text. `inputFingerprint` binds the host request's task, file contents, assertions, model and check options, and the Python version. `requestSha256` hashes the body passed to fetch. A null transport hash means fetch was not called.

### Uninstall

```bash
pipx uninstall agiw-mcp
uv tool uninstall agiw-mcp
```

Then delete the config directory if you want the local state gone, and delete the Keychain or Secret Service item if you created one.

```bash
security delete-generic-password -s agiw-mcp -a typesafe
secret-tool clear service agiw-mcp account typesafe
```

Remove the server from the MCP client yourself. The client name in the examples above is `agiw`.

## Development

From a checkout, with Python 3.10+ and, for the Nisi tests that need it, Node 22+:

```bash
python3 scripts/scrub_check.py
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
```

A passing test run ends with `OK`. CI also runs `python -m compileall -q src` and `ruff check src tests scripts`.

The tests do not use the network. Model and Jev calls are fakes on 127.0.0.1, test mode refuses other URLs, and a socket guard blocks anything else. Without Node 22+, a fake runner covers the Python side. Binding those fakes needs permission to bind a local port.

After `agiw-mcp` is on `PATH`:

```bash
python3 scripts/stdio_handshake.py agiw-mcp
```

## Security and license

What is sent, consent, the Nisi sandbox and how to report a vulnerability: [SECURITY.md](SECURITY.md).

Apache-2.0. [LICENSE](LICENSE), [NOTICE](NOTICE), [PROVENANCE.md](PROVENANCE.md). The vendored Nisi v0.2.0 package keeps its own LICENSE and manifest in `src/agiw_mcp/vendor/nisi/`.

Issues and focused pull requests: [CONTRIBUTING.md](CONTRIBUTING.md). Maintained by Louis Calata, [fortheloveof.studio](https://fortheloveof.studio).
