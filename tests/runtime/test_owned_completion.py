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


    def test_unavailable_waitid_still_terminates_owned_provider_and_descendant(self):
        import errno
        for failure in (errno.EPERM, errno.EINVAL, errno.ENOSYS):
            with self.subTest(errno=failure):
                marker = self.base / ('waitid-child-' + str(failure))
                self.configure(spawn_child=str(marker), sleep=True)
                original = os.waitid
                def unavailable(*args):
                    if marker.exists():
                        raise OSError(failure, 'synthetic unavailable exit observation')
                    return original(*args)
                pidfd = leader_fd = None
                try:
                    with mock.patch.object(os, 'waitid', side_effect=unavailable):
                        code, result, _ = self.invoke()
                    try:
                        leader_fd = os.pidfd_open(json.loads(self.receipt.read_text())['pid'])
                    except ProcessLookupError:
                        pass
                    child = int(marker.read_text())
                    try:
                        pidfd = os.pidfd_open(child)
                    except ProcessLookupError:
                        pass
                    if pidfd is not None:
                        poll = select.poll()
                        poll.register(pidfd, select.POLLIN)
                        self.assertTrue(poll.poll(1000), 'owned descendant survived unusable waitid')
                finally:
                    if pidfd is not None:
                        try:
                            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        os.close(pidfd)
                    if leader_fd is not None:
                        try:
                            signal.pidfd_send_signal(leader_fd, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        os.close(leader_fd)

    def test_lost_child_ownership_never_signals_cached_process_group(self):
        import errno
        with mock.patch.object(os, 'waitid', side_effect=ChildProcessError(errno.ECHILD, 'synthetic lost child')):
            with mock.patch.object(os, 'killpg') as kill:
                process = mock.Mock(pid=12345, returncode=None)
                with self.assertRaises(ChildProcessError):
                    provider._owned_exit(process)
                provider._retire_owned_group(process)
                provider._stop(process, immediate=True)
                process.wait.assert_not_called()
                kill.assert_not_called()

    def test_marker_is_final_semantic_commit_without_later_revoking_checks(self):
        from relay_runtime.native_io import TransportCapture
        original_record = provider._atomic_record
        original_check = TransportCapture._check_directories
        committed = False
        def record(directory, name, value, **kwargs):
            nonlocal committed
            original_record(directory, name, value, **kwargs)
            if name == 'capture-complete.json':
                committed = True
        def check(capture):
            if committed:
                raise OSError('synthetic semantic validation after final commit')
            original_check(capture)
        with mock.patch.object(provider, '_atomic_record', side_effect=record):
            with mock.patch.object(TransportCapture, '_check_directories', check):
                code, result, directory = self.invoke('--capture-transport', 'e' * 64)
        self.assertEqual(0, code, result)
        self.assertEqual('complete', result['transport_capture']['status'])
        self.assertTrue((directory / 'capture-complete.json').exists())

    def test_post_commit_directory_close_fault_is_cleanup_diagnostic(self):
        original_record, original_close = provider._atomic_record, os.close
        committed = False
        def record(directory, name, value, **kwargs):
            nonlocal committed
            original_record(directory, name, value, **kwargs)
            if name == 'capture-complete.json':
                committed = True
        def close(fd):
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            original_close(fd)
            if committed and directory:
                raise OSError('synthetic directory descriptor close fault')
        with mock.patch.object(provider, '_atomic_record', side_effect=record):
            with mock.patch.object(os, 'close', side_effect=close):
                code, result, directory = self.invoke('--capture-transport', 'e' * 64)
        self.assertEqual(0, code, result)
        self.assertEqual('complete', result['transport_capture']['status'])
        self.assertTrue(result['transport_capture']['post_commit_cleanup_unavailable'])
        self.assertTrue((directory / 'capture-complete.json').exists())

    def test_uncertain_marker_publication_retracts_binding_best_effort(self):
        original = provider._atomic_record
        def record(directory, name, value, **kwargs):
            original(directory, name, value, **kwargs)
            if name == 'capture-complete.json':
                raise OSError('synthetic reported marker sync failure')
        with mock.patch.object(provider, '_atomic_record', side_effect=record):
            code, result, directory = self.invoke('--capture-transport', 'e' * 64)
        self.assertNotEqual(0, code)
        self.assertEqual(protocol.ANSWER, result['result'])
        self.assertEqual('incomplete', result['transport_capture']['status'])
        self.assertFalse((directory / 'capture-complete.json').exists())

    def test_unretractable_marker_never_attests_caller_finalization(self):
        original_record, original_unlink = provider._atomic_record, os.unlink
        def record(directory, name, value, **kwargs):
            original_record(directory, name, value, **kwargs)
            if name == 'capture-complete.json':
                raise OSError('synthetic reported marker sync failure')
        def unlink(path, **kwargs):
            if path == 'capture-complete.json':
                raise OSError('synthetic unavailable marker retraction')
            return original_unlink(path, **kwargs)
        with mock.patch.object(provider, '_atomic_record', side_effect=record):
            with mock.patch.object(os, 'unlink', side_effect=unlink):
                code, result, directory = self.invoke('--capture-transport', 'e' * 64)
        self.assertNotEqual(0, code)
        self.assertEqual('incomplete', result['transport_capture']['status'])
        binding = json.loads((directory / 'capture-complete.json').read_text())
        self.assertIs(False, binding['proves_caller_finalization'])
        import hashlib
        self.assertEqual(binding['receipt_sha256'],
                         hashlib.sha256((directory / 'result.json').read_bytes()).hexdigest())
