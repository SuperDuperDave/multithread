"""Normal completion cleans only its owned group and durably binds capture."""
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from relay_runtime import provider
import test_codex_protocol as protocol


class OwnedCompletionTests(unittest.TestCase):
    setUp = protocol.CodexProtocolTests.setUp
    executable = staticmethod(protocol.CodexProtocolTests.executable)
    configure = protocol.CodexProtocolTests.configure
    invoke = protocol.CodexProtocolTests.invoke

    def test_normal_completion_terminates_descendant_that_closed_stdio(self):
        receipt = self.base / 'synthetic-descendant'
        source = self.provider.read_text().replace(
            "        if spec.get('exit_after_events', True):",
            "        if spec.get('exit_after_events', True):\n"
            "            child = os.fork()\n"
            "            if child == 0:\n"
            "                for fd in (0, 1, 2): os.close(fd)\n"
            "                time.sleep(30)\n"
            "                os._exit(0)\n"
            f"            Path({str(receipt)!r}).write_text(str(child))")
        self.executable(self.provider, source.split('\n', 1)[1])
        pidfd = None
        try:
            code, result, _ = self.invoke('--capture-transport', 'e' * 64)
            child = int(receipt.read_text())
            try:
                pidfd = os.pidfd_open(child)
            except ProcessLookupError:
                pass
            if pidfd is not None:
                poll = select.poll()
                poll.register(pidfd, select.POLLIN)
                self.assertTrue(poll.poll(1000), 'owned descendant remains alive after normal completion')
            self.assertEqual((0, protocol.ANSWER), (code, result['result']))
            self.assertEqual('complete', result['transport_capture']['status'])
        finally:
            if pidfd is not None:
                try:
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.close(pidfd)

    def test_wait_observes_exit_without_releasing_owned_group_identity(self):
        process = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', 'pass'],
                                   start_new_session=True)
        try:
            self.assertEqual(0, provider._wait(process, 2))
            self.assertIsNone(process.returncode, 'leader was reaped before group cleanup')
            self.assertIsNotNone(os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT))
        finally:
            process.wait()

    def test_capture_requires_durable_receipt_binding_after_parent_sync(self):
        code, result, directory = self.invoke('--capture-transport', 'e' * 64)
        self.assertEqual(0, code, result)
        marker = directory / 'capture-complete.json'
        self.assertTrue(marker.exists(), 'inventory and premature receipt are insufficient')
        import hashlib
        binding = json.loads(marker.read_text())
        self.assertEqual(hashlib.sha256((directory / 'result.json').read_bytes()).hexdigest(),
                         binding['receipt_sha256'])
        self.assertEqual(result['transport_capture']['inventory_sha256'], binding['inventory_sha256'])

    def test_parent_sync_failure_after_receipt_rename_leaves_no_completion_binding(self):
        original = provider._atomic_record
        def fail(directory, name, value, **kwargs):
            if name == 'result.json':
                original(directory, name, value, **dict(kwargs, sync_directory=False))
                raise OSError('synthetic parent sync failure after rename')
            return original(directory, name, value, **kwargs)
        with mock.patch.object(provider, '_atomic_record', side_effect=fail):
            code, result, directory = self.invoke('--capture-transport', 'e' * 64)
        self.assertNotEqual(0, code)
        self.assertEqual(protocol.ANSWER, result['result'])
        self.assertEqual('incomplete', result['transport_capture']['status'])
        self.assertFalse((directory / 'capture-complete.json').exists())
        self.assertNotIn('inventory_sha256', result['transport_capture'])

    def test_receipt_file_and_parent_sync_precede_completion_binding(self):
        original_record, original_sync = provider._atomic_record, os.fsync
        current = None
        events = []
        def sync(fd):
            if current is not None:
                events.append((current, 'directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file'))
            return original_sync(fd)
        def record(directory, name, value, **kwargs):
            nonlocal current
            if name not in ('result.json', 'capture-complete.json'):
                return original_record(directory, name, value, **kwargs)
            self.assertTrue(kwargs['sync_directory'])
            retained = os.fstat(kwargs['directory_fd'])
            visible = directory.stat()
            self.assertEqual((visible.st_dev, visible.st_ino), (retained.st_dev, retained.st_ino))
            current = name
            try:
                return original_record(directory, name, value, **kwargs)
            finally:
                current = None
        with (mock.patch.object(provider, '_atomic_record', side_effect=record),
              mock.patch.object(provider.os, 'fsync', side_effect=sync)):
            code, result, _ = self.invoke('--capture-transport', 'e' * 64)
        self.assertEqual(0, code, result)
        self.assertEqual([('result.json', 'file'), ('result.json', 'directory'),
                          ('capture-complete.json', 'file'), ('capture-complete.json', 'directory')], events)

    def test_visible_parent_replacement_never_redirects_completion_publication(self):
        original = provider._atomic_record
        displaced = self.base / 'displaced-call'
        def record(directory, name, value, **kwargs):
            if name == 'result.json':
                directory.rename(displaced)
                directory.mkdir(mode=0o700)
            return original(directory, name, value, **kwargs)
        with mock.patch.object(provider, '_atomic_record', side_effect=record):
            code, result, directory = self.invoke('--capture-transport', 'e' * 64)
        self.assertNotEqual(0, code)
        self.assertEqual('incomplete', result['transport_capture']['status'])
        self.assertTrue((displaced / 'result.json').exists())
        self.assertFalse((directory / 'result.json').exists())
        self.assertFalse((displaced / 'capture-complete.json').exists())
        self.assertFalse((directory / 'capture-complete.json').exists())
