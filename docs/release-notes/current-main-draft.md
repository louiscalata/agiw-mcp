# Current main release draft

Status: draft. Version: TBD pending the maintainer. This file does not choose a
version, create a tag, or publish a release.

## Scope and commit boundary

The only published release is the `v0.1.0` tag at commit `f69630a`. This draft
covers current main at commit `28efb69`, so it summarizes changes after
`f69630a` up to that commit. Later commits need a fresh check.

Current main still reports `0.1.0`. That string does not identify this commit;
use the git revision. The package stays Python 3.10 or newer, standard library
only, with Node.js 22 or newer for a real Nisi run. It is not published to PyPI.

## User-facing changes since the published release

### Added

- Optional `reviewer_base_url`, so the reviewer can use a second loopback model
  server.
- Claim-ticket admission for `agiw_nisi_run`: a `{ticketId, claimState,
  writeScope, owner}` ticket, an optional `require_ticket` key, and recovery of
  a lock left by a dead local process when the age, machine, and PID checks
  agree.
- Compile Python candidates without executing them, and reject invalid module
  control flow during the static check.
- Cancellation of a Nisi run on request cancellation or stdin EOF, responsive
  ping during a run, and cleanup of the runner, temporary directory, and ticket
  on handled termination signals.
- Optional `check_python` in `config.json`: an absolute path to the interpreter
  used for Nisi's Python syntax check. The probe uses fixed `-I -S -B` flags and
  a 3 second limit and accepts only a strict `X.Y.Z` version.
- Optional `allow_unsandboxed_nisi` in `config.json` for a Node whose
  `node --help` lists no permission flag. The default is `false`.
- Optional `nisi_activity_log` in `config.json`, off by default. When enabled,
  it appends one line per validated `agiw_nisi_run` call with hashes, codes, time
  and counts. It never records task text, file contents, paths, ticket ids,
  owners, model ids, endpoints or keys.
- Optional progress notifications for `agiw_nisi_run` when the call carries a
  string or integer `_meta.progressToken`. At most 64 events are forwarded;
  `progress` starts at 1 and has no `total`.
- Independent readiness reporting for local prerequisites and Jev.
- Explicit receipt fingerprints for host inputs and the final transport body.

### Changed

- **Behavior change.** `agiw_nisi_run` now refuses with
  `NODE_SANDBOX_UNAVAILABLE` when Node lists neither `--permission` nor
  `--experimental-permission`. The refusal comes after ticket admission and
  before the temporary directory is created. Set `allow_unsandboxed_nisi` to
  `true` only if a run without a file-access limit is intended.
- `agiw_status` reports `node.sandbox`, `checkPython`, and the new configuration
  keys. `agiw_readiness` fails the node check when the sandbox is unavailable
  and can add a `syntax_python` check when `check_python` is set.
- The consent disclosure was corrected after `v0.1.0`. A grant stored under the
  tag must be granted again on current main. Grants made on current main remain
  valid.
- README and SECURITY text was corrected where it overstated the consent
  guarantee, described the model servers, or omitted client setup.

### Fixed

- Nisi runs work with an IPv6 loopback endpoint
  (`http://[::1]:<port>/...`).
- Model response bodies above 8 MiB are refused while they are received, with
  `LOCAL_CHAT_RESPONSE_TOO_LARGE`.
- Base URLs with an empty trailing `?` or `#`, endpoint spellings that
  configuration rejects, and endpoint ports outside 1-65535 are refused before
  the request.
- Ticket admission refuses a scope lock that is not a regular file, is larger
  than 16 KiB, or is nested too deeply to parse, as `TICKET_REGISTRY_CORRUPT`.
- Deeply nested JSON from Jev, model servers, `config.json` and `consent.json`
  fails closed. A non-regular `config.json` and `consent.json` are handled
  without blocking the server.
- The opt-in Nisi activity log serializes rotation and append, rolls back short
  writes, refuses hard links and symlinked config directories, and records
  malformed nullable metadata as null.
- Tracked HTTP sockets are closed on deadlines, and outstanding HTTP workers are
  capped.

## Compatibility and consent cautions

- Python 3.10 or newer; Node.js 22 or newer for Nisi. Apache-2.0. No runtime
  dependencies.
- Current main still reports `0.1.0` for every commit since the tag. Install
  from a checkout or git and record the revision; `agiw-mcp --version` alone
  does not identify the code.
- A `config.json` that uses `check_python`, `allow_unsandboxed_nisi` or
  `nisi_activity_log` is refused by an older build as `CONFIG_INVALID`. Remove
  those keys before downgrading.
- The Node refusal is a behavior change. Use a Node.js whose help lists a
  permission flag, or set `allow_unsandboxed_nisi` in `config.json`.
- A consent grant from `v0.1.0` no longer matches the corrected disclosure and
  must be granted again. A current-main grant stays valid. Consent remains a
  same-user file, not an OS user-presence check: any process that can write the
  config directory can change `consent.json`.
- Jev answers are advisory. `COMPLETED` means that run's checks passed; it is
  not a verdict that the proposal is right, and applying the files is a separate
  human decision.
- Progress notifications are optional. A client may not display them or extend
  its request timeout, and there is no heartbeat between events.

## Not implemented

- A release newer than `v0.1.0`, a new version number, a tag, a GitHub release,
  or a PyPI publication. The maintainer decides those.
- Ticket admission on Windows. It is refused as `TICKET_UNSUPPORTED_PLATFORM`
  before the ticket directory is touched. Runs without a ticket still work
  there unless `require_ticket` is true.
- OpenCode protocol `2026-07-28`. The default `legacy` handshake through
  `2025-11-25` is the one the server answers.
- Reading or writing the caller's project, launching an MCP client, downloading
  models, or starting a local model server.
- Applying proposed files or executing generated code.
- A live TypeSafe request or a real model run in the automated tests. CI uses
  local fakes.

## Checks in this draft

This draft was prepared by reading repository files only. No check below ran in
this pass.

- `python3 scripts/scrub_check.py`: NOT_RUN
- `python3 -m compileall -q src`: NOT_RUN
- `ruff check src tests scripts`: NOT_RUN
- `ruff format --check src tests scripts`: NOT_RUN
- `PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v`: NOT_RUN
- `python3 -m build`: NOT_RUN
- CI, including the Linux, macOS, Windows, no-Node, distribution, git-install
  and suite-compatibility jobs: NOT_RUN
- A live TypeSafe request, a real local model run, and launching Claude
  Desktop, Cursor, Claude Code, Factory or OpenCode: NOT_RUN

The version remains TBD until the maintainer assigns it. This draft makes no
