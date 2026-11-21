# Roadmap

The latest release is `v0.2.0`. Main reports version `0.2.0`; see [CHANGELOG.md](CHANGELOG.md) and the [v0.2.0 release notes](docs/release-notes/v0.2.0.md).

## Next release

- [x] Correct the public documentation found by the public review (#41), and the v0.1.0 release notes, repository description and homepage on GitHub.
- [x] Fix Nisi transport IPv6 loopback handling, enforce the 8 MiB response limit during receipt, and reject empty query and fragment delimiters in base URLs (#42).
- [x] Refuse runner endpoint ports outside 1-65535, matching configuration (#43).
- [x] Require a successful Node exit before accepting a Nisi report and fail closed if its temporary workspace cannot be removed (#85).
- [x] Refuse a non-regular, oversized or deeply nested ticket scope lock as `TICKET_REGISTRY_CORRUPT` (#46).
- [x] Refuse a Nisi run on a Node without a permission model unless `allow_unsandboxed_nisi` is set, and pin the consent disclosure hash in a test (lanes L1 and L2 of [docs/design/v0.2.md](docs/design/v0.2.md), #48).
- [x] Fail closed on deeply nested JSON from Jev, model servers, `config.json` and `consent.json`, and on a non-regular `config.json` or `consent.json` (#52).
- [x] Select the Python interpreter for the syntax check with the optional `check_python` key (lane L3 of [docs/design/v0.2.md](docs/design/v0.2.md), #53).
- [x] State the readiness permission-flag check in the `agiw_readiness` description and the Jev log rotation in SECURITY.md (#55).
- [x] Send optional progress notifications for `agiw_nisi_run` (lane L4 of [docs/design/v0.2.md](docs/design/v0.2.md), #56).
- [x] Harden the opt-in Nisi activity record against concurrent rotation, short writes, hard links and unsafe config directory handling; preserve a safe line for malformed nullable report metadata.
- [x] Answer protocol `2026-07-28` statelessly alongside the unchanged legacy handshake: gaps G1 to G6, G10 and G13 of [docs/design/protocol-2026-07-28.md](docs/design/protocol-2026-07-28.md) (#68). The modern path is taken only when `params._meta` carries the `io.modelcontextprotocol/protocolVersion` key (#71).
- [x] Vendor public Nisi 0.3.2 in place of 0.2.0, pinned by its manifest hash (#70).
- [x] Hold the opt-in Nisi activity record's lock with `flock` on macOS and Linux, so a killed writer no longer blocks later records, and mark a failed rollback with `nisi-activity.jsonl.damaged` (#72).
- [x] Test Python 3.14 in CI next to 3.10, 3.12 and 3.13 (#73).
- [ ] Resolve the two remaining public-review findings: commit metadata in published history, and the private scrub definitions listed under Deferred work. Both need an owner decision.
- [x] Assign a version and publish a reviewed release containing the changes on main (`v0.2.0`, GitHub only, not PyPI).

## Validation

CI runs unit tests on Linux, macOS and Windows. Separate jobs check built distributions and a git install. Their installed smoke exercises stateless `2026-07-28` discovery and tool listing over stdio. The git install also checks the legacy handshake and uses MCP Inspector to verify the six tool names. Windows ticket admission remains unsupported. See the [workflow](.github/workflows/ci.yml) and [README](README.md#development).

CI does not verify live TypeSafe requests or launch the supported MCP clients.

## Deferred work

- Move private scrub definitions out of the public scrub checker without weakening detection; the current checks are kept until then.
- Lane L6 of [docs/design/v0.2.md](docs/design/v0.2.md), the protocol `2026-07-28` gap list: G1 to G6, G10 and G13 are implemented (#68); the other gaps in [docs/design/protocol-2026-07-28.md](docs/design/protocol-2026-07-28.md) stay open. Lane L5, the local record of Nisi runs, is implemented as an opt-in `nisi_activity_log` key that is off by default; design decision D3 (on or off by default) is still open.
