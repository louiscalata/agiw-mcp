# AGIW MCP

**A careful coding workflow on your own local models, for any MCP-capable assistant.**

[![CI](https://github.com/louiscalata/agiw-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/louiscalata/agiw-mcp/actions/workflows/ci.yml)
![Licence: Apache-2.0](https://img.shields.io/badge/licence-Apache--2.0-blue)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-informational)
![Node.js 22+](https://img.shields.io/badge/node.js-22%2B-informational)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen)

AGIW MCP is a small local server for the [Model Context Protocol](https://modelcontextprotocol.io).
Add it to Claude Code, Factory or OpenCode, and your assistant gets three things:

| | |
|---|---|
| **Readiness check** | Say *"initiate online code mode"*. It checks this machine and answers `READY`, or names the one thing to fix first. |
| **Nisi workflow** | One model drafts, syntax checks and text assertions run, a second model reviews, and a short repair loop follows. You get proposed file contents and a report. It runs on your local models; project tests are never executed. |
| **Jev checks** | Optional advisory questions before and after a task (risk, missing detail, unbacked claims), answered by TypeSafe's Jev model. Nothing is sent until you consent. |

Python standard library only, plus Node.js for Nisi. No telemetry.

## Quick start

**1. Install this source checkout**

The changes described here are unreleased. Run from the reviewed checkout; record
its commit and local diff when reporting issues. The package version remains `0.1.0`
until a release is assigned; it alone does not identify these changes.

```bash
pipx install .
```

**2. Point it at your local model server.** You need an OpenAI-compatible server on
your own machine (for example LM Studio) and two different models: an author and a
reviewer. Save this as `config.json` in `~/Library/Application Support/agiw-mcp/`
(macOS) or `~/.config/agiw-mcp/` (Linux):

```json
{
  "model_base_url": "http://127.0.0.1:1234/v1",
  "author_model": "your-author-model-id",
  "reviewer_model": "your-reviewer-model-id"
}
```

**3. Add it to your assistant**

```bash
claude mcp add agiw-mcp -- agiw-mcp
```

(Factory and OpenCode are under [Setup details](#setup-details).)

**4. Say "initiate online code mode".** The assistant runs the readiness check and
tells you what, if anything, is missing.
`READY` covers the local prerequisites. Optional Jev readiness is reported separately
in `jev.status` (`READY` or `NOT_READY`) with its consent and key checks.
`agiw_nisi_run` works without Jev; key lookup is skipped until consent is recorded.

## What it will and won't do

- **It proposes; it never edits.** Nisi receives file contents in the request and
  returns proposed contents. It never reads or writes your project files and never
  runs generated code. You decide what to apply.
- **Local models only.** The model server must be on `127.0.0.1` or `::1`.
  Hostnames, HTTPS and other addresses are refused.
- **Jev is opt-in through MCP.** The MCP tool cannot grant consent. The CLI asks
  for confirmation in a terminal, but any process with same-user shell or config
  write access can change the consent record. This is not an OS user-presence
  boundary. Jev's answers are advisory, not a verdict.
- **One tool call at a time per server process.** During Nisi runs, ping and
  cancellation remain responsive. Separate server processes can run concurrently.
- **No telemetry.** The only per-call log is a Jev activity log of hashes, sizes
  and outcome codes, never your text. Other local state includes `config.json`,
  `consent.json`, the rotated `jev-activity.jsonl.1`, and ticket admission state.

## Tools

| Tool | What it does |
|---|---|
| `agiw_readiness` | Checks prerequisites in order and returns `READY` or the first missing one with its fix. Starts nothing. |
| `agiw_status` | Versions, configuration and which prerequisites are met. No secrets. |
| `agiw_consent` | Shows exactly what Jev would receive, or revokes consent. The MCP tool cannot grant consent. |
| `agiw_jev_pre` | Advisory Jev questions about a task before you start (task text up to 12 KiB). |
| `agiw_jev_post` | Advisory Jev questions about a result or report (truncated to 12 KiB). |
| `agiw_nisi_run` | One Nisi v0.2.0 workflow run on the file contents you supply, with your local models. |

A local task: `agiw_nisi_run` → you review the proposed files. If you choose to
configure Jev, add `agiw_jev_pre` before the run and `agiw_jev_post` after it.

## Setup details

<details>
<summary><b>Requirements and the local model server</b></summary>

- Python 3.10 or newer.
- macOS and Linux are covered by CI. Windows is experimental and not covered by CI;
  ticket admission is disabled there.
- Node.js 22 or newer on the `PATH` of the program that starts `agiw-mcp` (your MCP client).
- A local OpenAI-compatible chat server that supports JSON-schema structured output
  (`response_format: {"type": "json_schema"}`), listening on
  `http://127.0.0.1:<port>/v1` or `http://[::1]:<port>/v1`.
- Two **different** models. The review is only independent if the reviewer is a
  different model. Use the exact IDs that `GET <model_base_url>/models` lists:
  Nisi refuses a response whose `model` field differs from the configured ID.

Try it without installing:

```bash
uvx --from . agiw-mcp --version
```

</details>

<details>
<summary><b>Configuration file and environment variables</b></summary>

| OS | Config directory |
|---|---|
| macOS | `~/Library/Application Support/agiw-mcp` |
| Linux | `$XDG_CONFIG_HOME/agiw-mcp` (default `~/.config/agiw-mcp`) |
| Windows | `%APPDATA%\agiw-mcp` |

`AGIW_MCP_CONFIG_DIR` overrides the config directory and must be an absolute path.

All keys in `config.json` are optional; unknown keys are refused. Besides the three
shown in the quick start: `keychain_service` (default `agiw-mcp`), `keychain_account`
(default `typesafe`), `jev_model` (default `jev-1.14.0`), `max_output_tokens`
(default 4096), `reasoning_effort` (unset by default) and `require_ticket` (default `false`).

`require_ticket: true` makes `agiw_nisi_run` refuse any request without a claim ticket.

`reasoning_effort` (`none`, `low`, `medium` or `high`) is added to each Nisi chat request. Set it
to `none` for reasoning models such as Qwen 3 or Granite 4 on LM Studio: under JSON-schema output they
otherwise spend their tokens on hidden reasoning and return empty answers.

The model settings can come from the environment instead:
`AGIW_MCP_BASE_URL` (or the legacy `AGIW_MCP_MODEL_BASE_URL`, used only when
`AGIW_MCP_BASE_URL` is unset or empty), `AGIW_MCP_AUTHOR_MODEL`, `AGIW_MCP_REVIEWER_MODEL`,
`AGIW_MCP_REASONING_EFFORT`.

| Config key | Environment variable | Precedence and validation |
|---|---|---|
| `model_base_url` | `AGIW_MCP_BASE_URL` | `AGIW_MCP_BASE_URL`, then legacy `AGIW_MCP_MODEL_BASE_URL` when `AGIW_MCP_BASE_URL` is unset or empty, then the config file, then `http://127.0.0.1:1234/v1`. Only HTTP loopback literals (`127.0.0.1` or `[::1]`) are accepted; for a server on port 1235, use `http://127.0.0.1:1235/v1`. |
| `reviewer_base_url` | `AGIW_MCP_REVIEWER_BASE_URL` | Optional. `AGIW_MCP_REVIEWER_BASE_URL`, then the config file, then unset, which means the reviewer uses `model_base_url`. Set it to send the reviewer's requests to a different local server, for example `http://127.0.0.1:1245/v1`. Same loopback-only rule as `model_base_url`. |

Check your setup at any time:

```bash
agiw-mcp --version
agiw-mcp readiness
```

</details>

<details>
<summary><b>Jev key and consent</b></summary>

Jev needs your own TypeSafe API key. agiw-mcp looks for it in this order and never
stores it in a plain file:

1. The environment variable `TYPESAFE_API_KEY`, set in the MCP client's environment
   for this server.
2. The macOS Keychain. Type the key at the prompt (not on the command line, where it
   would be saved in your shell history):

   ```bash
   security add-generic-password -s agiw-mcp -a typesafe -w
   ```

3. The Linux Secret Service (GNOME Keyring, KWallet), which also prompts:

   ```bash
   secret-tool store --label='agiw-mcp TypeSafe' service agiw-mcp account typesafe
   ```

On Windows, use the environment variable. On macOS, the readiness check
verifies presence without reading the key. On Linux, `secret-tool lookup`
returns the value, which agiw-mcp discards.

The Jev tools refuse to run until you consent:

```bash
agiw-mcp consent show      # exactly what is sent, and to where
agiw-mcp consent grant     # interactive; type I agree and press Return
agiw-mcp consent revoke
```

The confirmation is case-insensitive. In order, the prompt trims outer
whitespace, strips leading and trailing straight single or double quote
characters, removes all trailing periods, trims outer whitespace again, and
compares the result case-insensitively with `i agree`. For example, `I AGREE`,
`'I agree'`, `I agree...` and `"I agree."` are accepted; `"I agree".` is not,
because quote stripping happens before period removal. Extra words or a
different space between the two words are not accepted. Non-interactive input
is refused, and an unrecognized confirmation records nothing.

Consent is tied to a hash of the disclosure text, so if a later version changes what
is sent, you are asked again. See [SECURITY.md](SECURITY.md).

</details>

<details>
<summary><b>Client setup: Claude Code, Factory, OpenCode</b></summary>

**Claude Code**

```bash
claude mcp add agiw-mcp -- agiw-mcp
```

With the model settings in the environment:

```bash
claude mcp add agiw-mcp -e AGIW_MCP_AUTHOR_MODEL=your-author-model-id \
  -e AGIW_MCP_REVIEWER_MODEL=your-reviewer-model-id -- agiw-mcp
```

**Factory (droid)**

```bash
droid mcp add agiw-mcp agiw-mcp
```

or in `~/.factory/mcp.json`:

```json
{
  "mcpServers": {
    "agiw-mcp": { "type": "stdio", "command": "agiw-mcp", "args": [] }
  }
}
```

**OpenCode** (`opencode.json` or `opencode.jsonc`):

```json
{
  "mcp": {
    "agiw-mcp": { "type": "local", "command": ["agiw-mcp"], "enabled": true }
  }
}
```

If the client cannot find `agiw-mcp`, use the absolute path that
`command -v agiw-mcp` prints, and make sure `node` is on that client's `PATH`.

</details>

<details>
<summary><b>Nisi run arguments and limits</b></summary>

`agiw_nisi_run` takes `task` (required), `files` (a list of `{path, content}` with
relative paths using ASCII letters, digits, `._@+-` and `/`, with no empty, `.` or `..`
parts, trailing-dot parts or Windows device names such as `nul.txt` and `aux.txt`),
`new_files` (paths the author may create), `acceptance_criteria`,
`assertions` (`{path, contains}` or `{path, not_contains}`), `repair_budget` (0 to 3,
default 1), `timeout_seconds` (30 to 1800, default 600) and an optional `ticket`.

Paths are at most 256 characters; each part is at most 128. A part starts with
a letter, digit or `_@+-`, optionally preceded by one dot, and may then contain
letters, digits or `._@+-`. The same rules apply to `new_files`.

A `ticket` is `{ticketId, claimState, writeScope, owner}`, supplied by the caller for
cooperative exclusion across processes sharing the registry. It is not independent
authorization: the caller also chooses `claimState` and `owner`. Only `claimState`
`Granted` or `Active` is admitted; `writeScope` is a list of `/`-separated path scopes.
Every path in `files` and `new_files` must equal or lie below a scope, otherwise
`TICKET_SCOPE_MISMATCH` is returned before admission. The lock record retains `owner`. Before
Node is started or a temporary folder is created, the ticket is admitted all-or-nothing: it
is refused (`TICKET_SCOPE_CONFLICT`) if any scope equals, contains or is contained in a scope
of a ticket already admitted, by this or another agiw-mcp process. Scopes are compared
ignoring case and Unicode normalization (`README.md` and `readme.MD` are the same scope).
Scope parts that Windows would alias are refused (`TICKET_INVALID`) rather than normalized:
parts ending in `.` or a space (`calc.py.`), parts containing `:`, and device names such as
`CON`, `NUL`, `COM1` or `LPT1` with any extension. The ticket is released when the run ends.
Admission state lives in `tickets/` under the config directory, or in `AGIW_MCP_TICKET_DIR`
(an absolute path). On macOS and Linux that directory must not be a symlink and must be owned
by you with mode `0700` (a looser mode is tightened if possible), and every directory above
it, including through symlinks, must be owned by you or root and not writable by group or
others unless sticky (like `/tmp`); otherwise admission is refused (`TICKET_REGISTRY_UNSAFE`).
A run's locks are released only from the directory they were created in. Admission and scope
locks record a machine identity, PID, optional process start identity and creation time.
After SIGKILL or power loss, a later admission automatically reclaims a lock only when both
its creation time and file modification time are at least 60 seconds old, its machine identity
matches this host (and Linux PID namespace), and a PID liveness check confirms `ESRCH` (no process).
Recorded start identities are checked when present. A live PID is always kept, even if its start
identity differs; permission errors and uncertain liveness also keep the lock. Recovery uses an
inode-level advisory lock and verifies that the pathname still names that inode before unlinking,
so competing reclaimers cannot remove a replacement lock. The kernel releases that advisory lock
on process exit; recovery creates no extra lock file that could itself be stranded.
Each successful removal emits `TICKET_LOCK_RECLAIMED kind=admission|scope pid=...`
with a reason and age through Python logging (stderr by default), without owner text or scope paths.
Foreign-host locks, legacy locks without process-owner metadata, malformed/partial records and
locks whose machine identity cannot be verified remain fail-closed. These require manual cleanup
only after independently confirming that no runs are active. Recovery requires POSIX descriptor
operations and `flock`; do not share registries between cloned machines with identical machine IDs
or use filesystems without reliable advisory locking. Machine identity comes from Darwin
`gethostuuid` or Linux `/etc/machine-id`; hostname alone is never trusted.
On Windows, ticket admission is disabled for now: any ticket is refused
(`TICKET_UNSUPPORTED_PLATFORM`) before the state directory is touched, until agiw-mcp can
check the directory for reparse points, ownership and permissions. Runs without a ticket
still work on Windows unless `require_ticket` is `true`.
Refusals name only the ticket id.

Its static checks compile Python (`compile` without execution), parse JavaScript
(`node --check`) and JSON; other file types get structural checks only, and the result says which files were
checked. Python uses the interpreter running agiw-mcp; `pythonCheckerVersion`
reports that version, so syntax for a newer Python may fail. Its tests are: at least
one file changed, no proposed file is empty, and your assertions. Limits: `files` and `new_files` each allow up to 64 entries, with no
combined cap. Supplied file contents are limited to 256 KiB per file and 512 KiB
in total, measured as UTF-8 bytes. Proposed file contents have a separate 2 MiB
total limit.

Tool calls are serialized per process. During Nisi runs, ping and cancellation are
handled while up to 16 other messages wait in the pending queue (with an additional
bounded input queue). Cancelling the active request or closing stdin stops its
runner and suppresses its reply. SIGTERM/SIGHUP also trigger cleanup. The runner
limit is `timeout_seconds + 30`; process and stream cleanup can add further waits.
Other tools still run synchronously. Nisi's `COMPLETED` means the configured checks, tests
and review passed; it is not a guarantee of correctness.

The result's `taskFingerprint` is Nisi's task-record hash, which excludes the supplied
text and contents. `inputFingerprint` binds the host request's task, file contents,
assertions, model/check options and Python version (excluding only the interpreter
path). Each model receipt includes it. `requestSha256` hashes the final body passed
to fetch; `adapterRequestSha256` preserves the original vendor hash before the host
adds `reasoning_effort`. A null transport hash means fetch was not invoked, not
proof that a provider received nothing.

</details>

<details>
<summary><b>Uninstall</b></summary>

```bash
claude mcp remove agiw-mcp          # or remove it from your client's config
pipx uninstall agiw-mcp
```

Then delete the config directory listed above, and the Keychain or Secret Service
item if you created one:

```bash
security delete-generic-password -s agiw-mcp -a typesafe     # macOS
secret-tool clear service agiw-mcp account typesafe          # Linux
```

</details>

## Development

```bash
python3 scripts/scrub_check.py
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
```

The tests never reach the network: the model server and Jev are fakes on 127.0.0.1,
test mode refuses non-loopback URLs, and a socket guard blocks any other connection.
Without Node 22+, a fake runner covers the Python side. Binding the loopback fakes
needs local port binding to be allowed.

## Security and licence

Security model and how to report a problem: [SECURITY.md](SECURITY.md).

Apache-2.0. See [LICENSE](LICENSE), [NOTICE](NOTICE) and [PROVENANCE.md](PROVENANCE.md).
The vendored Nisi v0.2.0 package keeps its own LICENSE and manifest in
`src/agiw_mcp/vendor/nisi/`.
