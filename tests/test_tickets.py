# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
import errno
import json
import os
import stat
import subprocess
import sys
import threading
import unittest
from unittest import mock

from support import SRC, IsolatedTestCase, subprocess_env

from agiw_mcp import nisi, tickets, tools
from agiw_mcp.config import load_config
from agiw_mcp.errors import ToolError

ARGS = {"task": "Fix add", "files": [{"path": "calc.py", "content": "def add(a, b):\n    return a - b\n"}]}
WINDOWS_ALIASES = ("calc.py.", "calc.py ", "src./a.py", "src /a.py", "a/b.", "a/b..", "CON", "con.txt",
                   "src/NUL.tar.gz", "Com1", "lpt9.log", "aux ", "con .txt", "PRN", "COM¹", "conin$",
                   "a:b", "calc.py:stream", "c:", "src/a.py::$DATA", " src", "src ")


def ticket(ticket_id="T-1", state="Granted", scope=("src",), owner="writer"):
    return {"ticketId": ticket_id, "claimState": state, "writeScope": list(scope), "owner": owner}


class TicketValidationTests(IsolatedTestCase):
    def test_admissible_states(self):
        for state in ("Granted", "Active"):
            self.assertEqual(tickets.validate(ticket(state=state))["claimState"], state)

    def test_non_admissible_states(self):
        for state in ("Requested", "Held", "Conflict", "Unknown", "granted", "Revoked"):
            with self.assertRaises(ToolError, msg=state) as ctx:
                tickets.validate(ticket(state=state))
            self.assertEqual(ctx.exception.code, "TICKET_NOT_ADMISSIBLE")
            self.assertEqual(ctx.exception.details, {"ticketId": "T-1"})

    def test_malformed_tickets(self):
        cases = [None, "", "T-1", [], {}, 7,
                 {k: v for k, v in ticket().items() if k != "claimState"},
                 {k: v for k, v in ticket().items() if k != "writeScope"},
                 {k: v for k, v in ticket().items() if k != "owner"},
                 {k: v for k, v in ticket().items() if k != "ticketId"},
                 {**ticket(), "extra": 1},
                 ticket(ticket_id=""), ticket(ticket_id="  "), ticket(ticket_id="a/b"), ticket(ticket_id=5),
                 ticket(state=""), ticket(state=None), ticket(owner=""), ticket(owner=None),
                 ticket(scope=()), ticket(scope=("",)), ticket(scope=("  ",)), ticket(scope=("a//b",)),
                 ticket(scope=("../x",)), ticket(scope=("a/./b",)), ticket(scope=("/",)), ticket(scope=(3,)),
                 ticket(scope=(".",)), ticket(scope=("..",)), ticket(scope=("a/..",)),
                 ticket(scope=("a\\b",)), {**ticket(), "writeScope": "src"}]
        for case in cases:
            with self.assertRaises(ToolError, msg=repr(case)[:80]) as ctx:
                tickets.validate(case)
            self.assertEqual(ctx.exception.code, "TICKET_INVALID", repr(case)[:80])

    def test_windows_aliases_refused(self):
        # Win32 strips trailing '.' and ' ', maps device names to devices and reads ':' as a data stream.
        for alias in WINDOWS_ALIASES:
            with self.assertRaises(ToolError, msg=repr(alias)) as ctx:
                tickets.validate(ticket(scope=("docs", alias)))
            self.assertEqual(ctx.exception.code, "TICKET_INVALID", repr(alias))
            self.assertEqual(ctx.exception.details, {"ticketId": "T-1"})

    def test_names_resembling_devices_are_accepted(self):
        scopes = ("console.py", "nul_check.py", "com10", "lpt0.txt", "auxiliary/x", ".gitignore", "a.b.c",
                  "src/connect.py", "comx.py", "/src/")
        self.assertEqual(tickets.validate(ticket(scope=scopes))["writeScope"],
                         ("console.py", "nul_check.py", "com10", "lpt0.txt", "auxiliary/x", ".gitignore", "a.b.c",
                          "src/connect.py", "comx.py", "src"))

    def test_refusals_expose_only_ticket_id(self):
        for state in ("Held", "Requested", "Conflict", "Unknown", "secret-state"):
            with self.assertRaises(ToolError) as ctx:
                tickets.validate(ticket(state=state, scope=("secret-scope",), owner="secret-owner"))
            self.assertEqual(ctx.exception.details, {"ticketId": "T-1"})
            text = repr(ctx.exception.payload())
            for leaked in ("secret", state):
                self.assertNotIn(leaked, text)

    def test_non_utf8_encodable_text_is_invalid(self):
        for case in (ticket(scope=("\ud800",)), ticket(scope=("src", "a/\udcff.py")), ticket(owner="o\ud800")):
            with self.assertRaises(ToolError) as ctx:
                tickets.validate(case)
            self.assertEqual(ctx.exception.code, "TICKET_INVALID")
            self.assertEqual(ctx.exception.details, {"ticketId": "T-1"})

    def test_scopes_are_canonicalized_and_deduplicated(self):
        clean = tickets.validate(ticket(scope=("README.md", "readme.MD", "/Src/A.py/", "café", "CAFÉ")))
        self.assertEqual(clean["writeScope"], ("readme.md", "src/a.py", "café"))

    def test_overlap_ignores_case(self):
        self.assertTrue(tickets.overlaps("README.md", "readme.MD"))
        self.assertTrue(tickets.overlaps("Src", "src/a.py"))
        self.assertTrue(tickets.overlaps("src/A/b.py", "SRC/a"))
        self.assertFalse(tickets.overlaps("Src", "srcx"))

    def test_overlap_on_path_boundaries(self):
        self.assertTrue(tickets.overlaps("src", "src"))
        self.assertTrue(tickets.overlaps("src", "src/a.py"))
        self.assertTrue(tickets.overlaps("src/a/b", "src/a"))
        self.assertFalse(tickets.overlaps("src", "srcx"))
        self.assertFalse(tickets.overlaps("src/a", "src/ab"))
        self.assertFalse(tickets.overlaps("docs", "src"))


class RegistryTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "tickets"
        self.registry = tickets.Registry(self.root)

    def scope_files(self, root=None):
        root = self.root if root is None else root
        return sorted(root.glob("scope-*.lock")) if root.exists() else []

    def records(self, root=None):
        return sorted((json.loads(p.read_text()) for p in self.scope_files(root)), key=lambda r: r["scope"])

    def test_refused_ticket_creates_nothing(self):
        for bad in (ticket(state="Requested"), ticket(scope=()), None, ticket(scope=("ok", "\ud800"))):
            with self.assertRaises(ToolError):
                self.registry.admit(bad)
        self.assertFalse(self.root.exists())
        self.assertEqual(self.registry.admitted(), [])

    def test_windows_alias_refused_before_registry(self):
        for alias in WINDOWS_ALIASES:
            with self.assertRaises(ToolError, msg=repr(alias)) as ctx:
                self.registry.admit(ticket(scope=("ok", alias)))
            self.assertEqual(ctx.exception.code, "TICKET_INVALID")
        self.assertFalse(self.root.exists())
        self.assertEqual(self.registry.admitted(), [])

    def test_windows_alias_cannot_bypass_held_scope(self):
        self.registry.admit(ticket("T-1", scope=("calc.py", "src")))
        for alias in ("calc.py.", "calc.py ", "CALC.PY.", "src./x", "src /x"):
            with self.assertRaises(ToolError, msg=repr(alias)) as ctx:
                self.registry.admit(ticket("T-2", scope=(alias,)))
            self.assertEqual(ctx.exception.code, "TICKET_INVALID", repr(alias))
        self.assertEqual(self.registry.admitted(), ["T-1"])
        self.assertEqual(len(self.scope_files()), 2)

    def test_overlap_refused_atomically(self):
        self.registry.admit(ticket("T-1", scope=("src/a",)))
        before = self.scope_files()
        with self.assertRaises(ToolError) as ctx:
            self.registry.admit(ticket("T-2", scope=("docs", "src/a/b.py", "tests")))
        self.assertEqual(ctx.exception.code, "TICKET_SCOPE_CONFLICT")
        self.assertEqual(ctx.exception.details, {"ticketId": "T-2"})
        self.assertEqual(self.scope_files(), before)
        self.assertEqual(self.registry.admitted(), ["T-1"])
        # The non-overlapping parts were not registered: another ticket can still take them.
        self.registry.admit(ticket("T-3", scope=("docs", "tests")))
        self.assertEqual(self.registry.admitted(), ["T-1", "T-3"])

    def test_prefix_overlap_in_both_directions(self):
        self.registry.admit(ticket("T-1", scope=("src/a/b.py",)))
        with self.assertRaises(ToolError):
            self.registry.admit(ticket("T-2", scope=("src",)))
        self.registry.admit(ticket("T-3", scope=("srcx", "src/a/c.py")))

    def test_windows_refused_before_touching_filesystem(self):
        # Both platform signals refuse on their own; nothing below admit() may run.
        boom = mock.Mock(side_effect=AssertionError("filesystem touched on Windows"))
        for patch in (mock.patch.object(tickets.os, "name", "nt"), mock.patch.object(tickets.sys, "platform", "win32")):
            for candidate in (ticket(), ticket(state="Requested"), None):
                with patch, mock.patch.object(tickets, "_open_dir", boom), \
                        mock.patch.object(tickets, "validate", boom), self.assertRaises(ToolError) as ctx:
                    self.registry.admit(candidate)
                self.assertEqual(ctx.exception.code, "TICKET_UNSUPPORTED_PLATFORM")
                self.assertIn("Windows", ctx.exception.message)
                self.assertIn("reparse-point", ctx.exception.message)
                self.assertEqual(ctx.exception.details, {})
        boom.assert_not_called()
        self.assertFalse(self.root.exists())
        self.assertEqual(self.registry.admitted(), [])

    def test_windows_refusal_leaves_posix_admission_unchanged(self):
        with mock.patch.object(tickets.sys, "platform", "win32"), self.assertRaises(ToolError):
            self.registry.admit(ticket())
        self.registry.admit(ticket())
        self.assertEqual(self.registry.admitted(), ["T-1"])
        self.assertEqual(len(self.scope_files()), 1)

    def test_same_ticket_id_twice_refused(self):
        self.registry.admit(ticket("T-1", scope=("a",)))
        with self.assertRaises(ToolError) as ctx:
            self.registry.admit(ticket("T-1", scope=("b",)))
        self.assertEqual(ctx.exception.code, "TICKET_SCOPE_CONFLICT")

    def test_release_allows_readmission(self):
        self.registry.admit(ticket("T-1", scope=("src",)))
        with self.assertRaises(ToolError):
            self.registry.admit(ticket("T-2", scope=("src",)))
        self.assertTrue(self.registry.release("T-1"))
        self.assertFalse(self.registry.release("T-1"))
        self.assertEqual(self.scope_files(), [])
        self.registry.admit(ticket("T-2", scope=("src",)))
        self.assertEqual(self.registry.admitted(), ["T-2"])

    def lock_writes(self, fail_on=None, chunk=None, fail_close_on=None):
        """Patch os.write/os.close for lock files only. The n-th lock write (1 = admit.lock) raises
        ENOSPC, every write is cut to ``chunk`` bytes, and the n-th lock close raises EIO after closing."""
        real_open, real_write, real_close = os.open, os.write, os.close
        lock_fds, writes, closes = set(), [], []

        def fake_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT and str(path).endswith(".lock"):
                lock_fds.add(fd)
            return fd

        def fake_write(fd, data):
            if fd not in lock_fds:
                return real_write(fd, data)
            writes.append(fd)
            if len(writes) == fail_on:
                raise OSError(errno.ENOSPC, "disk full")
            return real_write(fd, bytes(data[:chunk]) if chunk is not None else data)

        def fake_close(fd):
            real_close(fd)
            if fd in lock_fds:
                lock_fds.discard(fd)
                closes.append(fd)
                if len(closes) == fail_close_on:
                    raise OSError(errno.EIO, "close failed")

        return [mock.patch.object(tickets.os, "open", fake_open), mock.patch.object(tickets.os, "write", fake_write),
                mock.patch.object(tickets.os, "close", fake_close)]

    def assert_failed_cleanly(self, **faults):
        patches = self.lock_writes(**faults)
        with patches[0], patches[1], patches[2]:
            with self.assertRaises(ToolError) as ctx:
                self.registry.admit(ticket("T-1", scope=("a", "b", "c", "d")))
        self.assertEqual(ctx.exception.code, "TICKET_REGISTRY_UNAVAILABLE", faults)
        self.assertEqual(ctx.exception.details, {"ticketId": "T-1"})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [], faults)
        self.assertEqual(self.registry.admitted(), [])
        self.registry.admit(ticket("T-1", scope=("a", "b", "c", "d")))
        self.assertTrue(self.registry.release("T-1"))

    def test_scope_write_failure_after_create_rolls_back(self):
        # The third scope file exists when its write fails: it must be removed along with the first two.
        self.assert_failed_cleanly(fail_on=4)

    def test_scope_close_failure_rolls_back(self):
        self.assert_failed_cleanly(fail_close_on=3)

    def test_admission_lock_write_and_close_failures_leave_nothing(self):
        self.assert_failed_cleanly(fail_on=1)
        self.assert_failed_cleanly(fail_close_on=1)

    def test_zero_byte_write_rolls_back(self):
        self.assert_failed_cleanly(chunk=0)

    def test_short_writes_are_completed(self):
        patches = self.lock_writes(chunk=1)
        with patches[0], patches[1], patches[2]:
            self.registry.admit(ticket("T-1", scope=("src/a.py", "docs")))
        self.assertEqual(self.records(), [{"scope": "docs", "ticketId": "T-1"},
                                          {"scope": "src/a.py", "ticketId": "T-1"}])
        with self.assertRaises(ToolError):
            self.registry.admit(ticket("T-2", scope=("src",)))

    def test_create_exclusive_unlinks_on_failure(self):
        self.root.mkdir(mode=0o700)
        with mock.patch.object(tickets.os, "write", side_effect=OSError(errno.EIO, "io")):
            with self.assertRaises(OSError):
                tickets._create_exclusive(self.root, None, "x.lock", b"data")
        self.assertFalse((self.root / "x.lock").exists())

    def test_held_admission_lock_refuses_clearly(self):
        self.root.mkdir(parents=True)
        (self.root / tickets.ADMIT_LOCK).write_text("1")
        with mock.patch.object(tickets, "ADMIT_WAIT_SECONDS", 0.05):
            with self.assertRaises(ToolError) as ctx:
                self.registry.admit(ticket())
        self.assertEqual(ctx.exception.code, "TICKET_REGISTRY_BUSY")
        self.assertEqual(self.scope_files(), [])

    def test_corrupt_scope_lock_refuses(self):
        self.root.mkdir(parents=True)
        (self.root / "scope-bad.lock").write_text("not json")
        with self.assertRaises(ToolError) as ctx:
            self.registry.admit(ticket("T-9", scope=("x",)))
        self.assertEqual(ctx.exception.code, "TICKET_REGISTRY_CORRUPT")

    def test_case_aliases_conflict(self):
        self.registry.admit(ticket("T-1", scope=("README.md", "Src/Lib")))
        for alias in ("readme.MD", "README.MD", "src", "SRC/lib/x.py", "src/LIB"):
            with self.assertRaises(ToolError, msg=alias) as ctx:
                self.registry.admit(ticket("T-2", scope=(alias,)))
            self.assertEqual(ctx.exception.code, "TICKET_SCOPE_CONFLICT", alias)
        self.assertEqual(self.registry.admitted(), ["T-1"])
        self.registry.admit(ticket("T-3", scope=("readme.txt", "srcx")))

    def test_case_alias_lock_files_collide(self):
        # Even a registry that skipped the overlap scan would hit the same O_EXCL file.
        self.assertEqual(tickets._scope_name("README.md"), tickets._scope_name("readme.MD"))
        self.assertEqual(tickets._scope_name("café"), tickets._scope_name("CAFÉ"))
        self.registry.admit(ticket("T-1", scope=("Docs/Guide.md",)))
        self.assertEqual(self.records(), [{"scope": "docs/guide.md", "ticketId": "T-1"}])

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_registry_dir_created_private(self):
        self.registry.admit(ticket())
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_registry_dir_loose_mode_is_tightened(self):
        self.root.mkdir()
        os.chmod(self.root, 0o777)
        self.registry.admit(ticket())
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_registry_dir_chmod_failure_fails_closed(self):
        self.root.mkdir()
        os.chmod(self.root, 0o777)
        with mock.patch.object(tickets.os, "fchmod", side_effect=PermissionError(errno.EPERM, "no")):
            with self.assertRaises(ToolError) as ctx:
                self.registry.admit(ticket())
        self.assertEqual(ctx.exception.code, "TICKET_REGISTRY_UNSAFE")
        self.assertEqual(ctx.exception.details, {"ticketId": "T-1"})
        self.assertEqual(list(self.root.iterdir()), [])

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_registry_dir_owned_by_another_user_fails_closed(self):
        self.root.mkdir(mode=0o700)
        with mock.patch.object(tickets.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(ToolError) as ctx:
                self.registry.admit(ticket())
        self.assertEqual(ctx.exception.code, "TICKET_REGISTRY_UNSAFE")
        self.assertEqual(list(self.root.iterdir()), [])

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_registry_dir_symlink_fails_closed(self):
        target = self.tmp / "elsewhere"
        target.mkdir(mode=0o700)
        self.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ToolError) as ctx:
            self.registry.admit(ticket())
        self.assertEqual(ctx.exception.code, "TICKET_REGISTRY_UNSAFE")
        self.assertEqual(list(target.iterdir()), [])

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_registry_dir_swapped_for_symlink_at_open_fails_closed(self):
        # The leaf was created (or found) as a directory, then became a symlink: O_NOFOLLOW must still refuse it.
        target = self.tmp / "elsewhere"
        target.mkdir(mode=0o700)
        real_open = os.open

        def swapping_open(path, flags, *args, **kwargs):
            if path == self.root.name and kwargs.get("dir_fd") is not None and flags & os.O_DIRECTORY:
                os.rmdir(self.root)
                self.root.symlink_to(target, target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(tickets.os, "open", swapping_open):
            with self.assertRaises(ToolError) as ctx:
                self.registry.admit(ticket())
        self.assertEqual(ctx.exception.code, "TICKET_REGISTRY_UNSAFE")
        self.assertEqual(list(target.iterdir()), [])

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_registry_path_is_a_file_fails_closed(self):
        self.root.write_text("x")
        with self.assertRaises(ToolError) as ctx:
            self.registry.admit(ticket())
        self.assertEqual(ctx.exception.code, "TICKET_REGISTRY_UNSAFE")

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_release_when_dir_became_unsafe_keeps_locks(self):
        self.registry.admit(ticket("T-1", scope=("src",)))
        os.chmod(self.root, 0o777)
        with mock.patch.object(tickets.os, "fchmod", side_effect=PermissionError(errno.EPERM, "no")):
            self.assertTrue(self.registry.release("T-1"))
        self.assertEqual(len(self.scope_files()), 1)

    # --- ancestry ------------------------------------------------------------

    def assert_unsafe(self, root, untouched=()):
        with self.assertRaises(ToolError) as ctx:
            tickets.Registry(root).admit(ticket())
        self.assertEqual(ctx.exception.code, "TICKET_REGISTRY_UNSAFE")
        self.assertEqual(ctx.exception.details, {"ticketId": "T-1"})
        for directory in untouched:
            self.assertEqual(list(directory.iterdir()), [], directory)

    def shared(self, mode=0o777, name="shared"):
        path = self.tmp / name
        path.mkdir()
        os.chmod(path, mode)
        return path

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_insecure_ancestor_refused(self):
        for index, mode in enumerate((0o777, 0o770, 0o703, 0o720)):
            shared = self.shared(mode, f"shared-{index}")
            self.assert_unsafe(shared / "tickets", untouched=[shared])
            # Deeper below the writable ancestor is no better, even through directories of our own.
            (shared / "mine").mkdir(mode=0o700)
            self.assert_unsafe(shared / "mine" / "tickets", untouched=[shared / "mine"])

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_sticky_writable_ancestor_accepted(self):
        shared = self.shared(0o1777)
        registry = tickets.Registry(shared / "tickets")
        registry.admit(ticket())
        self.assertTrue(registry.release("T-1"))

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_ancestor_owned_by_another_user_refused_before_creating_anything(self):
        parent = self.tmp / "parent"
        parent.mkdir(mode=0o700)
        real_fstat = os.fstat
        parent_id = (parent.stat().st_dev, parent.stat().st_ino)

        def foreign_parent(fd):
            info = real_fstat(fd)
            if (info.st_dev, info.st_ino) == parent_id:
                values = list(info)
                values[stat.ST_UID] = os.geteuid() + 1
                return os.stat_result(values)
            return info

        with mock.patch.object(tickets.os, "fstat", foreign_parent):
            self.assert_unsafe(parent / "tickets", untouched=[parent])

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_secure_symlinked_ancestor_is_resolved(self):
        real = self.tmp / "real"
        real.mkdir(mode=0o700)
        (self.tmp / "link").symlink_to(real, target_is_directory=True)
        sub = self.tmp / "sub"
        sub.mkdir(mode=0o700)
        (sub / "rel").symlink_to("../real", target_is_directory=True)
        for root in (self.tmp / "link" / "a", sub / "rel" / "b"):
            registry = tickets.Registry(root)
            registry.admit(ticket())
            self.assertEqual(len(self.scope_files(real / root.name)), 1)
            self.assertTrue(registry.release("T-1"))

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_symlink_in_insecure_directory_refused(self):
        real = self.tmp / "real"
        real.mkdir(mode=0o700)
        shared = self.shared()
        (shared / "link").symlink_to(real, target_is_directory=True)
        self.assert_unsafe(shared / "link" / "tickets", untouched=[real])

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_symlink_to_insecure_directory_refused(self):
        shared = self.shared()
        (self.tmp / "link").symlink_to(shared, target_is_directory=True)
        self.assert_unsafe(self.tmp / "link" / "tickets", untouched=[shared])
        (self.tmp / "abs").symlink_to(str(shared) + "/", target_is_directory=True)
        self.assert_unsafe(self.tmp / "abs" / "x" / "tickets", untouched=[shared])

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_symlink_owned_by_another_user_refused(self):
        real = self.tmp / "real"
        real.mkdir(mode=0o700)
        link = self.tmp / "link"
        link.symlink_to(real, target_is_directory=True)
        real_stat = os.stat

        def foreign_link(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == "link" and kwargs.get("follow_symlinks") is False:
                values = list(info)
                values[stat.ST_UID] = os.geteuid() + 1
                return os.stat_result(values)
            return info

        with mock.patch.object(tickets.os, "stat", foreign_link):
            self.assert_unsafe(link / "tickets", untouched=[real])

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_symlink_loop_refused(self):
        (self.tmp / "loop").symlink_to("loop")
        self.assert_unsafe(self.tmp / "loop" / "tickets")

    # --- directory identity ----------------------------------------------------

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_release_after_registry_replaced_does_not_touch_new_registry(self):
        self.registry.admit(ticket("T-1", scope=("src",)))
        aside = self.tmp / "aside"
        os.rename(self.root, aside)
        # A fresh registry now sits at the same path and admits an overlapping ticket with the same lock name.
        newer = tickets.Registry(self.root)
        newer.admit(ticket("T-2", scope=("src",)))
        self.assertTrue(self.registry.release("T-1"))
        self.assertEqual(self.records(), [{"scope": "src", "ticketId": "T-2"}])
        # T-1's own lock went with the directory it was created in.
        self.assertEqual(self.scope_files(aside), [])
        with self.assertRaises(ToolError) as ctx:
            newer.admit(ticket("T-3", scope=("src/a.py",)))
        self.assertEqual(ctx.exception.code, "TICKET_SCOPE_CONFLICT")
        self.assertTrue(newer.release("T-2"))
        self.assertEqual(self.scope_files(), [])

    @unittest.skipUnless(tickets._DIR_FD, "POSIX directory checks")
    def test_release_after_registry_replaced_by_symlink_does_not_follow_it(self):
        self.registry.admit(ticket("T-1", scope=("src",)))
        os.rename(self.root, self.tmp / "aside")
        decoy = self.tmp / "decoy"
        decoy.mkdir(mode=0o700)
        lock = decoy / tickets._scope_name("src")
        lock.write_text(json.dumps({"scope": "src", "ticketId": "OTHER"}))
        self.root.symlink_to(decoy, target_is_directory=True)
        self.assertTrue(self.registry.release("T-1"))
        self.assertTrue(lock.exists())
        self.assertEqual(self.scope_files(self.tmp / "aside"), [])

    def test_path_based_release_refuses_replaced_registry(self):
        # Without descriptor support (Windows) the directory identity recorded at admission is checked.
        with mock.patch.object(tickets, "_DIR_FD", False):
            self.registry.admit(ticket("T-1", scope=("src",)))
            aside = self.tmp / "aside"
            os.rename(self.root, aside)
            newer = tickets.Registry(self.root)
            newer.admit(ticket("T-2", scope=("src",)))
            self.assertTrue(self.registry.release("T-1"))
            self.assertEqual(self.records(), [{"scope": "src", "ticketId": "T-2"}])
            self.assertEqual(self.records(aside), [{"scope": "src", "ticketId": "T-1"}])
            self.assertTrue(newer.release("T-2"))
            self.assertEqual(self.scope_files(), [])

    def _race(self, registries):
        barrier = threading.Barrier(len(registries))
        results = []

        def attempt(index, registry):
            barrier.wait()
            try:
                registry.admit(ticket(f"T-{index}", scope=("src" if index % 2 else "src/m.py", f"own-{index}")))
                results.append(index)
            except ToolError as exc:
                refused.append(exc.code)

        refused = []
        threads = [threading.Thread(target=attempt, args=(i, r)) for i, r in enumerate(registries)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(results) + len(refused), len(registries))
        self.assertLessEqual(set(refused), {"TICKET_SCOPE_CONFLICT", "TICKET_REGISTRY_BUSY"})
        return results

    def test_concurrent_threads_admit_exactly_one(self):
        results = self._race([self.registry] * 16)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(self.scope_files()), 2)

    def test_concurrent_registries_on_one_dir_admit_exactly_one(self):
        # Separate Registry objects share no thread lock: only the lock files serialize them.
        results = self._race([tickets.Registry(self.root) for _ in range(16)])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(self.scope_files()), 2)

    def test_other_process_admission_is_respected(self):
        code = ("import sys; from agiw_mcp import tickets; from pathlib import Path; "
                "tickets.Registry(Path(sys.argv[1])).admit({'ticketId': 'P-1', 'claimState': 'Active', "
                "'writeScope': ['src'], 'owner': 'other'}); print('ok', flush=True); sys.stdin.read()")
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(self.root)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, env=subprocess_env(self.config_dir), cwd=str(SRC))
        try:
            self.assertEqual(child.stdout.readline().strip(), b"ok")
            with self.assertRaises(ToolError) as ctx:
                self.registry.admit(ticket("T-1", scope=("src/a.py",)))
            self.assertEqual(ctx.exception.code, "TICKET_SCOPE_CONFLICT")
        finally:
            child.stdin.close()
            child.wait(10)
            child.stdout.close()


class NisiTicketTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(author_model="author-model", reviewer_model="reviewer-model")
        # write_config() honours the umask (0775 under umask 002); agiw-mcp itself creates this folder 0700.
        os.chmod(self.config_dir, 0o700)
        self.root = self.config_dir / "tickets"

    def test_group_writable_config_dir_refused(self):
        if not tickets._DIR_FD:
            self.skipTest("POSIX directory checks")
        os.chmod(self.config_dir, 0o775)
        exc = self.run_refused({**ARGS, "ticket": ticket()})
        self.assertEqual(exc.code, "TICKET_REGISTRY_UNSAFE")

    def no_side_effects(self):
        """Patches that fail the test if Nisi probes Node, creates its temp dir or starts a process."""
        boom = mock.Mock(side_effect=AssertionError("side effect before admission"))
        return [mock.patch.object(nisi, "find_node", boom), mock.patch.object(nisi.tempfile, "mkdtemp", boom),
                mock.patch.object(nisi.subprocess, "Popen", boom), mock.patch.object(nisi.subprocess, "run", boom),
                mock.patch.object(nisi, "_run_validated", boom)], boom

    def run_refused(self, args, config=None):
        patches, boom = self.no_side_effects()
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with self.assertRaises(ToolError) as ctx:
            nisi.run(args, config or load_config())
        boom.assert_not_called()
        self.assertEqual(list(self.root.glob("*")) if self.root.exists() else [], [])
        return ctx.exception

    def test_each_non_admissible_state_refused_without_side_effects(self):
        for state in ("Requested", "Held", "Conflict", "Unknown"):
            exc = self.run_refused({**ARGS, "ticket": ticket(state=state)})
            self.assertEqual(exc.code, "TICKET_NOT_ADMISSIBLE", state)

    def test_blank_and_malformed_tickets_refused_without_side_effects(self):
        for bad in (None, "", {}, [], "T-1", ticket(ticket_id=""), ticket(scope=()), ticket(scope=("",)),
                    {k: v for k, v in ticket().items() if k != "claimState"}):
            exc = self.run_refused({**ARGS, "ticket": bad})
            self.assertEqual(exc.code, "TICKET_INVALID", repr(bad)[:60])

    def test_windows_alias_refused_without_side_effects(self):
        for alias in ("calc.py.", "calc.py ", "NUL", "calc.py:x"):
            exc = self.run_refused({**ARGS, "ticket": ticket(scope=(alias,))})
            self.assertEqual(exc.code, "TICKET_INVALID", repr(alias))
        self.assertFalse(self.root.exists())

    def test_windows_refused_without_side_effects(self):
        config = load_config()
        with mock.patch.object(tickets.sys, "platform", "win32"):
            exc = self.run_refused({**ARGS, "ticket": ticket()}, config)
        self.assertEqual(exc.code, "TICKET_UNSUPPORTED_PLATFORM")
        self.assertFalse(self.root.exists())
        self.assertEqual(tickets.registry().admitted(), [])

    def test_overlapping_ticket_refused_without_side_effects(self):
        held = tickets.registry()
        held.admit(ticket("HELD", scope=("src",)))
        self.addCleanup(held.release, "HELD")
        patches, boom = self.no_side_effects()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaises(ToolError) as ctx:
                nisi.run({**ARGS, "ticket": ticket("T-2", scope=("other", "src/calc.py"))}, load_config())
        self.assertEqual(ctx.exception.code, "TICKET_SCOPE_CONFLICT")
        boom.assert_not_called()
        self.assertEqual(held.admitted(), ["HELD"])

    def test_require_ticket_without_ticket_refuses(self):
        self.write_config(author_model="author-model", reviewer_model="reviewer-model", require_ticket=True)
        self.assertTrue(load_config().require_ticket)
        exc = self.run_refused(dict(ARGS))
        self.assertEqual(exc.code, "TICKET_REQUIRED")

    def test_require_ticket_must_be_boolean(self):
        self.write_config(require_ticket="yes")
        with self.assertRaises(ToolError) as ctx:
            load_config()
        self.assertEqual(ctx.exception.code, "CONFIG_INVALID")

    def test_admitted_before_run_and_released_after(self):
        seen = []

        def fake_run(request, config, temp_parent):
            seen.append(tickets.registry().admitted())
            self.assertNotIn("ticket", request)
            return {"outcome": "COMPLETED"}

        with mock.patch.object(nisi, "_run_validated", fake_run):
            result = nisi.run({**ARGS, "ticket": ticket("T-1", state="Active")}, load_config())
        self.assertEqual(result, {"outcome": "COMPLETED"})
        self.assertEqual(seen, [["T-1"]])
        self.assertEqual(tickets.registry().admitted(), [])
        self.assertEqual(list(self.root.glob("scope-*.lock")), [])

    def test_released_when_run_fails(self):
        with mock.patch.object(nisi, "find_node", return_value=(None, None)):
            with self.assertRaises(ToolError) as ctx:
                nisi.run({**ARGS, "ticket": ticket("T-1")}, load_config())
        self.assertEqual(ctx.exception.code, "NODE_UNAVAILABLE")
        self.assertEqual(tickets.registry().admitted(), [])
        tickets.registry().admit(ticket("T-2"))
        tickets.registry().release("T-2")

    def test_require_ticket_with_admissible_ticket_runs(self):
        self.write_config(author_model="author-model", reviewer_model="reviewer-model", require_ticket=True)
        with mock.patch.object(nisi, "_run_validated", return_value={"outcome": "COMPLETED"}) as inner:
            nisi.run({**ARGS, "ticket": ticket()}, load_config())
        inner.assert_called_once()

    def test_no_ticket_default_config_behaves_as_before(self):
        self.assertFalse(load_config().require_ticket)
        with mock.patch.object(nisi, "find_node", return_value=(None, None)) as find:
            with self.assertRaises(ToolError) as ctx:
                nisi.run(dict(ARGS), load_config())
        self.assertEqual(ctx.exception.code, "NODE_UNAVAILABLE")
        find.assert_called_once()
        self.assertFalse(self.root.exists())
        with mock.patch.object(nisi, "_run_validated", return_value={"outcome": "COMPLETED"}) as inner:
            nisi.run(dict(ARGS), load_config())
        request = inner.call_args.args[0]
        self.assertEqual(request, nisi.validate_request(ARGS))
        self.assertFalse(self.root.exists())

    def test_tool_schema_accepts_ticket(self):
        schema = next(tool for tool in tools.TOOLS if tool["name"] == "agiw_nisi_run")["inputSchema"]
        self.assertIn("ticket", schema["properties"])
        self.assertNotIn("ticket", schema["required"])
        self.assertFalse(schema["properties"]["ticket"]["additionalProperties"])

    def test_tool_refusal_payload_names_only_ticket_id(self):
        result, is_error = tools.call_tool("agiw_nisi_run", {**ARGS, "ticket": ticket(
            "T-7", state="Held", scope=("private/scope",), owner="private-owner")})
        self.assertTrue(is_error)
        self.assertEqual(result["error"]["code"], "TICKET_NOT_ADMISSIBLE")
        self.assertEqual(result["error"]["details"], {"ticketId": "T-7"})
        self.assertNotIn("private", repr(result))
        self.assertNotIn("Held", repr(result))

    def test_tool_refuses_unencodable_scope_before_registry(self):
        result, is_error = tools.call_tool("agiw_nisi_run", {**ARGS, "ticket": ticket("T-8", scope=("\ud800",))})
        self.assertTrue(is_error)
        self.assertEqual(result["error"]["code"], "TICKET_INVALID")
        self.assertEqual(result["error"]["details"], {"ticketId": "T-8"})
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
