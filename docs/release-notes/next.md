# Next release notes

These historical notes cover selected pull requests through #21 after tag `v0.1.0` (`f69630a`). They are not a complete list of changes on current main. See [CHANGELOG.md](../../CHANGELOG.md) for the current unreleased summary. The package version remains `0.1.0`.

- #1 keeps `SystemRoot`, `SYSTEMDRIVE`, and `WINDIR` in the Windows environment used to probe Node.
- #2 brings the Mac working copy of config, keys, readiness, and Nisi response handling into this repo, with more tests.
- #3 admits a claim ticket before a Nisi run and refuses tickets on Windows before the ticket directory is touched.
- #4 lets the reviewer use a second loopback model server.
- #9 adds a pull request template.
- #10 reclaims ticket locks left by a dead local process when the age, machine, and PID checks agree.
- #11 corrects the docs so they match the code.
- #12 makes the scrub check skip a worktree `.git` pointer and names the MCP client `agiw`.
- #13 keeps the still-needed fixes from the closed Cursor pull requests.
- #14 removes unused ticket wrappers and corrects the consent comment.
- #15 rewrites the README as a landing page and checks a git install plus AGIW Suite main in CI.
- #16 documents Claude Desktop and Cursor stdio config.
- #17 stops the setuptools warning about vendored Nisi directories without changing the installed files.
- #18 points the OpenCode example at `mcp.servers` and keeps the Claude Desktop and Cursor blocks.
- #19 ignores one gitleaks hit, the published SHA-256 of `policy/file-access.mjs` in the vendored Nisi manifest.
- #20 uses US spellings and says the Claude Desktop guide does not list a Linux config path.
- #21 renames the unreleased changelog sections so they match what the notes describe.

## Verification in this historical review

- Claude Desktop, Claude Code, Factory, OpenCode, and Cursor were not launched.
- macOS Keychain was not used.
- Windows and MCP Inspector were not covered by this historical review. Current CI includes a Windows unit-test job and MCP Inspector `tools/list`.
- `pipx install` was not run in these notes.
- A live request to TypeSafe was not sent.
- OpenCode protocol `2026-07-28` is not implemented, and that client was not started.
