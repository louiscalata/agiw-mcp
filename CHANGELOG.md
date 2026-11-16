# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The only published tag is `v0.1.0`. Later commits on main stay version `0.1.0`
until a release is assigned. Entries under Unreleased are not a new release.

## [Unreleased]

### Added

- Optional `check_python` key in `config.json`: an absolute path to the Python interpreter that checks the syntax of Python candidates. Unset keeps the interpreter that runs agiw-mcp. The key is read only from `config.json`, is not a tool argument, and is refused as unknown by a build from before this change. After ticket admission and before the temporary directory is created, the run probes the interpreter with fixed `-I -S -B` flags and a 3 second limit and accepts only a plain `X.Y.Z` version. A failed probe refuses with `CHECK_PYTHON_UNAVAILABLE`, whose message does not contain the path, and releases an admitted ticket. `pythonCheckerVersion` in the result is the selected interpreter's version. `agiw_status` reports `checkPython`, and `agiw_readiness` adds a `syntax_python` check after `nisi` only when the key is set. The server enforces no minimum version for the selected interpreter. (#53)
- Optional progress notifications for `agiw_nisi_run`. A string or integer `_meta.progressToken` on the call requests them; a boolean or other value counts as absent, and other tools ignore the token. The server sends `notifications/progress` for the `author`, `static`, `tests` and `review` stages, each `started` and `finished`, with an increasing `progress` count from 1 and no `total`. At most 64 events are forwarded, and an event is dropped if the internal queue is full. The `message` (`<stage> attempt <n> <status>`) is sent only when the negotiated protocol is `2025-03-26` or newer. No progress is sent after cancellation or the result. A call without a valid token produces the same stdout frames as before. (#56)

### Changed

- **Behavior change.** `agiw_nisi_run` refuses with `NODE_SANDBOX_UNAVAILABLE` when `node --help` lists neither `--permission` nor `--experimental-permission`. Previously the run proceeded without a file-access limit and reported sandbox `"none"`. Use a Node.js that lists the flag, or set `"allow_unsandboxed_nisi": true` in `config.json`. The key is read only from `config.json`. A build from before this change refuses a `config.json` that contains it. (#48)
- `agiw_status` reports `node.sandbox`. `agiw_readiness` fails the node check when Node has no permission model and `allow_unsandboxed_nisi` is false, or when `node --help` fails. (#48)

### Fixed

- Nisi runs work with an IPv6 loopback endpoint (`http://[::1]:<port>/...`). The host transport previously refused the bracketed hostname that Node returns. (#42)
- The host transport refuses a model response body above 8 MiB while receiving it, with `LOCAL_CHAT_RESPONSE_TOO_LARGE`, for both success and error responses. Previously the whole body was buffered before the size check. (#42)
- Configured base URLs with an empty trailing `?` or `#` are rejected as invalid configuration. (#42)
- The host runner checks the raw author and reviewer endpoint text before parsing it and refuses spellings that configuration rejects, such as `127.1`, `0x7f.1` or userinfo, with `LOCAL_CHAT_DESTINATION_REFUSED`. (#42)
- The host runner refuses author and reviewer endpoint ports outside 1-65535 with `LOCAL_CHAT_DESTINATION_REFUSED`, matching configuration. (#43)
- Ticket admission refuses a scope lock that is not a regular file, is larger than 16 KiB, or is nested too deeply to parse (`TICKET_REGISTRY_CORRUPT`), instead of blocking or failing with an internal error. (#46)
- A Jev or model-server reply that is nested too deeply to parse is refused as an invalid response instead of failing with an internal error; Jev reports `JEV_BAD_RESPONSE` and still logs the call. A deeply nested `config.json` is `CONFIG_INVALID`. A `config.json` that is not a regular file is `CONFIG_UNREADABLE` and no longer blocks the server. A deeply nested or non-regular `consent.json` reads as consent not recorded. (#52)

### Documentation

- Correct the README and SECURITY limitations found by a public review: separate author and reviewer model servers, `reasoning_effort` omitted unless set, valid empty configuration, the tagged README's consent limit, and Windows CI coverage in CONTRIBUTING. The roadmap now lists release and validation work instead of session history. (#41)
- SECURITY.md maps selected MCP security practices to this stdio server and preserves the same-user consent limit.
- `docs/release-notes/next.md` preserves historical review notes through pull request #21; it is not a complete account of current main. The version stays `0.1.0`.
- README client setup includes Claude Desktop (`claude_desktop_config.json`) and Cursor (`.cursor/mcp.json` and `~/.cursor/mcp.json`).
- Use US spellings in the README, contributing notes, and provenance (`license`, `behavior`). The Claude Desktop section distinguishes the current guide's macOS, Windows and Linux paths from the 2025-11-25 snapshot, which lists only macOS and Windows.
- Point the OpenCode example at `mcp.servers` with `type: local` and `codemode: false`. Add the Linux `secret-tool store` command. State that OpenCode protocol `2026-07-28` is not implemented.
- The README lists the legacy `initialize` versions the server accepts: `2025-11-25`, `2025-06-18`, `2025-03-26`, and `2024-11-05`.
- `docs/design/v0.2.md` proposes the next scope of work as lanes L1 to L6, with owner decisions D1 to D5. (#47)
- The `agiw_readiness` description returned by `tools/list` now says the Node check needs a Node.js 22+ whose `node --help` lists a permission flag, or `allow_unsandboxed_nisi` set in `config.json`, and lists the checks in the order readiness runs them. SECURITY.md states that `jev-activity.jsonl` is rotated to `.1` above 1 MiB, so only the current file and one previous file are kept. (#55)
- README and SECURITY.md describe `check_python` (#53) and progress notifications (#56).

### Tests

- Python style: `docs/python-style.md` records the convention. A new `lint` CI job pins Ruff 0.17.4 and runs `ruff check` and `ruff format --check`; the unpinned Ruff step left the `distribution` job. The rule set adds `I`, `UP`, `N`, `C90`, `BLE`, `T20`, `RUF100`, and `RUF102`, with an import-order baseline for ten files. All Python files outside the vendored bundle were formatted with `ruff format`; the AST, with docstrings normalized, is unchanged, and so is the consent disclosure hash.
- The git-install CI job lists tools with MCP Inspector 2.14.0. It does not call Jev or a model.
- Windows CI runs the scrub check, compile, and unit tests on Python 3.12 and Node 22. Ticket-admission tests are skipped there because admission is refused before the ticket directory is touched.
- The trickling-server deadline test waits up to a bounded time for the client's close (EOF or reset) instead of a fixed one-second join, which failed once on a busy Windows runner. Test-only; `net.py` is unchanged. (#44)
- A test pins the SHA-256 of the consent disclosure, so an edit that would invalidate stored grants fails a test first. (#48)
- New test modules cover the reader limits (#52), `check_python` (#53), the readiness description and the Jev log rotation (#55), and Nisi progress (#56).

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
