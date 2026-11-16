# RPC serve golden corpus

`cases.json` records inputs, explicit schedules, and complete replay transcripts
for the production revision and source hash in `generated_from`. The transport
tests use the real reader with in-memory streams. Scheduling tests publish
inputs and worker actions in a fixed order; they check scheduling semantics
without claiming to reproduce every operating-system thread interleaving.

Each transcript includes output bytes, diagnostics, exceptions, dependency-call
order, cancellation flags, thread-local cleanup, and remaining queue contents.
Worker admission also records thread creation, so an unknown method carrying
the Nisi tool name cannot silently move from synchronous dispatch to a worker.
Cancellation cases distinguish integer zero from absent identifiers, invalid
boolean identifiers, and equal-valued boolean and integer identifiers.

From a Git checkout, check every expectation against a fresh export of the
pinned production revision:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3 -B \
  -m test_support.rpc_serve_golden.record
```

To add cases, give each input a unique name, group (`streams` or `scheduling`),
base64-encoded newline-delimited JSON input, and any schedule or fault options
supported by `support.py`. Then run the same command with `--write` and review
the resulting diff. The recorder captures expectations by execution; do not
hand-edit them. It checks the pinned RPC source hash and rejects unscripted
scheduling failures. It resolves the temporary export path before comparing
import identity, including when `TMPDIR` uses a symbolic link.

The recorder needs Git and the pinned revision in local history. Replaying the
tests from an unpacked source distribution needs neither. The source manifest
includes this corpus explicitly. A version or tool-schema change can alter
saved initialization and tool-list responses; update the revision and source
hash deliberately, then regenerate and review the complete transcript diff.

Run the two golden modules from the repository or source-distribution root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3 -B -m unittest \
  test_rpc_serve_golden test_rpc_serve_scheduling_golden -q
```
