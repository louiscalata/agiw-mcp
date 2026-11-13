# Roadmap

The latest release is `v0.1.0`. Main contains later fixes and still reports version `0.1.0`; see [CHANGELOG.md](CHANGELOG.md).

## Next release

- [ ] Resolve the outstanding public-review findings.
- [ ] Assign a version and publish a reviewed release containing the changes on main.

## Validation

CI runs unit tests on Linux, macOS and Windows. Separate jobs check built distributions and a git install, and MCP Inspector `tools/list` checks the six tool names. Windows ticket admission remains unsupported. See the [workflow](.github/workflows/ci.yml) and [README](README.md#development).

CI does not verify live TypeSafe requests or launch the supported MCP clients.

## Deferred work

- Move private scrub definitions out of the public scrub checker without weakening detection; the current checks are kept until then.
- Python target-interpreter selection.
- Progress notifications.
