# Changelog

## Unreleased (based on 417db82)

- Compile Python candidates without executing them; reject invalid module control flow.
- Enforce ticket file scopes and retain the declared owner in scope locks.
- Bind host inputs and final transport bodies to explicit receipt hashes.
- Cancel Nisi on request cancellation or stdin EOF; keep ping responsive during Nisi runs.
- Clean up runners, temporary directories and tickets on handled termination signals.
- Close tracked HTTP sockets on deadlines and cap outstanding HTTP workers.
- Report local readiness independently of optional Jev setup.
- Correct consent, validation and platform documentation. The corrected disclosure
  changes its hash, so previous Jev consent must be granted again.
- Add installed wheel/sdist checks and Ruff to CI.

The package version remains 0.1.0 pending release-owner assignment. No tag or
published release is implied by this entry.
