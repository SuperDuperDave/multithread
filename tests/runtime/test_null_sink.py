"""Real null-device descriptors and confinement; all writable fixtures are disposable."""

import errno
import fcntl
import os
from pathlib import Path
import platform
import signal
import socket
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from relay_runtime import confinement as subject

_OPEN = os.open
_FSTAT = os.fstat


@unittest.skipUnless(sys.platform == "linux" and platform.machine() == "x86_64",
                     "null confinement requires x86-64 Linux")
class NullSinkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-null-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def assert_substitution_refused(self, replacement, *, stat_error=False):
        opened = []

        def open_actual(path, flags):
            self.assertEqual("/dev/null", path)
            # Prevent a regressed open from blocking forever on the FIFO case.
            self.assertTrue(flags & os.O_PATH)
            fd = _OPEN(replacement, flags)
            opened.append(fd)
            return fd

        try:
            with mock.patch.object(subject.os, "open", side_effect=open_actual):
                if stat_error:
                    with mock.patch.object(subject.os, "fstat",
                                           side_effect=OSError(errno.EIO, "fixture stat failure")):
                        with self.assertRaises(OSError):
                            subject._open_null_sink()
                else:
                    with self.assertRaises(subject.ConfinementError):
                        subject._open_null_sink()
            self.assertEqual(1, len(opened))
            with self.assertRaises(OSError) as caught:
                _FSTAT(opened[0])
            self.assertEqual(errno.EBADF, caught.exception.errno)
        finally:
            for fd in opened:
                try:
                    os.close(fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise

    def test_genuine_null_returns_a_held_noninheritable_path_descriptor(self):
        fd = subject._open_null_sink()
        try:
            info = _FSTAT(fd)
            self.assertTrue(stat.S_ISCHR(info.st_mode))
            self.assertEqual(os.makedev(1, 3), info.st_rdev)
            self.assertTrue(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_PATH)
            self.assertFalse(os.get_inheritable(fd))
        finally:
            os.close(fd)

    def test_real_non_device_shapes_refuse_without_leaking_descriptors(self):
        regular = self.base / "regular"
        regular.write_bytes(b"must remain unchanged")
        fifo = self.base / "fifo"
        os.mkfifo(fifo, 0o600)
        directory = self.base / "directory"
        directory.mkdir()
        symlink = self.base / "symlink"
        symlink.symlink_to("/dev/null")
        # AF_UNIX has a fixed pathname limit; TMPDIR may be a long CI staging path.
        # Only this real socket fixture needs a short, privately owned directory.
        with tempfile.TemporaryDirectory(prefix="relay-sock-", dir="/tmp") as short:
            self.assertEqual(0o700, stat.S_IMODE(os.stat(short).st_mode))
            self.assertEqual(os.geteuid(), os.stat(short).st_uid)
            socket_path = Path(short) / "socket"
            self.assertLess(len(os.fsencode(socket_path)), 108)
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(socket_path))
                for replacement in (regular, fifo, directory, symlink, socket_path):
                    with self.subTest(shape=replacement.name):
                        self.assert_substitution_refused(replacement)
        self.assertEqual(b"must remain unchanged", regular.read_bytes())

    def test_real_wrong_character_device_refuses_without_leaking_descriptor(self):
        wrong = os.stat("/dev/zero")
        self.assertTrue(stat.S_ISCHR(wrong.st_mode))
        self.assertNotEqual(os.makedev(1, 3), wrong.st_rdev)
        self.assert_substitution_refused("/dev/zero")

    def test_fstat_failure_closes_the_real_null_descriptor(self):
        self.assert_substitution_refused("/dev/null", stat_error=True)

    def test_real_forked_policy_allows_null_but_denies_foreign_objects_and_creation(self):
        subject.abi_version()
        foreign = self.base / "foreign"
        foreign.write_bytes(b"untouched")
        created = self.base / "new-file"
        directory = self.base / "new-directory"
        parent_pid = os.getpid()
        reader, writer = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(reader)
            signal.alarm(5)
            try:
                subject.restrict_file_writes((), parent_pid=parent_pid)
                null_fd = _OPEN("/dev/null", os.O_RDWR | os.O_CLOEXEC)
                try:
                    if os.write(null_fd, b"discarded") != 9 or os.read(null_fd, 1) != b"":
                        raise AssertionError("null did not retain its kernel sink behavior")
                finally:
                    os.close(null_fd)
                attempts = (
                    lambda: _OPEN(foreign, os.O_WRONLY),
                    lambda: _OPEN("/dev/zero", os.O_WRONLY),
                    lambda: _OPEN(created, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                    lambda: os.mkdir(directory),
                )
                for operation in attempts:
                    try:
                        opened = operation()
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EPERM):
                            raise
                    else:
                        if isinstance(opened, int):
                            os.close(opened)
                        raise AssertionError("an ungranted object or namespace write was permitted")
                os.write(writer, b"confined")
                os._exit(0)
            except BaseException as exc:
                os.write(writer, (type(exc).__name__ + ": " + str(exc)).encode()[:2000])
                os._exit(1)
        os.close(writer)
        try:
            # The child's alarm also bounds failures before it reports a result.
            message = os.read(reader, 4096)
            _, status = os.waitpid(child, 0)
        finally:
            os.close(reader)
        self.assertEqual(0, os.waitstatus_to_exitcode(status), message.decode())
        self.assertEqual(b"confined", message)
        self.assertEqual(b"untouched", foreign.read_bytes())
        self.assertFalse(created.exists())
        self.assertFalse(directory.exists())
