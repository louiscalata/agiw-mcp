# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The only published tag is `v0.1.0`. Later commits on main stay version `0.1.0`
until a release is assigned. Entries under Unreleased are not a new release.

## [Unreleased]

### Documentation

- `docs/release-notes/next.md` lists each merged pull request after `v0.1.0` in one sentence, plus what was not verified. The version stays `0.1.0`.
- README client setup includes Claude Desktop (`claude_desktop_config.json`) and Cursor (`.cursor/mcp.json` and `~/.cursor/mcp.json`).
- Use US spellings in the README, contributing notes, and provenance (`license`, `behavior`). The Claude Desktop section says the local-server guide lists macOS and Windows paths and does not list a Linux path.
- Point the OpenCode example at `mcp.servers` with `type: local` and `codemode: false`. Add the Linux `secret-tool store` command. State that OpenCode protocol `2026-07-28` is not implemented.
- The README lists the legacy `initialize` versions the server accepts: `2025-11-25`, `2025-06-18`, `2025-03-26`, and `2024-11-05`.

### Tests

- The git-install CI job lists tools with MCP Inspector 2.14.0. It does not call Jev or a model.

### Packaging

- Discover `agiw_mcp` namespace packages so a build no longer warns that the vendored Nisi directories would be ignored. The installed files are unchanged.

### README and distribution

- Rewrite the README as a landing page: pitch, the install and readiness commands that were run, how readiness, consent-gated Jev and Nisi work, and an honest status. Badges are CI, Apache-2.0 and Python 3.10+.
- Add [CONTRIBUTING.md](CONTRIBUTING.md). [SECURITY.md](SECURITY.md) was already present.
- Include `tests/support.py` and `scripts/` in the sdist. Distutils was adding `tests/test*.py` and omitting the helper those tests import. The wheel already contained the installed package.
- CI installs this commit with `uv tool install git+https://...@<sha>`, checks `--version`, and speaks MCP initialize and `tools/list`.
- CI clones AGIW Suite `main` and checks the shared Nisi v0.2.0 bundle, the LICENSE bytes, and the Jev model and endpoint.

### Already on main, not in 0.1.0

- Optional `reviewer_base_url`, so the reviewer can use a second loopback server.
- Claim-ticket admission for Nisi runs. Refused on Windows before the ticket directory is touched. Overlapping scopes are refused. Locks left by a dead process can be reclaimed when the machine, age and PID checks agree.
- Compile Python candidates without executing them, and reject invalid module control flow.
- Cancel a Nisi run on request cancellation or stdin EOF. Ping stays responsive during the run. Clean up the runner, temporary directory and ticket on handled termination signals.
- Close tracked HTTP sockets on deadlines and cap outstanding HTTP workers.
- Report local readiness independently of optional Jev setup.
- Bind host inputs and the final transport body to explicit receipt hashes.
- Correct the consent disclosure. The new text has a new hash, so earlier Jev consent has to be granted again.
- Installed wheel and sdist checks, and Ruff, in CI.
- Scrub check skips a worktree's `.git` pointer file. The MCP client name in the docs is `agiw`.

## [0.1.0] - 2026-10-24

First published tag, commit `f69630a`. Local stdio MCP server: readiness for online code mode, consent-gated Jev checks, and one Nisi v0.2.0 run on file contents supplied in the request. Python 3.10 or newer. Node.js 22 or newer for Nisi. Standard library only. Apache-2.0. Not published to PyPI.

[Unreleased]: https://github.com/louiscalata/agiw-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/louiscalata/agiw-mcp/releases/tag/v0.1.0
