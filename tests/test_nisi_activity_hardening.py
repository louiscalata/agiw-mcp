# Copyright 2026 AGIW MCP contributors
# SPDX-License-Identifier: Apache-2.0
"""Regression checks for concurrent and interrupted Nisi activity writes."""

from __future__ import annotations

from contextlib import contextmanager, redirect_stderr
import errno
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from support import SRC, IsolatedTestCase, subprocess_env
from test_nisi_activity import ARGS, GOOD, POSIX

from agiw_mcp import activity, nisi
from agiw_mcp.config import Config
from agiw_mcp.errors import ToolError

FLOCK = unittest.skipIf(activity.fcntl is None, "flock is POSIX only")
LINE = "agiw-mcp: nisi activity record skipped ({})\n"


class RecordHardeningTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.config_dir.mkdir(mode=0o700)
        self.log = self.config_dir / activity.NISI_LOG_FILE
        self.previous = self.config_dir / (activity.NISI_LOG_FILE + ".1")
        self.lock = self.config_dir / (activity.NISI_LOG_FILE + ".lock")
        self.marker = self.config_dir / (activity.NISI_LOG_FILE + activity.NISI_DAMAGED_SUFFIX)

    def entries(self):
        return [json.loads(line) for line in self.log.read_bytes().splitlines()]

    def assert_lock_free(self):
        """No writer holds the lock: the flock file stays and can be locked; the fallback file is gone."""
        if activity.fcntl is None:
            self.assertFalse(self.lock.exists())
            return
        fd = os.open(self.lock, os.O_WRONLY)
        try:
            activity.fcntl.flock(fd, activity.fcntl.LOCK_EX | activity.fcntl.LOCK_NB)
        finally:
            os.close(fd)

    @contextmanager
    def stderr(self):
        captured = io.StringIO()
        with redirect_stderr(captured):
            yield captured

    @contextmanager
    def held_by_another_process(self):
        script = (
            "import fcntl, os, sys\n"
            "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "sys.stdin.read()\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", script, str(self.lock)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertEqual(process.stdout.readline(), b"held\n")
            yield
        finally:
            process.stdin.close()
            process.wait(timeout=15)
            process.stdout.close()
            process.stderr.close()

    @contextmanager
    def log_close_error(self):
        real_open, real_close = os.open, os.close
        outstanding = {}
        failed_closes = []

        def tracked_open(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            outstanding[fd] = Path(path).name
            return fd

        def close_then_error(fd):
            name = outstanding.pop(fd)
            real_close(fd)
            if name == activity.NISI_LOG_FILE:
                failed_closes.append(fd)
                raise OSError(errno.EIO, "log close failed")

        with (
            mock.patch.object(activity.os, "open", side_effect=tracked_open),
            mock.patch.object(activity.os, "close", side_effect=close_then_error),
        ):
            yield
        self.assertEqual(len(failed_closes), 1)
        self.assertEqual(outstanding, {})

    def test_interleaved_rotation_preserves_the_previous_history(self):
        old = b'{"old":"' + b"x" * activity.MAX_LOG_BYTES + b'"}\n'
        self.log.write_bytes(old)
        real_lstat = os.lstat
        nested = []
        entered = False

        def interleave(path, *args, **kwargs):
            nonlocal entered
            info = real_lstat(path, *args, **kwargs)
            if Path(path).name == activity.NISI_LOG_FILE and not entered:
                entered = True
                nested.append(activity.record_nisi(**GOOD))
            return info

        with mock.patch.object(activity.os, "lstat", side_effect=interleave):
            self.assertTrue(activity.record_nisi(**GOOD))
        self.assertTrue(entered)
        self.assertEqual(self.previous.read_bytes(), old)
        self.assertEqual(len(self.entries()), 1 + sum(nested))

    def test_short_write_rolls_back_before_the_next_append(self):
        self.assertTrue(activity.record_nisi(**GOOD))
        before = self.log.read_bytes()
        real_write = os.write
        with mock.patch.object(activity.os, "write", side_effect=lambda fd, line: real_write(fd, line[:10])):
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(self.log.read_bytes(), before)
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(len(self.entries()), 2)

    def test_write_exception_after_partial_bytes_rolls_back(self):
        self.assertTrue(activity.record_nisi(**GOOD))
        before = self.log.read_bytes()
        real_write = os.write

        def partial_then_error(fd, line):
            real_write(fd, line[:10])
            raise OSError(errno.ENOSPC, "no room")

        with mock.patch.object(activity.os, "write", side_effect=partial_then_error):
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(self.log.read_bytes(), before)
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(len(self.entries()), 2)

    def test_failed_rollback_writes_the_marker_and_refuses_further_appends(self):
        self.assertTrue(activity.record_nisi(**GOOD))
        real_write = os.write
        with (
            self.stderr() as err,
            mock.patch.object(activity.os, "write", side_effect=lambda fd, line: real_write(fd, line[:10])),
            mock.patch.object(activity.os, "ftruncate", side_effect=OSError(errno.EIO, "rollback failed")),
        ):
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(err.getvalue(), LINE.format("damaged"))
        damaged = self.log.read_bytes()
        self.assertTrue(self.marker.is_file())
        self.assertEqual(self.marker.read_bytes(), b"")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)
        self.assert_lock_free()
        with self.stderr():
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(self.log.read_bytes(), damaged)
        self.log.unlink()
        self.marker.unlink()
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(len(self.entries()), 1)

    def test_failed_rollback_in_the_fallback_also_writes_the_marker_and_removes_the_lock(self):
        self.assertTrue(activity.record_nisi(**GOOD))
        self.lock.unlink(missing_ok=True)
        real_write = os.write
        with (
            mock.patch.object(activity, "fcntl", None),
            self.stderr(),
        ):
            with (
                mock.patch.object(activity.os, "write", side_effect=lambda fd, line: real_write(fd, line[:10])),
                mock.patch.object(activity.os, "ftruncate", side_effect=OSError(errno.EIO, "rollback failed")),
            ):
                self.assertFalse(activity.record_nisi(**GOOD))
            self.assertTrue(self.marker.exists())
            self.assertFalse(self.lock.exists())
            self.assertFalse(activity.record_nisi(**GOOD))
            self.assertFalse(self.lock.exists())

    def test_fallback_keeps_its_lock_when_the_marker_cannot_be_written(self):
        self.assertTrue(activity.record_nisi(**GOOD))
        self.lock.unlink(missing_ok=True)
        real_write, real_open = os.write, os.open

        def no_marker(path, *args, **kwargs):
            if Path(path).name == self.marker.name:
                raise OSError(errno.EACCES, "denied")
            return real_open(path, *args, **kwargs)

        with (
            mock.patch.object(activity, "fcntl", None),
            self.stderr() as err,
            mock.patch.object(activity.os, "open", side_effect=no_marker),
            mock.patch.object(activity.os, "write", side_effect=lambda fd, line: real_write(fd, line[:10])),
            mock.patch.object(activity.os, "ftruncate", side_effect=OSError(errno.EIO, "rollback failed")),
        ):
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(err.getvalue(), LINE.format("damaged"))
        self.assertFalse(self.marker.exists())
        self.assertTrue(self.lock.exists())

    def test_a_damaged_marker_blocks_writes_and_prints_one_line(self):
        self.assertTrue(activity.record_nisi(**GOOD))
        before = self.log.read_bytes()
        self.marker.write_bytes(b"")
        with self.stderr() as err:
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(err.getvalue(), LINE.format("damaged"))
        self.assertEqual(self.log.read_bytes(), before)
        self.assert_lock_free()
        self.marker.unlink()
        with self.stderr() as err:
            self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(err.getvalue(), "")

    @POSIX
    def test_a_symlinked_marker_still_blocks_and_is_not_followed(self):
        victim = self.tmp / "victim"
        victim.write_bytes(b"keep")
        self.marker.symlink_to(victim)
        with self.stderr() as err:
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(err.getvalue(), LINE.format("damaged"))
        self.assertTrue(self.marker.is_symlink())
        self.assertEqual(victim.read_bytes(), b"keep")
        self.assertFalse(self.log.exists())

    def test_diagnostic_never_carries_request_content(self):
        fields = {**GOOD, "inputFingerprint": "b" * 64, "outcome": "SECRET_OUTCOME"}
        with self.stderr() as err, mock.patch.object(activity.os, "write", side_effect=OSError(errno.ENOSPC, "x")):
            self.assertFalse(activity.record_nisi(**fields))
        self.assertEqual(err.getvalue(), LINE.format("error"))
        self.assertNotIn("b" * 64, err.getvalue())
        self.assertNotIn("SECRET", err.getvalue())
        self.assertNotIn(str(self.tmp), err.getvalue())

    def test_failed_rollback_and_close_write_the_marker_and_refuse_without_changing_bytes(self):
        self.assertTrue(activity.record_nisi(**GOOD))
        before = self.log.read_bytes()
        real_write = os.write
        partial = []

        def short_write(fd, line):
            partial.append(line[:10])
            return real_write(fd, partial[-1])

        with (
            self.log_close_error(),
            mock.patch.object(activity.os, "write", side_effect=short_write),
            mock.patch.object(activity.os, "ftruncate", side_effect=OSError(errno.EIO, "rollback failed")),
        ):
            self.assertFalse(activity.record_nisi(**GOOD))
        damaged = self.log.read_bytes()
        self.assertEqual(len(partial), 1)
        self.assertEqual(len(damaged) - len(before), 10)
        self.assertEqual(damaged, before + partial[0])
        self.assertTrue(self.marker.exists())
        self.assert_lock_free()
        self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(self.log.read_bytes(), damaged)
        self.assertTrue(self.marker.exists())

    def test_successful_rollback_and_close_error_release_the_lock(self):
        self.assertTrue(activity.record_nisi(**GOOD))
        before = self.log.read_bytes()
        real_write = os.write
        with (
            self.log_close_error(),
            mock.patch.object(activity.os, "write", side_effect=lambda fd, line: real_write(fd, line[:10])),
        ):
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(self.log.read_bytes(), before)
        self.assert_lock_free()
        self.assertFalse(self.marker.exists())
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(len(self.entries()), 2)

    def test_successful_append_and_close_error_preserve_the_line_and_release_the_lock(self):
        self.assertTrue(activity.record_nisi(**GOOD))
        before = self.log.read_bytes()
        with self.log_close_error():
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertTrue(self.log.read_bytes().startswith(before))
        self.assertEqual(len(self.entries()), 2)
        self.assert_lock_free()
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(len(self.entries()), 3)

    def test_write_failure_before_bytes_and_close_error_release_the_lock(self):
        self.assertTrue(activity.record_nisi(**GOOD))
        before = self.log.read_bytes()
        with (
            self.log_close_error(),
            mock.patch.object(activity.os, "write", side_effect=OSError(errno.ENOSPC, "no room")),
        ):
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(self.log.read_bytes(), before)
        self.assert_lock_free()
        self.assertFalse(self.marker.exists())
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(len(self.entries()), 2)

    def test_failed_rollback_and_close_preserve_the_run_result(self):
        result = {**GOOD}
        real_write = os.write
        with (
            self.log_close_error(),
            mock.patch.object(activity.os, "write", side_effect=lambda fd, line: real_write(fd, line[:10])),
            mock.patch.object(activity.os, "ftruncate", side_effect=OSError(errno.EIO, "rollback failed")),
            mock.patch.object(nisi, "_admit_and_run", return_value=result),
        ):
            self.assertIs(nisi.run(dict(ARGS), Config(nisi_activity_log=True)), result)
        self.assertEqual(len(self.log.read_bytes()), 10)
        self.assertTrue(self.marker.exists())

    def test_failed_rollback_and_close_preserve_the_run_error(self):
        error = ToolError("REFUSED", "Run refused.")
        real_write = os.write
        with (
            self.log_close_error(),
            mock.patch.object(activity.os, "write", side_effect=lambda fd, line: real_write(fd, line[:10])),
            mock.patch.object(activity.os, "ftruncate", side_effect=OSError(errno.EIO, "rollback failed")),
            mock.patch.object(nisi, "_admit_and_run", side_effect=error),
        ):
            with self.assertRaises(ToolError) as caught:
                nisi.run(dict(ARGS), Config(nisi_activity_log=True))
            self.assertIs(caught.exception, error)
        self.assertEqual(len(self.log.read_bytes()), 10)
        self.assertTrue(self.marker.exists())

    @POSIX
    def test_hardlinked_log_is_refused_before_rotation(self):
        victim = self.tmp / "victim"
        victim.write_bytes(b"keep\n")
        os.link(victim, self.log)
        self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(victim.read_bytes(), b"keep\n")
        self.assertFalse(self.previous.exists())

    @POSIX
    def test_oversized_hardlinked_log_is_not_rotated(self):
        victim = self.tmp / "victim"
        data = b"x" * (activity.MAX_LOG_BYTES + 1)
        victim.write_bytes(data)
        os.link(victim, self.log)
        self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(victim.read_bytes(), data)
        self.assertTrue(self.log.samefile(victim))
        self.assertFalse(self.previous.exists())

    @POSIX
    def test_hardlink_added_between_stat_and_open_is_refused(self):
        self.log.write_bytes(b"")
        victim = self.tmp / "victim"
        real_open = os.open

        def add_link(path, *args, **kwargs):
            if Path(path).name == activity.NISI_LOG_FILE:
                os.link(self.log, victim)
            return real_open(path, *args, **kwargs)

        with mock.patch.object(activity.os, "open", side_effect=add_link):
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(victim.read_bytes(), b"")

    @POSIX
    def test_stricter_directory_permissions_are_preserved(self):
        os.chmod(self.config_dir, 0o500)
        self.addCleanup(os.chmod, self.config_dir, 0o700)
        self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(stat.S_IMODE(self.config_dir.stat().st_mode), 0o500)
        self.assertFalse(self.log.exists())

    @POSIX
    def test_symlinked_config_directory_is_not_followed_or_chmodded(self):
        target = self.tmp / "target"
        target.mkdir(mode=0o500)
        self.addCleanup(os.chmod, target, 0o700)
        self.config_dir.rmdir()
        self.config_dir.symlink_to(target, target_is_directory=True)
        self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o500)
        self.assertEqual(list(target.iterdir()), [])

    @POSIX
    def test_directory_swap_before_open_does_not_follow_symlink(self):
        target = self.tmp / "target"
        target.mkdir(mode=0o700)
        moved = self.tmp / "original"
        real_open = os.open
        changed = False

        def swap(path, *args, **kwargs):
            nonlocal changed
            if Path(path) == self.config_dir and not changed:
                changed = True
                self.config_dir.rename(moved)
                self.config_dir.symlink_to(target, target_is_directory=True)
            return real_open(path, *args, **kwargs)

        with mock.patch.object(activity.os, "open", side_effect=swap):
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertTrue(changed)
        self.assertEqual(list(target.iterdir()), [])

    def test_fallback_busy_lock_has_a_bounded_wait_and_preserves_contents(self):
        self.lock.write_bytes(b"existing owner")
        started = time.monotonic()
        with mock.patch.object(activity, "fcntl", None), self.stderr() as err:
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(err.getvalue(), LINE.format("locked"))
        self.assertEqual(self.lock.read_bytes(), b"existing owner")
        self.assertFalse(self.log.exists())

    @FLOCK
    def test_a_stale_lock_file_does_not_block_the_next_record(self):
        self.lock.write_bytes(b"left by a killed writer")
        os.chmod(self.lock, 0o600)
        with self.stderr() as err:
            self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(self.lock.read_bytes(), b"left by a killed writer")
        self.assertEqual(len(self.entries()), 1)

    @FLOCK
    def test_a_writer_killed_while_holding_the_lock_does_not_wedge_the_record(self):
        script = (
            "import fcntl, os, signal, sys\n"
            "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "os.kill(os.getpid(), signal.SIGKILL)\n"
        )
        process = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(self.lock)], timeout=15)
        self.assertNotEqual(process.returncode, 0)
        self.assertTrue(self.lock.exists())
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(len(self.entries()), 1)

    @FLOCK
    def test_a_lock_held_by_another_process_is_respected_with_a_bounded_wait(self):
        with self.held_by_another_process():
            started = time.monotonic()
            with self.stderr() as err:
                self.assertFalse(activity.record_nisi(**GOOD))
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(err.getvalue(), LINE.format("locked"))
            self.assertFalse(self.log.exists())
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(len(self.entries()), 1)

    @FLOCK
    def test_a_transient_flock_conflict_is_retried(self):
        real = activity.fcntl
        calls = []

        def conflict_once(fd, operation):
            calls.append(operation)
            if len(calls) == 1:
                raise BlockingIOError(errno.EWOULDBLOCK, "busy")
            return real.flock(fd, operation)

        fake = SimpleNamespace(flock=conflict_once, LOCK_EX=real.LOCK_EX, LOCK_NB=real.LOCK_NB)
        with mock.patch.object(activity, "fcntl", fake), mock.patch.object(activity.time, "sleep") as pause:
            self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(calls, [real.LOCK_EX | real.LOCK_NB] * 2)
        pause.assert_called_once()
        self.assert_lock_free()
        self.assertEqual(len(self.entries()), 1)

    def test_a_transient_lock_conflict_is_retried_in_the_fallback(self):
        real_open = os.open
        conflicted = False

        def conflict_once(path, *args, **kwargs):
            nonlocal conflicted
            if Path(path).name == self.lock.name and not conflicted:
                conflicted = True
                raise FileExistsError(errno.EEXIST, "busy")
            return real_open(path, *args, **kwargs)

        with (
            mock.patch.object(activity, "fcntl", None),
            mock.patch.object(activity.os, "open", side_effect=conflict_once),
            mock.patch.object(activity.time, "sleep") as pause,
        ):
            self.assertTrue(activity.record_nisi(**GOOD))
        self.assertTrue(conflicted)
        pause.assert_called_once()
        self.assertFalse(self.lock.exists())
        self.assertEqual(len(self.entries()), 1)

    def lock_close_error(self):
        real_close, real_open = os.close, os.open
        lock_fds = set()

        def tracked_open(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if Path(path).name == self.lock.name:
                lock_fds.add(fd)
            return fd

        def close_then_error(fd):
            real_close(fd)
            if fd in lock_fds:
                lock_fds.discard(fd)
                raise OSError(errno.EIO, "lock close failed")

        return (
            mock.patch.object(activity.os, "open", side_effect=tracked_open),
            mock.patch.object(activity.os, "close", side_effect=close_then_error),
        )

    def test_fallback_lock_close_and_unlink_failures_do_not_wedge(self):
        real_unlink = os.unlink
        tracked_open, failing_close = self.lock_close_error()
        with mock.patch.object(activity, "fcntl", None), tracked_open, failing_close:
            self.assertTrue(activity.record_nisi(**GOOD))
            self.assertFalse(self.lock.exists())
            with mock.patch.object(activity.os, "unlink", side_effect=OSError(errno.EIO, "unlink failed")):
                self.assertTrue(activity.record_nisi(**GOOD))
            self.assertTrue(self.lock.exists())
            # A lock name left by a failed unlink is reported, bounded, and recovered by removing it.
            with self.stderr() as err:
                self.assertFalse(activity.record_nisi(**GOOD))
            self.assertEqual(err.getvalue(), LINE.format("locked"))
            real_unlink(self.lock)
            self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(len(self.entries()), 3)

    @FLOCK
    def test_flock_close_failure_still_releases_the_lock(self):
        tracked_open, failing_close = self.lock_close_error()
        with tracked_open, failing_close:
            self.assertTrue(activity.record_nisi(**GOOD))
        self.assert_lock_free()
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(len(self.entries()), 2)

    @POSIX
    def test_a_hardlinked_lock_is_refused(self):
        victim = self.tmp / "victim"
        victim.write_bytes(b"keep")
        os.link(victim, self.lock)
        with self.stderr() as err:
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(err.getvalue(), LINE.format("error"))
        self.assertEqual(victim.read_bytes(), b"keep")
        self.assertFalse(self.log.exists())

    def test_lock_is_released_after_a_rotation_failure(self):
        old = b"x" * (activity.MAX_LOG_BYTES + 1)
        self.log.write_bytes(old)
        with mock.patch.object(activity.os, "replace", side_effect=OSError(errno.EIO, "rename failed")):
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(self.log.read_bytes(), old)
        self.assert_lock_free()
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(self.previous.read_bytes(), old)

    @POSIX
    def test_existing_directory_mode_is_not_tightened(self):
        os.chmod(self.config_dir, 0o755)
        self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(stat.S_IMODE(self.config_dir.stat().st_mode), 0o755)

    @POSIX
    def test_directory_identity_is_checked_after_open(self):
        real_fstat = os.fstat

        def different_directory(fd):
            info = real_fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino + 1)
            return info

        with mock.patch.object(activity.os, "fstat", side_effect=different_directory):
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(list(self.config_dir.iterdir()), [])

    @POSIX
    def test_directory_type_is_checked_after_open(self):
        real_fstat = os.fstat

        def not_a_directory(fd):
            info = real_fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_dev=info.st_dev, st_ino=info.st_ino)
            return info

        with mock.patch.object(activity.os, "fstat", side_effect=not_a_directory):
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(list(self.config_dir.iterdir()), [])

    @POSIX
    def test_directory_swap_after_open_keeps_operations_in_the_opened_directory(self):
        self.log.write_bytes(b"")
        target = self.tmp / "target"
        target.mkdir(mode=0o700)
        moved = self.tmp / "original"
        real_lstat = os.lstat
        changed = False

        def swap_after_stat(path, *args, **kwargs):
            nonlocal changed
            info = real_lstat(path, *args, **kwargs)
            if Path(path).name == activity.NISI_LOG_FILE and not changed:
                changed = True
                self.config_dir.rename(moved)
                self.config_dir.symlink_to(target, target_is_directory=True)
            return info

        with mock.patch.object(activity.os, "lstat", side_effect=swap_after_stat):
            self.assertTrue(activity.record_nisi(**GOOD))
        self.assertTrue(changed)
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(len((moved / activity.NISI_LOG_FILE).read_bytes().splitlines()), 1)
        self.assertEqual((moved / self.lock.name).exists(), activity.fcntl is not None)

    @POSIX
    def test_symlink_swap_between_stat_and_open_does_not_write_the_target(self):
        self.log.write_bytes(b"")
        victim = self.tmp / "victim"
        victim.write_bytes(b"keep")
        real_open = os.open

        def swap(path, *args, **kwargs):
            if Path(path).name == activity.NISI_LOG_FILE:
                self.log.unlink()
                self.log.symlink_to(victim)
            return real_open(path, *args, **kwargs)

        with mock.patch.object(activity.os, "open", side_effect=swap):
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(victim.read_bytes(), b"keep")

    @POSIX
    def test_symlink_is_refused_without_platform_nofollow(self):
        victim = self.tmp / "victim"
        victim.write_bytes(b"keep")
        self.log.symlink_to(victim)
        with mock.patch.object(activity.os, "O_NOFOLLOW", 0):
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertEqual(victim.read_bytes(), b"keep")

    def test_lock_is_released_after_a_write_failure(self):
        with mock.patch.object(activity.os, "write", side_effect=OSError(errno.ENOSPC, "no room")):
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assert_lock_free()
        self.assertTrue(activity.record_nisi(**GOOD))

    @POSIX
    def test_a_symlinked_lock_is_not_followed_or_removed(self):
        victim = self.tmp / "victim"
        victim.write_bytes(b"keep")
        self.lock.symlink_to(victim)
        self.assertFalse(activity.record_nisi(**GOOD))
        self.assertTrue(self.lock.is_symlink())
        self.assertEqual(victim.read_bytes(), b"keep")
        self.assertFalse(self.log.exists())

    @POSIX
    def test_open_flags_keep_nofollow_and_nonblock(self):
        real_open = os.open
        opened = []

        def checked(path, flags, *args, **kwargs):
            if Path(path).name == activity.NISI_LOG_FILE:
                opened.append(flags)
                self.assertTrue(flags & os.O_NOFOLLOW)
                self.assertTrue(flags & os.O_NONBLOCK)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(activity.os, "open", side_effect=checked):
            self.assertTrue(activity.record_nisi(**GOOD))
        self.assertEqual(len(opened), 1)

    def test_post_open_file_type_is_checked(self):
        real_fstat = os.fstat

        def changed(fd):
            info = real_fstat(fd)
            if stat.S_ISREG(info.st_mode):
                return SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_nlink=1, st_size=0)
            return info

        with mock.patch.object(activity.os, "fstat", side_effect=changed):
            self.assertFalse(activity.record_nisi(**GOOD))
        self.assertFalse(self.log.exists() and self.log.read_bytes())

    def test_all_owned_descriptors_close_after_success_and_failures(self):
        real_open, real_close = os.open, os.close
        for failure in (False, True):
            with self.subTest(write_failure=failure):
                outstanding = set()

                def tracked_open(*args, outstanding=outstanding, **kwargs):
                    fd = real_open(*args, **kwargs)
                    outstanding.add(fd)
                    return fd

                def tracked_close(fd, outstanding=outstanding):
                    outstanding.remove(fd)
                    return real_close(fd)

                try:
                    with (
                        mock.patch.object(activity.os, "open", side_effect=tracked_open),
                        mock.patch.object(activity.os, "close", side_effect=tracked_close),
                    ):
                        if failure:
                            with mock.patch.object(activity.os, "write", side_effect=OSError(errno.ENOSPC, "no room")):
                                self.assertFalse(activity.record_nisi(**GOOD))
                        else:
                            self.assertTrue(activity.record_nisi(**GOOD))
                    self.assertEqual(outstanding, set())
                finally:
                    for fd in outstanding:
                        real_close(fd)

    def test_parallel_writers_keep_every_success_with_one_rotation(self):
        old = b'{"old":"' + b"x" * activity.MAX_LOG_BYTES + b'"}\n'
        self.log.write_bytes(old)
        script = r"""
import json, pathlib, sys, time
from agiw_mcp import activity
config, index = pathlib.Path(sys.argv[1]), int(sys.argv[2])
while not (config / "go").exists():
    time.sleep(0.001)
fields = json.loads(sys.argv[3])
kept = []
for item in range(80):
    fields["outcome"] = f"P{index}_{item}"
    if activity.record_nisi(**fields):
        kept.append(fields["outcome"])
print(json.dumps(kept))
"""
        processes = []
        try:
            for index in range(4):
                processes.append(
                    subprocess.Popen(
                        [sys.executable, "-B", "-c", script, str(self.config_dir), str(index), json.dumps(GOOD)],
                        env=subprocess_env(self.config_dir, PYTHONPATH=str(SRC)),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                )
            (self.config_dir / "go").touch()
            successful = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stderr.decode())
                successful.extend(json.loads(stdout))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertGreater(len(successful), 0)
        self.assertEqual(self.previous.read_bytes(), old)
        actual = [entry["outcome"] for entry in self.entries()]
        self.assertCountEqual(actual, successful)


class LogNormalizationTests(IsolatedTestCase):
    def entries(self):
        path = self.config_dir / activity.NISI_LOG_FILE
        self.assertTrue(path.is_file(), "validated calls must retain a safe activity line")
        return [json.loads(line) for line in path.read_bytes().splitlines()]

    def test_bad_repair_counts_are_recorded_as_null_without_changing_result(self):
        config = Config(nisi_activity_log=True)
        for value in (True, False, -1, 1001, 1.5, "0", [], {}, None):
            with self.subTest(value=value):
                result = {**GOOD, "repairAttempts": value}
                with mock.patch.object(nisi, "_admit_and_run", return_value=result):
                    self.assertIs(nisi.run(dict(ARGS), config), result)
        entries = self.entries()
        self.assertEqual(len(entries), 9)
        self.assertTrue(all(entry["repairAttempts"] is None for entry in entries))

    def test_valid_repair_count_boundaries_are_preserved(self):
        for value in (0, 1000):
            nisi._log_run(time.monotonic(), False, {**GOOD, "repairAttempts": value})
        self.assertEqual([entry["repairAttempts"] for entry in self.entries()], [0, 1000])

    def test_unknown_sandbox_is_recorded_as_null_without_changing_result(self):
        config = Config(nisi_activity_log=True)
        for value in (True, False, 0, 1, "unexpected", "none\n", [], {}, None):
            with self.subTest(value=value):
                result = {**GOOD, "sandbox": value}
                with mock.patch.object(nisi, "_admit_and_run", return_value=result):
                    self.assertIs(nisi.run(dict(ARGS), config), result)
        entries = self.entries()
        self.assertEqual(len(entries), 9)
        self.assertTrue(all(entry["sandbox"] is None for entry in entries))

    def test_valid_sandbox_labels_are_preserved(self):
        for value in ("none", "node-permission"):
            nisi._log_run(time.monotonic(), False, {**GOOD, "sandbox": value})
        self.assertEqual([entry["sandbox"] for entry in self.entries()], ["none", "node-permission"])


if __name__ == "__main__":
    unittest.main()
