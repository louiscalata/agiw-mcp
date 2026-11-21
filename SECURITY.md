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

## MCP security practices

This table maps selected [MCP security best practices](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices)
to this local stdio server. OAuth and remote-session features named below are not
implemented here. The table does not cover every practice in that guide.

| Practice | Applicability here | Implementation and limits |
|---|---|---|
| Confused deputy through an OAuth proxy | OAuth proxy absent | There is no delegated OAuth flow or remote MCP HTTP endpoint. The process speaks JSON-RPC on stdio ([rpc.py](src/agiw_mcp/rpc.py)). |
| Token passthrough | MCP bearer tokens absent | The server does not accept an MCP access token and forward it. The separate TypeSafe key is read after recorded consent and sent to the fixed Jev endpoint ([keys.py](src/agiw_mcp/keys.py), [jev.py](src/agiw_mcp/jev.py)); [Jev tests](tests/test_jev.py) check key redaction and refused redirects. |
| Server-side request forgery | Configured model requests | Author and reviewer base URLs must use HTTP and a loopback literal, with optional port and base path ([config.py](src/agiw_mcp/config.py)); other model URLs are refused. Jev uses the fixed URL in [consent.py](src/agiw_mcp/consent.py). Python HTTP requests bypass proxies and refuse redirects ([net.py](src/agiw_mcp/net.py)). Loopback does not authenticate the service. [Configuration tests](tests/test_keys_consent_config.py) cover both model URL settings. |
| Session or state-handle hijacking | Remote MCP session mechanism absent | The stdio transport has no `Mcp-Session-Id` ([rpc.py](src/agiw_mcp/rpc.py)). A claim ticket is caller-supplied cooperative exclusion, not authentication ([tickets.py](src/agiw_mcp/tickets.py)). |
| Local MCP server compromise | Applies | The server runs as its invoking user. Nisi returns proposed contents without writing the project ([nisi.py](src/agiw_mcp/nisi.py)). Same-user config write access can forge consent; the stored record is not OS user presence. The MCP tool refuses grants ([tools.py](src/agiw_mcp/tools.py), [consent tests](tests/test_keys_consent_config.py)). |
| Scope minimization | OAuth scopes absent | There is no OAuth scope list. Six fixed tools are exposed ([tools.py](src/agiw_mcp/tools.py)). When a claim ticket is supplied, proposed paths must fit its `writeScope`; a ticket is required only when configured ([ticket tests](tests/test_tickets.py)). Jev still requires recorded consent. |

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
- Each run gets a fresh private temporary directory (mode 0700 on macOS and Linux).
  The vendored Nisi files are copied into it and their bytes are checked against the pinned
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
  If neither flag is listed, the run is refused with
  `NODE_SANDBOX_UNAVAILABLE`. Setting `allow_unsandboxed_nisi` to `true` in
  `config.json` turns that refusal off: the runner then starts without
  permission flags, has no file-access limit and reports `sandbox: "none"`.
  A "neither flag" result is not cached, so each run asks Node again. With
  the key set, such a run asks `node --help` twice: before the temporary
  directory is created and again when the command line is built. A failure of
  the second probe refuses with `NODE_UNAVAILABLE` and the directory is removed.
- The permission check runs after ticket admission and before the temporary
  directory is created, so ticket refusals come first and involve no Node
  process. A ticketed run refused by this check has created its ticket lock
  files and removes them before returning. The per-install `identity.key` in
  the ticket directory stays, as after any admitted run. A second check on the final
  command line refuses a runner without permission flags unless the key is
  true.
- Optional MCP progress is emitted only when `agiw_nisi_run` receives a
  string or integer `_meta.progressToken`; booleans and other values are
  ignored, as are tokens on other tools. The host validates runner events
  against a closed set of fields and stage/status values, then composes the
  notification message itself. No model text, task text, path or model id is
  forwarded. Events are capped at 64 per run; queue-full events may be
  dropped. Every runner stdout byte, including event and malformed lines,
  counts toward the existing 16 MiB output limit. No progress token means no
  progress notifications or change to the existing MCP stdout frames.
  Progress is transient and creates no local record; no progress is sent
  after cancellation or the final result.
- The optional `check_python` setting selects the interpreter used for Python
  syntax checks. The path is validated as an absolute config string, then
  probed after ticket admission and before temporary-directory creation with
  fixed `-I -S -B` flags, a fixed version script, a minimal environment and a
  3 second limit. The probe accepts only a strict `X.Y.Z` version and adds no
  minimum-version gate. No minimum Python version is enforced for the
  selected interpreter; the checker is tested on Python 3.12 and newer. A failed
  probe returns `CHECK_PYTHON_UNAVAILABLE` with a fixed path-free message;
  ticket locks are released. `check_python` is config-only, not an environment
  override or tool argument. Its version is included in `pythonCheckerVersion`
  and the input fingerprint; its path is excluded from the fingerprint and
  from refusal text. `agiw_status` displays the path with `display_path` and
  reports the probed version. Readiness adds `syntax_python` after the Nisi
  check only when configured.
- The Python checker runs outside Node's permission model, just as the
  syntax-check child did before this option. The selected binary and the
  server's launch environment are trusted inputs: a same-user config writer
  or whoever controls the launch environment can select a different config
  and interpreter. The isolated flags, fixed probe, minimal environment and
  timeout limit probe behavior; they do not make a compromised interpreter
  safe.
- `allow_unsandboxed_nisi` is read only from `config.json`. No environment
  variable sets it and it is not a tool argument, so an MCP tool caller cannot
  set it. It sits behind the same boundary as the consent record, which has two
  parts: same-user write access to the config directory, and the server's
  launch environment. `AGIW_MCP_CONFIG_DIR` selects the config directory;
  otherwise `APPDATA` on Windows, `XDG_CONFIG_HOME` on other non-macOS systems,
  or the home directory decide it. Whoever sets that environment, for example
  in the `env` block of the client file that starts the server (for Cursor
  that can be a file inside the project), can point the server at a directory
  they control and so choose a `config.json` that sets the key, and a
  `consent.json`, without writing the user's config directory.
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
- Configuration and the Nisi HTTP transport accept only `127.0.0.1` and `[::1]`
  loopback literals over HTTP. Credentials, queries and fragments are refused,
  including an empty trailing `?` or `#`. The Nisi runner checks the endpoint
  text before URL parsing, so other spellings that a URL parser would turn into
  a loopback address (decimal, hexadecimal, octal or shortened IPv4, a trailing
  dot, longer IPv6 forms, `localhost`) are refused. Redirects are not followed.
  agiw-mcp cannot verify what the server behind that port does with the data.
- The host HTTP transport counts model response bytes before retaining each chunk.
  It aborts the request when a chunk would take the body above 8 MiB and refuses
  the response with `LOCAL_CHAT_RESPONSE_TOO_LARGE`.

## Threat model

In scope:

- An MCP caller trying to use the Jev tools without a recorded consent file,
  or to make agiw-mcp send the key or text somewhere other than TypeSafe.
- Leaking the key or local paths through error messages, logs or tool output.
- A Nisi run changing files outside its temporary directory. This rests on
  Node's permission model. A Node without one is refused by default; with
  `allow_unsandboxed_nisi` set to `true` nothing limits the runner's file
  access.
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
time, tool, SHA-256 and byte count of your text, the JSON request body's SHA-256
(`requestSha256`), the text's byte count after truncation (`sentBytes`), whether it
was truncated, the outcome code, HTTP status, number of flags and elapsed
milliseconds (`elapsedMs`). It never contains the text or the key. New logs are
created with mode 0600 on macOS and Linux; an existing log keeps its mode.
The log is rotated when it exceeds 1 MiB: the next write renames it to
`jev-activity.jsonl.1` (keeping its mode), replacing any earlier `.1`, and starts a
new log, so only the current and one previous file are kept. Rotation and append
hold the same bounded exclusive lock. On macOS and Linux, the persistent
`jev-activity.jsonl.lock` uses `flock`, so the kernel releases the lock when a
writer exits. Without `fcntl`, an exclusive-create lock file is removed after
the write; an interrupted writer can leave it behind for manual removal after
all writers have stopped. Lock contention or an unusable lock skips that record
without failing the Jev call.

`nisi-activity.jsonl` is a second, optional record. It is off by default and
is written only when `config.json` sets `nisi_activity_log` to `true`; no
environment variable or tool argument turns it on. When on, each
`agiw_nisi_run` call that passes input validation attempts to add one line: time,
`outcome` (the Nisi report outcome, or the refusal code), `inputFingerprint`
and `candidateFingerprint` (SHA-256 hex, or null), `repairAttempts` (or null),
`sandbox` (`node-permission`, `none` or null), `ticketed` (true or false),
`nisiVersion` and `elapsedMs`. A call refused before that point (unknown or
invalid arguments, `TICKET_REQUIRED` or `TICKET_INVALID`) adds nothing. Fields are allowlisted
and typed, so the file never holds task text, file contents or paths, a ticket
id or owner, model ids or endpoints, or a key. Caveat: `inputFingerprint` is a
hash over the request, so someone who can read the file and can guess the exact
inputs can confirm the guess; `inputSha256` in the Jev log has the same
property. Invalid repair counts (anything other than an integer from 0 through
1000, excluding booleans) and unrecognized sandbox labels are recorded as
null. The file is created with mode 0600 on macOS and Linux; an existing file
keeps its mode. The record refuses a symlinked config directory and refuses a
symlinked, hard-linked or non-regular log, checking the opened file as well as
the path. Existing config directory permissions are preserved, including modes
that deny writing. On macOS and Linux, directory-relative operations anchor
the writes to the opened config directory. On platforms without those
operations, checks use paths and cannot prevent a same-user path replacement
between check and use; native Windows race behavior has not been verified.

The log is rotated when it exceeds 1 MiB: the next write renames it to
`nisi-activity.jsonl.1`, replacing any earlier `.1`, so the current log and
one previous log are retained. Rotation and append hold the same exclusive
lock, and size is checked after acquiring it. On macOS and Linux the lock is
`flock` on `nisi-activity.jsonl.lock`, opened relative to the config directory
without following a symlink and only as a single-link regular file. That file
is empty, mode 0600, and never removed; the kernel releases the lock when its
holder closes it or exits, so a killed writer does not block later records.
Without `fcntl` (native Windows), the lock is an exclusive-create
`nisi-activity.jsonl.lock` that the writer removes after use; a process
interrupted while holding it can leave it behind, and it must be removed
manually only when no record writers are active. Lock waiting is bounded
(about 100 ms); a busy or unusable lock skips the record. A short write is
rolled back to the previous file size. If rollback also fails, the writer
creates the empty marker `nisi-activity.jsonl.damaged` (mode 0600) and every
later call skips the record while it exists. To recover, stop record writers,
inspect and repair or remove `nisi-activity.jsonl`, then delete the marker. If
the fallback cannot create the marker, it leaves its lock file in place
instead. Each skipped record (busy lock, damaged marker, or write failure)
prints one line to stderr, `agiw-mcp: nisi activity record skipped (locked)`,
`(damaged)` or `(error)`, with no request content, paths or fingerprints. A
failed write or lock acquisition never fails the run. A later ordinary rotation still
discards the older backup; this is bounded retention, not a permanent run
archive. The server has no tool or command that shows the record; delete the
config directory or these files to remove the record. Anyone who can write the
config directory or choose the server's launch environment can switch the
record on or off, as for the other `config.json` keys.

Ticket admission state is
stored separately. Each scope lock record also holds, in plain text, the canonical scope, the
ticket id and the ticket owner text, inside the mode 0700 ticket state directory; refusals do not
repeat them. A scope lock that is not a regular file, is larger than 16 KiB, or does not parse is
refused as `TICKET_REGISTRY_CORRUPT`. Admission and scope lock records include an HMAC-SHA256 of the machine identity,
PID, an optional HMAC-SHA256 of the process start identity, and creation time. The machine identity is
Linux `/etc/machine-id` plus the PID namespace, or the macOS host UUID from `gethostuuid`. The start
identity is the Linux boot id plus the process start time from `/proc/<pid>/stat`, or the macOS
process start time. Neither raw value is written. The HMAC key is 32 random bytes stored in the
ticket state directory as `identity.key`, created on first use with mode 0600. It is never logged
and must not be committed. The same install reuses that file, so the same machine and the same
process start still compare equal. A different key produces a different digest, which stops a lock
from being linked to another install or confirming a guessed machine id or boot id. The HMAC message
is prefixed with `agiw-mcp.lock-identity.v1` only to keep a machine digest distinct from a boot
digest; that prefix is not a secret. A key file that is missing, the wrong size, linked elsewhere,
or not mode 0600 is ignored, and recovery stays fail-closed.
A lock written by an older version, which stored those values in the clear, does not match and is
not reclaimed. A lock written with the unkeyed SHA-256 of that public prefix also does not match
and is not reclaimed. Remove either by hand only after the owning process is confirmed dead.
Recovery requires matching the local machine/PID-namespace HMAC,
a minimum age of 60 seconds in both metadata and filesystem mtime, and a PID probe confirming
that the process does not exist. A recorded start identity is checked; a live or unverifiable PID
is never reclaimed, even after PID reuse. Foreign, legacy, partial or malformed records remain
fail-closed. Recovery serializes reclaimers with `flock` on the candidate inode and checks pathname
identity before unlinking through the verified directory descriptor. Symlinks, hardlinked candidates
and files owned by another user are not reclaimed. This relies on reliable POSIX advisory locking,
unique machine identities, the identity key staying mode 0600, and the existing private-directory
trust boundary; same-user manual replacement of lock files during recovery is outside that boundary.

Successful recovery emits a `TICKET_LOCK_RECLAIMED` warning through Python logging (stderr by
default), with lock kind, PID, reason and age. It omits owner text, host identifiers, the identity
key and scope paths.
Recovery is demand-driven by a later admission, not a background service; incomplete lock writes
still require manual cleanup after all active runs have been ruled out.
