# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
import json
import os
import unittest
from unittest import mock

from support import IsolatedTestCase, NetworkBlocked

from agiw_mcp import config, consent, keys, net, tools
from agiw_mcp.config import ConfigError, load_config
from agiw_mcp.errors import ToolError

FAKE_KEY = "tsk_fake_" + "0123456789abcdef"


class KeyTests(IsolatedTestCase):
    def fake_tool(self, name, script):
        path = self.tmp / "bin" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + script, encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def use_keychain(self, script, platform="darwin"):
        os.environ["AGIW_MCP_TEST_OS_KEYSTORE"] = "1"
        fake = self.fake_tool("security" if platform == "darwin" else "secret-tool", script)
        patches = [mock.patch.object(keys, "_platform", return_value=platform)]
        if platform == "darwin":
            patches.append(mock.patch.object(keys, "SECURITY_BIN", fake))
        else:
            patches.append(mock.patch.object(keys, "SECRET_TOOL_CANDIDATES", (fake,)))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return fake

    def test_environment_key_wins(self):
        os.environ["TYPESAFE_API_KEY"] = FAKE_KEY
        self.assertEqual(keys.key_presence(load_config()), {"present": True, "source": "environment"})
        self.assertEqual(keys.read_key(load_config()), FAKE_KEY)

    def test_test_mode_never_touches_os_keystores(self):
        with mock.patch.object(keys.subprocess, "run", side_effect=AssertionError("ran a keystore tool")):
            self.assertFalse(keys.key_presence(load_config())["present"])
            with self.assertRaises(keys.KeyUnavailable):
                keys.read_key(load_config())

    @unittest.skipIf(os.name != "posix", "uses /bin/sh fakes")
    def test_macos_presence_does_not_request_the_secret(self):
        log = self.tmp / "argv.txt"
        self.use_keychain(f'echo "$@" >> "{log}"\nexit 0\n')
        self.assertTrue(keys.key_presence(load_config())["present"])
        argv = log.read_text().strip()
        self.assertIn("find-generic-password -s agiw-mcp -a typesafe", argv)
        self.assertNotIn("-w", argv.split())

    @unittest.skipIf(os.name != "posix", "uses /bin/sh fakes")
    def test_macos_read_key_and_missing_item(self):
        self.use_keychain(f'printf "%s\\n" "{FAKE_KEY}"\n')
        self.assertEqual(keys.read_key(load_config()), FAKE_KEY)

    @unittest.skipIf(os.name != "posix", "uses /bin/sh fakes")
    def test_keychain_failure_is_a_fixed_message_without_output(self):
        self.use_keychain('echo "security: SecKeychainSearchCopyNext: item not found /Users/someone" >&2\n'
                          'echo "partial-secret-material"\nexit 44\n')
        with self.assertRaises(keys.KeyUnavailable) as ctx:
            keys.read_key(load_config())
        self.assertEqual(str(ctx.exception), keys.KEY_UNAVAILABLE_MESSAGE)
        self.assertFalse(keys.key_presence(load_config())["present"])

    @unittest.skipIf(os.name != "posix", "uses /bin/sh fakes")
    def test_malformed_stored_key_is_refused_without_echo(self):
        self.use_keychain('printf "two words\\n"\n')
        with self.assertRaises(keys.KeyUnavailable) as ctx:
            keys.read_key(load_config())
        self.assertNotIn("two words", str(ctx.exception))

    @unittest.skipIf(os.name != "posix", "uses /bin/sh fakes")
    def test_linux_secret_tool_lookup(self):
        log = self.tmp / "argv.txt"
        self.use_keychain(f'echo "$@" >> "{log}"\nprintf "%s" "{FAKE_KEY}"\n', platform="linux")
        self.assertTrue(keys.key_presence(load_config())["present"])
        self.assertEqual(keys.read_key(load_config()), FAKE_KEY)
        self.assertIn("lookup service agiw-mcp account typesafe", log.read_text())

    def test_custom_service_and_account_from_config(self):
        self.write_config(keychain_service="my-service", keychain_account="me")
        cfg = load_config()
        self.assertEqual((cfg.keychain_service, cfg.keychain_account), ("my-service", "me"))


class ConsentTests(IsolatedTestCase):
    def test_show_describes_exactly_what_is_sent(self):
        info = consent.show()
        self.assertFalse(info["granted"])
        text = info["disclosure"]
        for needle in ("https://api.typesafe.ai/v1/systemone", "12288 bytes", "Authorization header",
                       "harm_if_wrong", "self_contradiction", "advisory, not a verdict"):
            self.assertIn(needle, text)
        self.assertNotIn(str(self.tmp), text)
        self.assertTrue(info["consentFile"].endswith("consent.json"))
        json.dumps(info)  # serializable for the tool layer

    def test_grant_requires_current_disclosure_hash(self):
        with self.assertRaises(ToolError) as ctx:
            consent.grant("0" * 64)
        self.assertEqual(ctx.exception.code, "DISCLOSURE_MISMATCH")
        self.assertFalse(consent.consent_recorded())
        consent.grant(consent.disclosure_sha256())
        self.assertTrue(consent.consent_recorded())

    @unittest.skipIf(os.name != "posix", "file modes")
    def test_consent_file_is_private(self):
        consent.grant(consent.disclosure_sha256())
        path = self.config_dir / consent.CONSENT_FILE
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config_dir.stat().st_mode & 0o777, 0o700)

    def test_revoke_and_stale_disclosure(self):
        consent.grant(consent.disclosure_sha256())
        consent.revoke()
        self.assertFalse(consent.consent_recorded())
        consent.grant(consent.disclosure_sha256())
        with mock.patch.object(consent, "disclosure_sha256", return_value="f" * 64):
            self.assertFalse(consent.consent_recorded())

    def test_consent_tool_actions(self):
        shown, error = tools.call_tool("agiw_consent", {"action": "show"})
        self.assertFalse(error)
        schema = next(tool for tool in tools.TOOLS if tool["name"] == "agiw_consent")["inputSchema"]
        self.assertEqual(schema["properties"]["action"]["enum"], ["show", "revoke"])
        for arguments in ({"action": "grant"},
                          {"action": "grant", "disclosure_sha256": shown["disclosureSha256"]},
                          {"action": "GRANT"}, {"action": "allow"}, {"action": "show", "granted": True}):
            refused, error = tools.call_tool("agiw_consent", arguments)
            self.assertTrue(error, arguments)
            self.assertFalse(consent.consent_recorded(), arguments)
        consent.grant(consent.disclosure_sha256())  # the terminal route
        self.assertTrue(consent.consent_recorded())
        revoked, error = tools.call_tool("agiw_consent", {"action": "revoke"})
        self.assertFalse(error)
        self.assertFalse(revoked["granted"])
        self.assertFalse(consent.consent_recorded())


class ConfigTests(IsolatedTestCase):
    def test_defaults(self):
        cfg = load_config()
        self.assertEqual(cfg.model_base_url, "http://127.0.0.1:1234/v1")
        self.assertEqual(cfg.chat_endpoint, "http://127.0.0.1:1234/v1/chat/completions")
        self.assertIsNone(cfg.author_model)

    def test_rejects_non_loopback_and_unknown_keys(self):
        for url in ("http://192.0.2.10:1234/v1", "https://127.0.0.1/v1", "http://localhost:1234/v1",
                    "http://user:pw@127.0.0.1/v1", "http://127.0.0.1/v1?x=1"):
            self.write_config(model_base_url=url)
            with self.assertRaises(ConfigError, msg=url):
                load_config()
        self.write_config(unknown_key=1)
        with self.assertRaises(ConfigError):
            load_config()

    def test_environment_overrides(self):
        self.write_config(author_model="a")
        os.environ["AGIW_MCP_REVIEWER_MODEL"] = "b"
        os.environ["AGIW_MCP_MODEL_BASE_URL"] = "http://[::1]:8080/v1/"
        cfg = load_config()
        self.assertEqual((cfg.author_model, cfg.reviewer_model), ("a", "b"))
        self.assertEqual(cfg.model_base_url, "http://[::1]:8080/v1")

    def test_display_path_hides_home(self):
        home = str(config.Path.home())
        self.assertEqual(config.display_path(home + "/x/y"), "~/x/y")
        self.assertEqual(config.display_path("/opt/tool"), "/opt/tool")

    def test_per_os_config_dir(self):
        del os.environ["AGIW_MCP_CONFIG_DIR"]
        with mock.patch.object(config.sys, "platform", "darwin"):
            self.assertTrue(str(config.config_dir()).endswith("Library/Application Support/agiw-mcp"))
        with mock.patch.object(config.sys, "platform", "linux"), mock.patch.object(config.os, "name", "posix"):
            os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "xdg")
            self.assertEqual(config.config_dir(), self.tmp / "xdg" / "agiw-mcp")


class NetworkGuardTests(IsolatedTestCase):
    def test_code_guard_refuses_non_loopback_in_test_mode(self):
        with self.assertRaises(net.NetworkRefused):
            net.guard("https://api.typesafe.ai/v1/systemone")
        net.guard("http://127.0.0.1:9/v1/models")

    def test_socket_guard_blocks_external_addresses(self):
        import socket
        with self.assertRaises(NetworkBlocked):
            socket.create_connection(("192.0.2.1", 443), timeout=0.1)


if __name__ == "__main__":
    unittest.main()
