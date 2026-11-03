# Security

## Reporting

Please report vulnerabilities privately through GitHub's "Report a
vulnerability" (security advisories) on `louiscalata/agiw-mcp`, not in a
public issue. Include the version, source commit, any local changes and the steps to reproduce.

## What leaves the machine

| Tool | Network destination | What is sent |
|---|---|---|
| `agiw_readiness`, `agiw_status` | your loopback model server only (`GET /models`; agiw-mcp waits up to 3 s per request) | nothing but the request line and headers |
| `agiw_consent` | none | nothing |
| `agiw_nisi_run` | your loopback model server only | the task text, the file contents you supplied, Nisi's prompts |
| `agiw_jev_pre`, `agiw_jev_post` | `https://api.typesafe.ai/v1/systemone` | your text (pre: at most 12 KiB; post: truncated to 12 KiB), the fixed questions, the Jev model name, and your API key in the `Authorization` header |

TypeSafe also sees your IP address and standard request metadata. The exact
disclosure is returned by `agiw_consent` (`show`) and `agiw-mcp consent show`.
agiw-mcp does not remove secrets from the text you pass to the Jev tools; do
not include any.

There is no telemetry and no update check. Python HTTP deadlines shut down tracked
sockets, including stalled headers and bodies. DNS/connect/TLS setup may outlive
the caller's deadline; at most two HTTP workers may remain outstanding per process,
and further calls fail rather than accumulating workers. A failed or timed-out Jev
exchange may already have sent its request. There is no automatic retry.

## Consent

- Enforced by the server. `agiw_jev_pre` and `agiw_jev_post` refuse with
  `CONSENT_REQUIRED` before looking up the key or opening a connection unless
  consent is recorded. Readiness and status also skip the Keychain and Secret
  Service lookups until consent is recorded, including when reporting all
  readiness checks. Status always reports whether `TYPESAFE_API_KEY` is set,
  never its value.
- Consent is bound to the SHA-256 of the disclosure text. A changed disclosure
  invalidates earlier consent.
- The `agiw_consent` tool can show or revoke consent but cannot grant it.
  `agiw-mcp consent grant` asks for confirmation in an interactive terminal.
  Any same-user process with shell or config write access, including an assistant,
  can grant or forge the JSON record. This is not an OS user-presence control.
- Consent is stored in `consent.json` in the config directory. On macOS and
  Linux, each write replaces it with a new file set to mode 0600. Setting the
  directory to mode 0700 is best effort.

## The API key

- Looked up in order: `TYPESAFE_API_KEY`, the macOS Keychain
  (`/usr/bin/security find-generic-password -s <service> -a <account> -w`), the
  Linux Secret Service (`secret-tool lookup service <service> account <account>`).
  Plaintext key files are not supported.
- Presence checks run after consent: the macOS query omits `-w`; the Linux
  `secret-tool lookup` retrieves the secret, but its output is sent to
  `/dev/null` so only the exit code is used.
- The Jev tools read the value after the consent check and send it only in the
  `Authorization` header to the fixed TypeSafe endpoint. Redirects are
  refused, so the key is never re-sent elsewhere.
- Every Jev key lookup failure returns `KEY_UNAVAILABLE` with one fixed
  message. Errors never include the key, any part of it, a path, or the output
  of `security` or `secret-tool`.

## Nisi runs

- Inputs are file contents in the request, never paths. agiw-mcp does not read
  or write project files.
- Each run gets a fresh private temporary directory (mode 0700). The vendored
  Nisi files are copied into it and their bytes are checked against the pinned
  manifest (itself pinned by SHA-256) before Node starts. The host runner is
  copied separately and is not covered by that manifest. agiw-mcp
  attempts to remove the directory afterwards, also on failure, timeout, cancellation,
  stdin EOF and handled SIGTERM/SIGHUP; removal errors are ignored. The test suite checks selected project, home
  and config directories for changes.
- Node's version and permission probes (`node --version` and `node --help`)
  run with the inherited `PATH`, `LANG=C.UTF-8` and `NODE_NO_WARNINGS=1`.
  On Windows, `SystemRoot`, `SYSTEMDRIVE` and `WINDIR` are also passed when
  nonempty, with names matched case-insensitively. Keys, proxy settings and
  other environment variables are not passed to them.
  A failed permission probe (exception or nonzero exit) refuses the run with
  `NODE_UNAVAILABLE`; that failed result is not cached.
- If Node's help lists `--permission` or `--experimental-permission`, the
  runner uses that permission model with file reads and writes limited to the
  temporary directory. When both flags are listed, `--permission` is preferred.
  If neither flag is listed, the current implementation proceeds without
  permission flags and reports `sandbox: "none"`; it does not refuse unsupported
  permission models, and there is no refusal code for that case.
- A comma in the resolved temporary directory path refuses the run with
  `TEMP_DIR_UNSAFE`, since Node interprets file access allowlists as
  comma-separated paths. Use a `TMPDIR` without a comma.
- The runner uses a minimal environment: `PATH` (the Node directory and,
  on POSIX, `/usr/bin` and `/bin`), `HOME`, `TMPDIR`, `TMP` and `TEMP`
  (all pointing to the temporary directory), `LANG` (`C.UTF-8`) and
  `NODE_NO_WARNINGS` (`1`). On Windows, `SYSTEMROOT` and `WINDIR` are also
  set to their parent values when present. The runner also has a
  wall-clock limit and capped output. On timeout, agiw-mcp kills the process
  group on POSIX and only the runner process on Windows.
- Generated code is never executed. Static checks compile or parse without execution: `node --check`
  for JavaScript, Python's `compile` in an isolated interpreter (`-I -S -B`),
  and `JSON.parse`. These checker processes are started by the runner, which is
  why `--allow-child-process` is set; they are fixed commands with the
  candidate as data. When Node advertises `--allow-net`, that flag is also set;
  it permits network access generally. Loopback restrictions are enforced by
  application URL validation, not by Node's network permission flag.
- The model endpoint must be a loopback literal (`127.0.0.1` or `[::1]`) over
  HTTP, checked by agiw-mcp and again by Nisi's adapter. agiw-mcp cannot verify
  what the server behind that port does with the data.

## Threat model

In scope:

- An MCP caller trying to use the Jev tools without a recorded consent file,
  or to make agiw-mcp send the key or text somewhere other than TypeSafe.
- Leaking the key or local paths through error messages, logs or tool output.
- A Nisi run changing files outside its temporary directory.
- Malformed or hostile model output (oversized, invalid JSON, out-of-scope
  paths, code that would do harm if run).
- Malformed JSON-RPC input (oversized lines, invalid frames).

Out of scope:

- Same-user shell/file access that forges consent or ticket process-owner records.
- Cleanup of running children or temporary files after uncatchable termination (SIGKILL or power loss).
  Abandoned ticket locks now have bounded recovery as described below.
- A compromised local account, Node.js or Python installation, or a malicious
  local model server. Anyone who can run commands as you can read your
  Keychain items through the same tools.
- The correctness of model output. Review proposed contents before applying
  them.
- What TypeSafe does with data it receives; see TypeSafe's terms.

## Local records

`jev-activity.jsonl` in the config directory records, per Jev call:
time, tool, SHA-256 and byte count of your text, whether it was truncated, the
outcome code, HTTP status and number of flags. It never contains the text or
the key. New logs are created with mode 0600 on macOS and Linux; an existing
log keeps its mode. Nisi runs have no per-call log; ticket admission state is
stored separately. Admission and scope lock records include machine identity, PID, optional process
start identity and creation time. Recovery requires matching local machine/PID-namespace identity,
a minimum age of 60 seconds in both metadata and filesystem mtime, and a PID probe confirming
that the process does not exist. A recorded start identity is checked; a live or unverifiable PID
is never reclaimed, even after PID reuse. Foreign, legacy, partial or malformed records remain
fail-closed. Recovery serializes reclaimers with `flock` on the candidate inode and checks pathname
identity before unlinking through the verified directory descriptor. Symlinks, hardlinked candidates
and files owned by another user are not reclaimed. This relies on reliable POSIX advisory locking,
unique machine identities and the existing private-directory trust boundary; same-user manual
replacement of lock files during recovery is outside that boundary.

Successful recovery emits a `TICKET_LOCK_RECLAIMED` warning through Python logging (stderr by
default), with lock kind, PID, reason and age. It omits owner text, host identifiers and scope paths.
Recovery is demand-driven by a later admission, not a background service; incomplete lock writes
still require manual cleanup after all active runs have been ruled out.
