# Roadmap

Canonical roadmap for this checkout. Base revision: `417db82`.

## Review remediation

- [x] Verify the three supplied reviews against source; consolidate duplicates.
- [x] Add regression tests and run them against the untouched base and edited source.
- [x] Fix Python compilation checks, ticket scope enforcement and receipt fingerprints.
- [x] Fix handled termination, Nisi cancellation/EOF and ping responsiveness.
- [x] Bound HTTP workers and shut down tracked sockets at the deadline.
- [x] Separate optional Jev readiness; fix CLI/config edge cases and accurate documentation.
- [x] Add installed wheel/sdist smoke and runner tests, plus CI lint.
- [ ] Run the 44 listening-fixture tests when the task's no-services/GET-only restriction permits them.
- [ ] Release-owner work: assign a version and publish a reviewed immutable release. No publication in this task.

## Deliberate follow-ups

- Remote PR metadata/privacy remediation is outside this checkout and task scope.
- [x] Add conservative stale ticket lock recovery after SIGKILL/power loss, with process-owner
  metadata, a 60-second age floor, local machine identity and PID checks, inode-serialized
  reclamation and redacted recovery receipts. Legacy or unverifiable records remain fail-closed.
- Optional Python target-interpreter selection and progress notifications are not added.
- Moving private scrub definitions to private configuration requires a replacement
  policy and CI configuration; the existing checks are preserved.
- Broad module extraction, duplicate-helper cleanup and repeated status probes are deferred.
- Product name expansion, pricing and additional client onboarding need authoritative inputs.

## Evidence

Local logs are under ignored `.venv/`: `all-regressions-before.log`,
`all-regressions-after.log`, `final-tests.log`, `build.log`,
`installed-wheel.log` and `installed-sdist.log`. No real model/provider calls were
made. Final discovery ran 217 tests:
173 passed and 44 listening-fixture tests were explicitly skipped. All 18 new
regression tests failed on the base (24 failures including subtests) and passed after the fixes. The vendored Nisi bytes
and manifest remain unchanged.

## Grok follow-up review

- [x] Drain buffered cancellation and EOF before starting pending RPC work; preserve FIFO call order.
- [x] Narrow the consent regression's exception handler to EOFError and KeyboardInterrupt.
  The claimed Ruff B036 failure did not reproduce: local Ruff 0.17.3 does not recognize B036,
  and both versions pass the configured lint check.
- [x] Report local HTTP worker exhaustion accurately in Jev and model readiness: nothing sent.
- [x] Align Nisi path validation with canonical ticket scope parts; document rejected names.
- [x] Demonstrate behavior regressions before/after; logs and the in-memory pre-fix replay
  script are in ignored `.venv/grok-review/`. The final 12-test focused run has 42 failures
  before the behavior fixes and passes after. This includes 36 rejected-path subtests.
- [x] Run the requested unmodified full-suite command: 226 tests, 182 passed and 44 errors.
  All 44 errors are sandbox PermissionError failures creating loopback listening fixtures;
  they are blocked tests, not passes or skips. No assertion failures remain.
- [x] Run `ruff check src tests scripts` and `git diff --check`: passed.

The earlier 217-test evidence above describes the prior
remediation run; this section records the subsequent Grok follow-up.

## Stale-lock recovery

- [x] Add same-host process-owner metadata to admission and scope locks, 60-second age checks,
  conservative PID/start-identity probes, atomic reclamation between cooperating reclaimers
  and redacted recovery log lines.
- [x] Add 17 recovery tests, including actual SIGKILL and competing-process admission,
  foreign/legacy/live-lock retention and replacement-inode protection. All 17 pass.
- [x] Run the requested full suite: 243 tests, 199 passed, 44 loopback-bind PermissionError
  errors imposed by the sandbox; zero assertion failures. These errors are blocked tests.
- [ ] Run the full suite with loopback listening permitted and validate on native Linux.

External review for this recovery run was unavailable: Claude
CLI was logged out, and the fallback worker status tool required unavailable approval.
