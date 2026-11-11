# AGENTS.md

Guidance for coding agents. Codex, Cursor, Copilot, and OpenCode read this
file. Start with [README.md](README.md), [CONTRIBUTING.md](CONTRIBUTING.md),
and [SECURITY.md](SECURITY.md). History and attribution:
[CHANGELOG.md](CHANGELOG.md), [PROVENANCE.md](PROVENANCE.md),
[docs/release-notes/next.md](docs/release-notes/next.md).

## Overview

AGIW MCP is a local stdio MCP server for one machine. It reports whether
online code mode can run here, can send optional advisory questions to
TypeSafe's Jev model, and can run one Nisi v0.2.0 workflow on file contents
the caller supplies. The six tools are `agiw_readiness`, `agiw_status`,
`agiw_consent`, `agiw_jev_pre`, `agiw_jev_post`, and `agiw_nisi_run`. Python
3.10 or newer, standard library only. Node.js 22 or newer is required for a
real Nisi run. Apache-2.0. Maintained by Louis Calata. The published tag and
current main both report version `0.1.0`.

## Ethos

Keep the server local and honest. It does not read or write the caller's
project, does not execute generated code, and does not send telemetry.
`COMPLETED` means that run's checks passed. It is not a verdict that the
proposal is right, and applying the files is a separate human decision. Jev
answers are advisory. On "initiate online code mode" with no task, call
`agiw_readiness` and report `READY` or the first missing prerequisite and its
fix. Start nothing else. Consent to contact TypeSafe is the person's own act.
Fail closed on vendor hashes, tickets, keys, and model URLs.

## Repository map

- `src/agiw_mcp/` — the server. `__main__.py` is the CLI and, with no
  arguments, the stdio entry. `rpc.py` speaks JSON-RPC. `tools.py` registers
  the six tools. `readiness.py` checks prerequisites in order. `consent.py`
  holds the disclosure. `jev.py` and `keys.py` call TypeSafe. `nisi.py`
  verifies the bundle and runs one workflow. `nisi_runner.mjs` is this
  server's host runner. `tickets.py` admits claim tickets. `config.py`,
  `net.py`, `questions.py`, `activity.py`, and `errors.py` support them.
- `src/agiw_mcp/vendor/nisi/` — pinned public Nisi v0.2.0. The host runner is outside that bundle.
- `tests/` — `unittest` modules. They do not use the network.
- `scripts/scrub_check.py`, `scripts/stdio_handshake.py`,
  `scripts/installed_smoke.py`, `scripts/suite_compat.py`.
- `.github/workflows/ci.yml` — CI. `pyproject.toml` — metadata and Ruff.

## Commands

From a checkout, Python 3.10 or newer. Install the linter and the builder
first with `python3 -m pip install ruff build`. Scrub prints
`scrub check passed`. `compileall -q` is silent on success. Tests end with
`OK`. `python3 -m build` writes `dist/`.

```bash
python3 scripts/scrub_check.py
python3 -m compileall -q src
ruff check src tests scripts
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
python3 -m build
```

## CI

`.github/workflows/ci.yml` runs on push and on pull request. Permissions are
`contents: read`. Same-repo Linux jobs use the box (`box-linux`). macOS jobs
use GitHub-hosted `macos-latest`. The Mac runner stays off. Same-repo Windows
jobs use the PC (`pc-win`). Fork pull requests use GitHub-hosted runners.

- `test`: Python 3.10, 3.12, and 3.13 with Node 22, plus Python 3.13 with
  Node 24, on Linux and macOS. Scrub, `compileall -q src`, and unit tests.
  `fail-fast` is false.
- `python-only`: Python 3.12 with Node hidden, so the fake runner runs.
- `distribution`: Ruff, then `python -m build`, then install the wheel and
  the sdist outside the checkout.
- `uv-git-install`: install this commit, `--version`, the stdio handshake, and MCP Inspector `tools/list`.
- `windows`: Python 3.12 and Node 22. Scrub, compile, and unit tests.
  Ticket-admission tests are skipped. Admission itself stays refused.
- `suite-compat`: clones AGIW Suite `main` and checks the Nisi v0.2.0 bundle, the LICENSE bytes, and the Jev pin.

## Conventions

- One small pull request per concern. Branch from `main`. Never push to
  `main`. Merge only when CI is green.
- Docs stay plain and factual. No hype.
- Do not commit secrets. Match the existing Python modules:
  `from __future__ import annotations`, a copyright line, and
  `SPDX-License-Identifier: Apache-2.0`.
- Keep runtime dependencies empty. Tests stay on loopback fakes.
- Ruff selects `E`, `F`, `W`, and `B`, line length 120, target `py310`.
  `src/agiw_mcp/questions.py` ignores `E501` because that text is part of
  the consent disclosure.
- Use US spellings in documentation.
- A `ToolError` is an MCP result with `isError`, not a JSON-RPC protocol
  error. Unknown `config.json` keys are refused. A model URL must be
  loopback HTTP: `http://127.0.0.1...` or `http://[::1]...`.
- Leave `pyproject.toml` and `src/agiw_mcp/__init__.py` at version `0.1.0`
  until a release is assigned. That string does not name later commits.

## Hard rules

- Do not create a git tag, a GitHub release, or a PyPI publication unless
  Louis has said yes for that release. Do not bump the version past `0.1.0`.
- Never edit `AGIW.md`.
- Do not record raw device identifiers in source, tests, docs, issues, or
  log lines. That includes a machine id, a host UUID, a hostname, a MAC
  address, and a serial number. Ticket locks store an HMAC-SHA256 of the
  machine identity and of the process start identity, under `identity.key`
  (mode 0600) in the ticket directory. The raw values are not written.
  Recovery warnings and ticket refusals leave them out. A refusal names the
  ticket id only. `scripts/scrub_check.py` fails the tree on private home
  paths, RFC 1918 addresses, `.local` names, and known private host tokens.
- If a check was not run, write NOT_RUN. Do not report a pass you did not see.

## MCP

`SUPPORTED_PROTOCOLS` in `src/agiw_mcp/rpc.py` is `2025-11-25`, `2025-06-18`,
`2025-03-26`, and `2024-11-05`, newest first. Initialize echoes a requested
version when it is in that list. Any other version is answered with
`2025-11-25`. Protocol `2026-07-28` is not implemented. OpenCode's default
`legacy` handshake, through `2025-11-25`, is the handshake this server answers.

With no CLI arguments the process is the MCP server, and stdout carries only
newline-delimited JSON-RPC frames. Diagnostics go to stderr and carry no
request content. `--version`, `readiness`, `status`, and `consent` are human
commands. MCP clients do not send them.

Update `src/agiw_mcp/vendor/nisi/` only through AGIW Suite's bundle script,
`bundle_nisi.py`. Do not hand-edit the package, `manifest.json`, or the pinned
manifest hash in `nisi.py`. The server checks that hash and every file hash
before a run. This repository does not contain the bundle script.

A ticket is `{ticketId, claimState, writeScope, owner}`. Only `Granted` or
`Active` is admitted. It is optional unless `require_ticket` is true
(`TICKET_REQUIRED` if missing). Every path must lie inside `writeScope`.
Scopes compare case-insensitively and under Unicode normalization. Equal
scopes and `/` prefixes overlap and are refused before Node starts. Windows
device names in a scope part are refused.

The ticket is released when the run ends, including on failure. On Windows,
admission raises `TICKET_UNSUPPORTED_PLATFORM` before the ticket directory is
touched. State lives in `tickets/` under the config directory, or in the
absolute path `AGIW_MCP_TICKET_DIR`. A lock whose local owner is confirmed
dead can be reclaimed after 60 seconds. Missing or unverifiable owner data
stays fail-closed.

Consent is the SHA-256 of the disclosure in `consent.py`. A changed disclosure
invalidates a stored grant. `agiw_jev_pre` and `agiw_jev_post` refuse with
`CONSENT_REQUIRED` before a key lookup or a network call. `agiw_consent` can
`show` or `revoke`. It cannot grant (`CONSENT_USER_ONLY`). Granting is
`agiw-mcp consent grant` in an interactive terminal. The confirmation is the
two words "I agree". Do not write `consent.json` and do not automate that prompt.
