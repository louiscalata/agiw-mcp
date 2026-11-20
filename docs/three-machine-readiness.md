# Three-machine readiness

AGIW MCP should stay ready to work with the AGIW Suite × Nisi setup on three
machines: the M5, the Windows PC, and Monterey (the Mac Pro). This page records
what the `agiw-mcp` repository already covers, how to check it, and what is
still NOT_RUN. It does not assign a version, tag, or publish a release.

AGIW Suite does not start this server. The shared pieces are the vendored Nisi
tree, the LICENSE bytes, and the Jev model and endpoint pin. Check those with
`scripts/suite_compat.py` against a local AGIW Suite checkout:

```bash
PYTHONPATH=src python3 scripts/suite_compat.py /path/to/agiw-suite
```

A passing run prints `suite compatibility passed`.

## What continuous integration already covers

Same-repo macOS jobs use the existing `mac-m5` runner. Same-repo Windows jobs
use the existing `pc-win` runner. Linux jobs and fork pull requests use
GitHub-hosted runners. See [AGENTS.md](../AGENTS.md) and
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

- Unit tests, the scrub check and `compileall` on Linux and macOS for Python
  3.10, 3.12, 3.13 and 3.14, with Node 22, plus Python 3.13 with Node 24.
- The same checks on Windows with Python 3.12 and Node 22. Ticket-admission
  tests are skipped there because admission itself is refused
  (`TICKET_UNSUPPORTED_PLATFORM`) before the ticket directory is touched.
  Runs without a ticket still work on Windows unless `require_ticket` is true.
- A Python-only job with Node hidden, so the fake runner covers the Python
  side.
- Built wheel and sdist install checks, a git install with an MCP stdio
  handshake, and MCP Inspector `tools/list` for the six tool names.
- `suite-compat`, which clones AGIW Suite `main` and runs
  `scripts/suite_compat.py`.

CI does not run on Monterey. CI does not send a live TypeSafe request, start a
local model server, or launch Claude Desktop, Cursor, Claude Code, Factory or
OpenCode.

## Monterey

The supported Python floor is 3.10. macOS 12 Monterey's system Python 3.9
cannot run the server; install Python 3.10 or newer there first, then use that
interpreter for the commands below. Node.js 22 or newer is required only when
a real Nisi run or the real-runner tests are needed.

## Local commands on any of the three machines

From a checkout, with Python 3.10 or newer and, for the Nisi tests that need
it, Node 22 or newer:

```bash
python3 scripts/scrub_check.py
python3 -m compileall -q src
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -q
```

If Ruff 0.17.4 is already available:

```bash
ruff check src tests scripts
ruff format --check src tests scripts
```

If it is not installed, leave those two checks as NOT_RUN rather than
installing a package unless the maintainer has said to. Do not publish to
PyPI, create a tag, or download models for this check. Do not start PAIR or
Ollama, and do not use reserved local ports that other work on the machine
already owns.

A passing unit-test run ends with `OK`. Write NOT_RUN for any command that did
not actually run, including a live Jev request, a real local model run, or a
check on a machine that was not used.

## Checks recorded with this page

These were run on the M5 from `agiw-mcp` `c14a0dd`, without installing
packages:

- Scrub check: passed (Python 3.14.10)
- `compileall -q src`: passed (Python 3.14.10)
- `ruff check` and `ruff format --check`: passed (Ruff 0.17.4)
- Unit tests: 1568 OK on Python 3.12.13 and on Python 3.14.10, with Node.js
  v24.22.2
- `scripts/suite_compat.py` against AGIW Suite `c104572`: passed (Nisi 0.3.2,
  22 files)
- `python3 -m build`, Python 3.10 and 3.13 on this machine, Monterey unit
  tests, a live TypeSafe request, a real local model run, and launching an MCP
  client: NOT_RUN

Windows unit tests continue to run in CI on the `pc-win` runner. A fresh
hands-on checkout on the PC and on Monterey remains NOT_RUN until someone runs
the commands above there and records the result.
