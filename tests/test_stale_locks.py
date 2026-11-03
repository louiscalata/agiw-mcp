# Copyright 2026 Louis Calata
# SPDX-License-Identifier: Apache-2.0
import errno
import json
import os
import subprocess
import sys
import threading
import time
from unittest import mock

from support import SRC, IsolatedTestCase, subprocess_env
from test_tickets import ticket
from agiw_mcp import tickets
from agiw_mcp.errors import ToolError


class StaleLockTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / 'tickets'
        self.fd = tickets._open_dir(self.root, 'TEST')
        self.addCleanup(os.close, self.fd)
        self.host = mock.patch.object(tickets, '_host_identity', return_value='test-host')
        self.host.start()
        self.addCleanup(self.host.stop)
        self.owner = {'host': 'test-host', 'pid': 2147483647, 'startTime': None,
                      'createdAt': time.time() - 120}

    def write_lock(self, name=tickets.ADMIT_LOCK, owner=True, age=120):
        record = {'scope': 'src', 'ticketId': 'DEAD', 'owner': 'private-owner'}
        if owner:
            record['processOwner'] = dict(self.owner)
        path = self.root / name
        path.write_text(json.dumps(record))
        os.utime(path, (time.time() - age, time.time() - age))
        return path

    def reclaim(self, name=tickets.ADMIT_LOCK):
        return tickets._reclaim_stale(self.root, self.fd, name)

    def test_stale_admission_and_scope_reclaimed_with_receipts(self):
        self.write_lock()
        self.write_lock(tickets._scope_name('src'))
        registry = tickets.Registry(self.root)
        with self.assertLogs('agiw_mcp.tickets', level='WARNING') as logs:
            registry.admit(ticket())
        self.addCleanup(registry.release, 'T-1')
        self.assertEqual(len(logs.output), 2)
        self.assertIn('TICKET_LOCK_RECLAIMED kind=admission', logs.output[0])
        self.assertIn('TICKET_LOCK_RECLAIMED kind=scope', logs.output[1])
        self.assertNotIn('private-owner', repr(logs.output))
        self.assertEqual(registry.admitted(), ['T-1'])
        record = json.loads(next(self.root.glob('scope-*.lock')).read_text())
        self.assertEqual(record['processOwner']['pid'], os.getpid())
        self.assertEqual(record['processOwner']['host'], 'test-host')

    def test_live_pid_kept_even_with_different_start_time(self):
        self.owner.update(pid=os.getpid(), startTime='old-start')
        path = self.write_lock()
        with mock.patch.object(tickets, '_process_start', return_value='new-start') as start:
            self.assertFalse(self.reclaim())
        start.assert_called_once_with(os.getpid())
        self.assertTrue(path.exists())

    def test_matching_start_time_kept(self):
        self.owner['startTime'] = 'same-start'
        self.write_lock()
        with mock.patch.object(tickets, '_process_start', return_value='same-start'), \
                mock.patch.object(tickets.os, 'kill') as kill:
            self.assertFalse(self.reclaim())
        kill.assert_not_called()

    def test_dead_pid_with_recorded_start_reclaimed(self):
        self.owner['startTime'] = 'old-start'
        self.write_lock()
        with mock.patch.object(tickets, '_process_start', return_value=None) as start:
            self.assertTrue(self.reclaim())
        start.assert_called_once_with(self.owner['pid'])

    def test_recent_or_future_lock_kept(self):
        for created_age, file_age in ((0, 120), (120, 0), (-120, 120), (120, -120)):
            with self.subTest(created_age=created_age, file_age=file_age):
                self.owner['createdAt'] = time.time() - created_age
                self.write_lock(age=file_age)
                self.assertFalse(self.reclaim())

    def test_foreign_or_unknown_host_kept_without_pid_probe(self):
        for host in ('another-host', None, ''):
            self.owner['host'] = host
            self.write_lock()
            with mock.patch.object(tickets.os, 'kill') as kill:
                self.assertFalse(self.reclaim())
            kill.assert_not_called()
        self.owner['host'] = 'test-host'
        self.write_lock()
        with mock.patch.object(tickets, '_host_identity', return_value=None):
            self.assertFalse(self.reclaim())

    def test_legacy_ownerless_locks_keep_blocking(self):
        path = self.write_lock(owner=False)
        path.write_text('12345')  # legacy admission files contain only a PID
        with mock.patch.object(tickets, 'ADMIT_WAIT_SECONDS', 0):
            with self.assertRaises(ToolError) as ctx:
                tickets.Registry(self.root).admit(ticket())
        self.assertEqual(ctx.exception.code, 'TICKET_REGISTRY_BUSY')
        path.unlink()
        self.write_lock(tickets._scope_name('src'), owner=False)
        with self.assertRaises(ToolError) as ctx:
            tickets.Registry(self.root).admit(ticket())
        self.assertEqual(ctx.exception.code, 'TICKET_SCOPE_CONFLICT')

    def test_malformed_metadata_kept(self):
        original = dict(self.owner)
        for key, value in (('pid', True), ('pid', -1), ('pid', '123'), ('pid', 2**100),
                           ('createdAt', float('nan')), ('createdAt', float('inf')),
                           ('createdAt', 'old'), ('createdAt', 2**2000), ('startTime', 123), ('startTime', '')):
            with self.subTest(key=key, value=value):
                self.owner = {**original, key: value}
                self.write_lock()
                self.assertFalse(self.reclaim())
        for content in ('', '{', '[]', 'null', 'x' * (tickets.MAX_LOCK_BYTES + 1)):
            self.write_lock().write_text(content)
            self.assertFalse(self.reclaim())

    def test_permission_and_unknown_pid_errors_keep_lock(self):
        path = self.write_lock()
        for code in (errno.EPERM, errno.EIO):
            with mock.patch.object(tickets.os, 'kill', side_effect=OSError(code, 'unknown')):
                self.assertFalse(self.reclaim())
        self.assertTrue(path.exists())

    def test_symlinks_and_hardlinks_kept(self):
        path = self.write_lock()
        aside = self.root / 'aside'
        path.rename(aside)
        path.symlink_to(aside)
        self.assertFalse(self.reclaim())
        path.unlink()
        os.link(aside, path)
        self.assertFalse(self.reclaim())
        self.assertTrue(aside.exists())

    def test_unverifiable_directory_kept(self):
        self.write_lock()
        self.assertFalse(tickets._reclaim_stale(self.root, None, tickets.ADMIT_LOCK))

    def test_two_reclaimers_one_wins(self):
        self.write_lock()
        barrier = threading.Barrier(2)
        results = []
        real_flock = tickets.fcntl.flock

        def together(fd, operation):
            barrier.wait(timeout=5)  # both have opened the original inode
            return real_flock(fd, operation)

        with mock.patch.object(tickets.fcntl, 'flock', side_effect=together):
            threads = [threading.Thread(target=lambda: results.append(self.reclaim())) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertEqual(sorted(results), [False, True])
        self.assertFalse((self.root / tickets.ADMIT_LOCK).exists())

    def test_delayed_reclaimer_does_not_unlink_replacement(self):
        path = self.write_lock()
        real_flock = tickets.fcntl.flock

        def replace_before_flock(fd, operation):
            # Another reclaimer has already removed the old inode and a new owner won O_EXCL.
            path.unlink()
            path.write_text('replacement')
            return real_flock(fd, operation)

        with mock.patch.object(tickets.fcntl, 'flock', side_effect=replace_before_flock):
            self.assertFalse(self.reclaim())
        self.assertEqual(path.read_text(), 'replacement')

    def test_sigkill_owner_and_competing_process_admissions(self):
        self.host.stop()  # real machine identity, real PIDs and actual process death
        self.assertIsNotNone(tickets._host_identity())
        code = """
import sys
from pathlib import Path
from agiw_mcp import tickets
r = tickets.Registry(Path(sys.argv[1]))
r.admit({'ticketId': sys.argv[2], 'claimState': 'Active', 'writeScope': ['src'], 'owner': 'test'})
print('admitted', flush=True)
sys.stdin.read()
"""
        env = subprocess_env(self.config_dir)
        child = subprocess.Popen([sys.executable, '-B', '-c', code, str(self.root), 'CRASH'],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=env, cwd=str(SRC))
        try:
            self.assertEqual(child.stdout.readline().strip(), b'admitted')
            child.kill()
            child.wait(10)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        path = next(self.root.glob('scope-*.lock'))
        record = json.loads(path.read_text())
        record['processOwner']['createdAt'] -= 120
        path.write_text(json.dumps(record))
        os.utime(path, (time.time() - 120, time.time() - 120))
        # Also exercise recovery of an admission file stranded by the same dead process.
        self.write_lock().write_text(json.dumps({'processOwner': record['processOwner']}))
        os.utime(self.root / tickets.ADMIT_LOCK, (time.time() - 120, time.time() - 120))
        contender = """
import sys
from pathlib import Path
from agiw_mcp import tickets
from agiw_mcp.errors import ToolError
print('ready', flush=True)
sys.stdin.readline()
r = tickets.Registry(Path(sys.argv[1]))
try:
    r.admit({'ticketId': sys.argv[2], 'claimState': 'Active', 'writeScope': ['src'], 'owner': 'test'})
    print('won', flush=True)
    sys.stdin.read()
except ToolError as e:
    print(e.code, flush=True)
"""
        children = []
        try:
            for index in range(2):
                children.append(subprocess.Popen(
                    [sys.executable, '-B', '-c', contender, str(self.root), f'R-{index}'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=env, cwd=str(SRC)))
            for proc in children:
                self.assertEqual(proc.stdout.readline().strip(), b'ready')
            for proc in children:
                proc.stdin.write(b'go\n')
                proc.stdin.flush()
            results = [proc.stdout.readline().strip() for proc in children]
            self.assertEqual(sorted(results), [b'TICKET_SCOPE_CONFLICT', b'won'])
            self.assertEqual(len(list(self.root.glob('scope-*.lock'))), 1)
        finally:
            for proc in children:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=10)


class ProcessIdentityTests(IsolatedTestCase):
    def test_linux_machine_identity_includes_pid_namespace(self):
        with mock.patch.object(tickets.sys, 'platform', 'linux'), \
                mock.patch.object(tickets.Path, 'read_text', return_value='a' * 32 + '\n'), \
                mock.patch.object(tickets.os, 'readlink', return_value='pid:[123]'):
            self.assertEqual(tickets._host_identity(), 'linux:' + 'a' * 32 + ':pid:[123]')

    def test_linux_start_identity_includes_boot_and_handles_complex_comm(self):
        # stat fields 3..21 precede starttime; comm may itself contain spaces and parentheses.
        data = '123 (a tricky ) name) ' + ' '.join(['0'] * 19 + ['456', '0'])
        with mock.patch.object(tickets.sys, 'platform', 'linux'), \
                mock.patch.object(tickets.Path, 'read_text', side_effect=[data, 'boot-id\n']):
            self.assertEqual(tickets._process_start(123), 'boot-id:456')

    def test_linux_identity_probe_failures_are_unknown(self):
        with mock.patch.object(tickets.sys, 'platform', 'linux'), \
                mock.patch.object(tickets.Path, 'read_text', side_effect=PermissionError):
            self.assertIsNone(tickets._host_identity())
            self.assertIsNone(tickets._process_start(123))
