# Contributing

Issues and small pull requests are welcome. This repository has merged both. Keep a change focused: one problem, the code that fixes it, and the check that shows it.

## Reports

Open a [GitHub issue](https://github.com/louiscalata/agiw-mcp/issues) for a bug or for a README claim the code does not support. Include the command you ran, the exit code, and the JSON or text the program printed. Include the git revision (`agiw-mcp --version` prints `0.1.0` for every commit since the tag, so the revision matters).

Do not include API keys, tokens, private host names, or machine addresses. A refusal code and the input shape are enough. If the report is a vulnerability, use the private advisory path in [SECURITY.md](SECURITY.md) instead of a public issue.

## Changes

Branch from `main`. Do not commit secrets. Do not tag, publish a release, or bump the version past `0.1.0` unless the maintainer has assigned that release. The version string alone does not identify unreleased commits.

From a checkout:

```bash
python3 scripts/scrub_check.py
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
python3 -m compileall -q src
```

CI also runs `ruff check src tests scripts` (selected rules `E`, `F`, `W`, `B`) and installs the built wheel and sdist outside the checkout. The tests do not use the network: model and Jev calls are local fakes, and a socket guard blocks anything else. Node 22 or newer runs the real Nisi runner; without it, a fake runner covers the Python side.

A pull request that changes install or the MCP entry point should say which install command was run and what `agiw-mcp --version` and the stdio handshake printed. `python3 scripts/stdio_handshake.py agiw-mcp` does that handshake once the command is on `PATH`.

Windows is not in CI. Ticket admission is disabled there. Say so if your change touches it.

Apache-2.0. A contribution is under that license. Third-party material needs its origin and the attribution it requires. See [PROVENANCE.md](PROVENANCE.md) for the files copied from AGIW Suite and Nisi.
