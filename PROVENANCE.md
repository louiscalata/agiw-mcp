# Provenance

Every file in this repository was written for AGIW MCP, except the files listed
below. Those were copied byte-for-byte from the public AGIW Suite repository
(https://github.com/louiscalata/agiw-suite), branch `edition-parity`, at commit
`5d52621` (full hash `5d526216d2aaab1f0db1d4f2a9a9f06592cbb096`, 2026-10-19).
Each is licensed under Apache-2.0 (SPDX: `Apache-2.0`) by the same copyright
holder, Louis Calata.

The vendored Nisi files are themselves the public Nisi v0.2.0 release
(https://github.com/louiscalata/nisi/releases/tag/v0.2.0, source commit
`41fb6aeb67713011abc40b1434f5a939a5704aa4`), as pinned by
`src/agiw_mcp/vendor/nisi/manifest.json`. agiw-mcp checks the SHA-256 of that
manifest (`d7cd9f31365ad47f01ccfbfaf2cf29923830abf642c60518cbeb1b9776d8cd23`)
and of every listed file before each run.

| File in this repository | Source path in agiw-suite @ 5d52621 | SPDX | SHA-256 |
|---|---|---|---|
| `LICENSE` | `LICENSE` | Apache-2.0 | `c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4` |
| `src/agiw_mcp/vendor/nisi/manifest.json` | `vendor/nisi/manifest.json` | Apache-2.0 | `d7cd9f31365ad47f01ccfbfaf2cf29923830abf642c60518cbeb1b9776d8cd23` |
| `src/agiw_mcp/vendor/nisi/package/LICENSE` | `vendor/nisi/package/LICENSE` | Apache-2.0 | `c95bae1d1ce0235ecccd3560b772ec1efb97f348a79f0fbe0a634f0c2ccefe2c` |
| `src/agiw_mcp/vendor/nisi/package/README.md` | `vendor/nisi/package/README.md` | Apache-2.0 | `9db0e88aca6f53847e05bc0f19165309eda0e185b691a1a500a957b4da3fa62e` |
| `src/agiw_mcp/vendor/nisi/package/adapters/apple-foundation-models.mjs` | `vendor/nisi/package/adapters/apple-foundation-models.mjs` | Apache-2.0 | `311f4898c31b4e0693093361fc19c344086c1f24946909fe134c14d37e25d080` |
| `src/agiw_mcp/vendor/nisi/package/adapters/local-chat.mjs` | `vendor/nisi/package/adapters/local-chat.mjs` | Apache-2.0 | `75dc8b128883fb14e524ceb05b4696e53406c2c8038ec4340b42f0ca471fe043` |
| `src/agiw_mcp/vendor/nisi/package/bin/nisi.mjs` | `vendor/nisi/package/bin/nisi.mjs` | Apache-2.0 | `56d758021043684a0d6ff35c443a57dea5de3e11828fff3afa61cc476479198f` |
| `src/agiw_mcp/vendor/nisi/package/canonical/canonical-json-v1.mjs` | `vendor/nisi/package/canonical/canonical-json-v1.mjs` | Apache-2.0 | `d1049a26d2bea0d14e959c0af22af15b975345091c5fa5360e8bd685637f8e59` |
| `src/agiw_mcp/vendor/nisi/package/examples/local-model-workflow.mjs` | `vendor/nisi/package/examples/local-model-workflow.mjs` | Apache-2.0 | `10bebdef922bb63c3716baa107cd037d3de6432b93a2e058e22151578738a2c1` |
| `src/agiw_mcp/vendor/nisi/package/examples/workflow.mjs` | `vendor/nisi/package/examples/workflow.mjs` | Apache-2.0 | `0d365578db02cb9aeda0aaa500aef61cad506e1dfdd0ce36a76235781d9795fd` |
| `src/agiw_mcp/vendor/nisi/package/gate/afm-content-executor.mjs` | `vendor/nisi/package/gate/afm-content-executor.mjs` | Apache-2.0 | `6a4c23a3495a00dbf98e9ff5c68a9008f421d6fe225a3768b6863d4e12324ed6` |
| `src/agiw_mcp/vendor/nisi/package/gate/content-consent.mjs` | `vendor/nisi/package/gate/content-consent.mjs` | Apache-2.0 | `a735eb35cb1373b3f3744dc7b3caa8b24ebbf394babba53d4f722136c72a0820` |
| `src/agiw_mcp/vendor/nisi/package/history/run-journal-store-v1.mjs` | `vendor/nisi/package/history/run-journal-store-v1.mjs` | Apache-2.0 | `772bee640e246d14da4bfe6d6d52e62f750436aa5c4caf1457eeef703f058eaf` |
| `src/agiw_mcp/vendor/nisi/package/history/run-journal-v1.mjs` | `vendor/nisi/package/history/run-journal-v1.mjs` | Apache-2.0 | `ab51ddd2ca2e3e1dcc24560991b1ffd703d015f584882613731a58814bc5db3b` |
| `src/agiw_mcp/vendor/nisi/package/index.mjs` | `vendor/nisi/package/index.mjs` | Apache-2.0 | `066c90d65209854cd6aeb838924ce28dca90d7dc75d0cd533d17fbba8e1c3db7` |
| `src/agiw_mcp/vendor/nisi/package/package.json` | `vendor/nisi/package/package.json` | Apache-2.0 | `f00a8fa85371b4f3146bf8f2ad2d02ad6fd4974a1a5e0080d6294af03d9205c4` |
| `src/agiw_mcp/vendor/nisi/package/policy/file-access.mjs` | `vendor/nisi/package/policy/file-access.mjs` | Apache-2.0 | `92f9f60220e1869ad0d6571ae2b5a8ddc9259b2463c4cf617d4cecee6eacc1d3` |
| `src/agiw_mcp/vendor/nisi/package/serialization/canonical-json-v1.mjs` | `vendor/nisi/package/serialization/canonical-json-v1.mjs` | Apache-2.0 | `1e5eb6222da2c7baae3cd45a5077ccc1e8ae25b5bc184646d9e55170dec53625` |
| `src/agiw_mcp/vendor/nisi/package/workflow/contracts.mjs` | `vendor/nisi/package/workflow/contracts.mjs` | Apache-2.0 | `c28a3ee3db6e9a936cd58fca8b8ee3f2744ca25c259e2ea278b3b09cdbb73d82` |
| `src/agiw_mcp/vendor/nisi/package/workflow/engine.mjs` | `vendor/nisi/package/workflow/engine.mjs` | Apache-2.0 | `c67c63baeadc725a8487af7108b01d3a38dd50e64ff35f1be66471c81e5f979b` |

## Referenced, not copied

These sources were read to match existing behaviour. No text or code from them
is included; the corresponding modules here are new implementations.

- agiw-suite `nisi_v02.py` and `bundle_nisi.py` (bounded child processes,
  manifest verification): see `src/agiw_mcp/nisi.py`.
- agiw-suite `jev_connection.py` and `JevKeychain.swift` (Keychain item
  convention, consent wording): see `src/agiw_mcp/keys.py` and
  `src/agiw_mcp/consent.py`.
- The Jev request shape (`POST /v1/systemone`, `model`, `state`, `questions`,
  `noul` and `score` answers) and the topics of the fixed pre/post questions
  come from the owner's own tooling; the questions in
  `src/agiw_mcp/questions.py` are reworded.
- Nisi's public documentation (workflow API, local model adapters).

`NOTICE` is new; it is not a copy of agiw-suite's NOTICE.
