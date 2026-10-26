# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
import json
import os
import subprocess
import unittest
from unittest import mock

from support import IsolatedTestCase, NetworkBlocked

from agiw_mcp import config, consent, keys, net, tools
from agiw_mcp.config import ConfigError, load_config
from agiw_mcp.errors import ToolError

FAKE_KEY = "tsk_fake_" + "0123456789abcdef"


class KeyTests(IsolatedTestCase):
    def test_clean_trims_one_crlf_or_lf(self):
        for ending in (b"", b"\n", b"\r\n"):
            with self.subTest(ending=ending):
                self.assertEqual(keys._clean(FAKE_KEY.encode() + ending), FAKE_KEY)
        for raw in (FAKE_KEY.encode() + b"\n\n", FAKE_KEY.encode() + b"\r", b"\xff"):
            with self.subTest(raw=raw), self.assertRaises(keys.KeyUnavailable) as ctx:
                keys._clean(raw)
            self.assertEqual(str(ctx.exception), keys.KEY_UNAVAILABLE_MESSAGE)

    def test_run_read_refuses_non_utf8_and_oversized_output(self):
        for raw in (b"\xff", b"x" * 4097):
            done = subprocess.CompletedProcess(["fake-keystore"], 0, stdout=raw)
            with self.subTest(size=len(raw)), mock.patch.object(keys.subprocess, "run", return_value=done), \
                    self.assertRaises(keys.KeyUnavailable) as ctx:
                keys._run_read(["fake-keystore"])
            self.assertEqual(str(ctx.exception), keys.KEY_UNAVAILABLE_MESSAGE)

    def test_run_read_trims_line_endings(self):
        for ending in (b"\n", b"\r\n"):
            done = subprocess.CompletedProcess(["fake-keystore"], 0, stdout=FAKE_KEY.encode() + ending)
            with self.subTest(ending=ending), mock.patch.object(keys.subprocess, "run", return_value=done):
                self.assertEqual(keys._run_read(["fake-keystore"]), FAKE_KEY)

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
    def test_config_file_parse_errors(self):
        path = self.config_dir / "config.json"
        path.parent.mkdir()
        for raw in (b" " * (config.MAX_CONFIG_BYTES + 1), b"\xff", b"{broken", b"[]",
                    b'{"author_model":"a","author_model":"b"}'):
            with self.subTest(raw=raw[:80]):
                path.write_bytes(raw)
                with self.assertRaises(ConfigError) as ctx:
                    load_config()
                self.assertEqual(ctx.exception.code, "CONFIG_INVALID")
        path.write_bytes(b"{}" + b" " * (config.MAX_CONFIG_BYTES - 2))
        self.assertEqual(load_config().source, "defaults")

    def test_config_unreadable(self):
        with mock.patch("builtins.open", side_effect=PermissionError("private path")), \
                self.assertRaises(ConfigError) as ctx:
            load_config()
        self.assertEqual(ctx.exception.code, "CONFIG_UNREADABLE")
        self.assertNotIn("private path", ctx.exception.message)

    def test_base_url_precedence(self):
        self.assertEqual(load_config().model_base_url, config.DEFAULT_BASE_URL)
        self.write_config(model_base_url="http://127.0.0.1:1235/v1")
        self.assertEqual(load_config().model_base_url, "http://127.0.0.1:1235/v1")
        os.environ["AGIW_MCP_MODEL_BASE_URL"] = "http://127.0.0.1:2345/v1"
        self.assertEqual(load_config().model_base_url, "http://127.0.0.1:2345/v1")
        os.environ["AGIW_MCP_BASE_URL"] = "http://[::1]:3456/v1/"
        os.environ["AGIW_MCP_AUTHOR_MODEL"] = "environment-author"
        cfg = load_config()
        self.assertEqual(cfg.model_base_url, "http://[::1]:3456/v1")
        self.assertEqual(cfg.source, "file+env")
        del os.environ["AGIW_MCP_MODEL_BASE_URL"]
        self.assertEqual(load_config().model_base_url, "http://[::1]:3456/v1")
        (self.config_dir / "config.json").unlink()
        self.assertEqual(load_config().source, "env")
        self.assertEqual(load_config().model_base_url, "http://[::1]:3456/v1")

    def test_environment_base_urls_keep_loopback_validation(self):
        self.write_config(model_base_url="http://127.0.0.1:1235/v1")
        for name in ("AGIW_MCP_BASE_URL", "AGIW_MCP_MODEL_BASE_URL"):
            for value in ("http://192.0.2.10:1234/v1", "https://127.0.0.1/v1", "http://localhost:1235/v1",
                          "http://user:pw@127.0.0.1/v1", "http://127.0.0.1/v1?x=1",
                          "http://127.0.0.1:65536/v1", "http://[::1]:0/v1", "http://127.0.0.1/v1#x"):
                with self.subTest(name=name, value=value), mock.patch.dict(os.environ, {name: value}):
                    with self.assertRaises(ConfigError) as ctx:
                        load_config()
                    self.assertEqual(ctx.exception.code, "CONFIG_INVALID")

    def test_multiple_environment_overrides_preserve_file_source(self):
        self.write_config(reviewer_model="file-reviewer")
        os.environ["AGIW_MCP_MODEL_BASE_URL"] = "http://127.0.0.1:1235/v1"
        os.environ["AGIW_MCP_AUTHOR_MODEL"] = "environment-author"
        cfg = load_config()
        self.assertEqual(cfg.source, "file+env")
        self.assertEqual(cfg.reviewer_model, "file-reviewer")
        self.assertEqual(cfg.author_model, "environment-author")

    def test_preferred_base_url_overrides_invalid_legacy_url(self):
        self.write_config(model_base_url="http://127.0.0.1:1234/v1")
        os.environ["AGIW_MCP_MODEL_BASE_URL"] = "https://localhost/v1"
        os.environ["AGIW_MCP_BASE_URL"] = "http://127.0.0.1:1235/v1"
        self.assertEqual(load_config().model_base_url, "http://127.0.0.1:1235/v1")

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

    def test_reviewer_base_url_falls_back_and_overrides(self):
        cfg = load_config()
        self.assertIsNone(cfg.reviewer_base_url)
        self.assertEqual(cfg.reviewer_chat_endpoint, "http://127.0.0.1:1234/v1/chat/completions")
        self.write_config(model_base_url="http://127.0.0.1:1235/v1")
        self.assertEqual(load_config().reviewer_models_endpoint, "http://127.0.0.1:1235/v1/models")
        self.write_config(model_base_url="http://127.0.0.1:1235/v1", reviewer_base_url="http://127.0.0.1:1245/v1/")
        cfg = load_config()
        self.assertEqual(cfg.reviewer_base_url, "http://127.0.0.1:1245/v1")
        self.assertEqual(cfg.chat_endpoint, "http://127.0.0.1:1235/v1/chat/completions")
        self.assertEqual(cfg.reviewer_chat_endpoint, "http://127.0.0.1:1245/v1/chat/completions")
        self.assertEqual(cfg.public()["reviewer_base_url"], "http://127.0.0.1:1245/v1")
        os.environ["AGIW_MCP_REVIEWER_BASE_URL"] = "http://[::1]:4567/v1"
        cfg = load_config()
        self.assertEqual(cfg.reviewer_base_url, "http://[::1]:4567/v1")
        self.assertEqual(cfg.source, "file+env")

    def test_reviewer_base_url_null_and_empty_env_fall_back(self):
        self.write_config(model_base_url="http://127.0.0.1:1235/v1", reviewer_base_url=None)
        with mock.patch.dict(os.environ, {"AGIW_MCP_REVIEWER_BASE_URL": ""}):
            cfg = load_config()
        self.assertIsNone(cfg.reviewer_base_url)
        self.assertEqual(cfg.reviewer_chat_endpoint, "http://127.0.0.1:1235/v1/chat/completions")

    def test_inherited_reviewer_base_url_is_cleared_by_test_isolation(self):
        from support import CLEARED_ENV
        self.assertIn("AGIW_MCP_REVIEWER_BASE_URL", CLEARED_ENV)
        self.assertNotIn("AGIW_MCP_REVIEWER_BASE_URL", os.environ)

    def test_reviewer_base_url_keeps_loopback_validation(self):
        for value in ("http://192.0.2.10:1245/v1", "https://127.0.0.1/v1", "http://localhost:1245/v1",
                      "http://user:pw@127.0.0.1/v1", "http://127.0.0.1/v1?x=1", "http://127.0.0.1:65536/v1", 5):
            with self.subTest(value=value):
                self.write_config(reviewer_base_url=value)
                with self.assertRaises(ConfigError) as ctx:
                    load_config()
                self.assertEqual(ctx.exception.code, "CONFIG_INVALID")
                self.assertIn("reviewer_base_url", ctx.exception.message)
        self.write_config()
        with mock.patch.dict(os.environ, {"AGIW_MCP_REVIEWER_BASE_URL": "http://192.0.2.10:1245/v1"}):
            with self.assertRaises(ConfigError):
                load_config()

    def test_reasoning_effort(self):
        self.assertIsNone(load_config().reasoning_effort)
        for value in ("none", "low", "medium", "high"):
            self.write_config(reasoning_effort=value)
            self.assertEqual(load_config().reasoning_effort, value)
        for value in ("off", "None", "", 0, True):
            self.write_config(reasoning_effort=value)
            with self.assertRaises(ConfigError, msg=repr(value)):
                load_config()
        self.write_config(reasoning_effort="high")
        os.environ["AGIW_MCP_REASONING_EFFORT"] = "none"
        self.assertEqual(load_config().reasoning_effort, "none")

    def test_terminal_grant_accepts_only_the_phrase(self):
        from agiw_mcp import __main__ as cli
        for answer, recorded in (("I agree", True), ("i agree", True), ('"I agree"', True), ("I agree.", True),
                                 ("  I AGREE  ", True), ("yes", False), ("I agree to nothing", False),
                                 ("I", False), ("", False)):
            consent.revoke()
            with mock.patch("sys.stdin") as stdin, mock.patch("builtins.input", return_value=answer), \
                    mock.patch("sys.stdout"):
                stdin.isatty.return_value = True
                cli._consent_cli("grant")
            self.assertEqual(consent.consent_recorded(), recorded, repr(answer))

    def test_display_path_hides_home(self):
        home = str(config.Path.home())
        self.assertEqual(config.display_path(home + "/x/y"), "~/x/y")
        self.assertEqual(config.display_path(home + "X"), home + "X")
        self.assertEqual(config.display_path("/opt/tool"), "/opt/tool")

    def test_display_path_windows_separators_and_drive_letter(self):
        with mock.patch.object(config.os, "name", "nt"), \
                mock.patch.object(config.Path, "home", return_value=r"C:\Users\Ada"):
            self.assertEqual(config.display_path(r"C:\Users\Ada\x\y"), "~/x/y")
            self.assertEqual(config.display_path("C:/Users/Ada/x/y"), "~/x/y")
            self.assertEqual(config.display_path(r"c:\users\ada\x\y"), "~/x/y")
            self.assertEqual(config.display_path(r"C:\Users\Ada"), "~")
            self.assertEqual(config.display_path("C:\\Users\\Ada\\"), "~")
            self.assertEqual(config.display_path(r"C:\Users\AdaExtra\x"), r"C:\Users\AdaExtra\x")
            self.assertEqual(config.display_path(r"D:\Users\Ada\x"), r"D:\Users\Ada\x")
            self.assertEqual(config.display_path("/opt/tool"), "/opt/tool")

    def test_per_os_config_dir(self):
        del os.environ["AGIW_MCP_CONFIG_DIR"]
        with mock.patch.object(config.sys, "platform", "darwin"):
            self.assertEqual(config.config_dir(),
                             config.Path.home() / "Library" / "Application Support" / "agiw-mcp")
        if os.name == "nt":
            # Path follows the real os.name; pretending this host is POSIX cannot build a path.
            with mock.patch.object(config.sys, "platform", "win32"):
                os.environ["APPDATA"] = str(self.tmp / "Roaming")
                self.assertEqual(config.config_dir(), self.tmp / "Roaming" / "agiw-mcp")
        else:
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
