# Roadmap

The latest release is `v0.1.0`. Main contains later fixes and still reports version `0.1.0`; see [CHANGELOG.md](CHANGELOG.md).

## Next release

- [x] Correct the public documentation found by the public review (#41), and the v0.1.0 release notes, repository description and homepage on GitHub.
- [x] Fix Nisi transport IPv6 loopback handling, enforce the 8 MiB response limit during receipt, and reject empty query and fragment delimiters in base URLs (#42).
- [x] Refuse runner endpoint ports outside 1-65535, matching configuration (#43).
- [x] Refuse a non-regular, oversized or deeply nested ticket scope lock as `TICKET_REGISTRY_CORRUPT` (#46).
- [x] Refuse a Nisi run on a Node without a permission model unless `allow_unsandboxed_nisi` is set, and pin the consent disclosure hash in a test (lanes L1 and L2 of [docs/design/v0.2.md](docs/design/v0.2.md), #48).
- [x] Fail closed on deeply nested JSON from Jev, model servers, `config.json` and `consent.json`, and on a non-regular `config.json` or `consent.json` (#52).
- [x] Select the Python interpreter for the syntax check with the optional `check_python` key (lane L3 of [docs/design/v0.2.md](docs/design/v0.2.md), #53).
- [x] State the readiness permission-flag check in the `agiw_readiness` description and the Jev log rotation in SECURITY.md (#55).
- [x] Send optional progress notifications for `agiw_nisi_run` (lane L4 of [docs/design/v0.2.md](docs/design/v0.2.md), #56).
- [ ] Resolve the two remaining public-review findings: commit metadata in published history, and the private scrub definitions listed under Deferred work. Both need an owner decision.
- [ ] Assign a version and publish a reviewed release containing the changes on main.

## Validation

CI runs unit tests on Linux, macOS and Windows. Separate jobs check built distributions and a git install, and MCP Inspector `tools/list` checks the six tool names. Windows ticket admission remains unsupported. See the [workflow](.github/workflows/ci.yml) and [README](README.md#development).

CI does not verify live TypeSafe requests or launch the supported MCP clients.

## Deferred work

- Move private scrub definitions out of the public scrub checker without weakening detection; the current checks are kept until then.
- Lanes L5 and L6 of [docs/design/v0.2.md](docs/design/v0.2.md), the local record of Nisi runs and the protocol `2026-07-28` gap list, are proposals only.
