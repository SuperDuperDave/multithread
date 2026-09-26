"""Trusted, stdlib-only Linux generation manager and retained-byte loader.

Includes an explicit offline release builder, installer and verified launcher.
No project is enrolled, hook changed or provider contacted by installation.
The bootstrap and the installing OS account are the explicit trust boundary.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys
import sysconfig
from types import MappingProxyType
import uuid


PAYLOAD_MODULES = {
    "relay_core": "relay_core/__init__.py",
    "relay_core.cli": "relay_core/cli.py",
    "relay_core.protocol": "relay_core/protocol.py",
    "relay_core.store": "relay_core/store.py",
    "relay_runtime": "relay_runtime/__init__.py",
    "relay_runtime.enrollment": "relay_runtime/enrollment.py",
    "relay_runtime.admission": "relay_runtime/admission.py",
    "relay_runtime.confinement": "relay_runtime/confinement.py",
    "relay_runtime.cli": "relay_runtime/cli.py",
    "relay_runtime.provider": "relay_runtime/provider.py",
    "relay_runtime.agent": "relay_runtime/agent.py",
    "relay_runtime.codex_peer": "relay_runtime/codex_peer.py",
    "relay_runtime.claude_peer": "relay_runtime/claude_peer.py",
    "relay_runtime.native_io": "relay_runtime/native_io.py",
    "relay_runtime.peer_control": "relay_runtime/peer_control.py",
    "relay_runtime.review_packet": "relay_runtime/review_packet.py",
    "relay_runtime.setup": "relay_runtime/setup.py",
    "relay_runtime.update": "relay_runtime/update.py",
}
PAYLOAD_FILES = frozenset(PAYLOAD_MODULES.values())
# Release management can inspect the published nine-module profile without
# making legacy or arbitrary partial closures eligible for current execution.
_LEGACY_PAYLOAD_FILES = frozenset({
    "relay_core/__init__.py", "relay_core/cli.py", "relay_core/protocol.py",
    "relay_core/store.py", "relay_runtime/__init__.py", "relay_runtime/enrollment.py",
    "relay_runtime/admission.py", "relay_runtime/confinement.py", "relay_runtime/cli.py",
})
_PEER_PAYLOAD_FILES = _LEGACY_PAYLOAD_FILES | {"relay_runtime/provider.py"}
_SETUP_PAYLOAD_FILES = _PEER_PAYLOAD_FILES | {"relay_runtime/setup.py", "relay_runtime/update.py"}
_PUBLISHED_V0415_PAYLOAD_FILES = frozenset({
    "relay_core/__init__.py", "relay_core/cli.py", "relay_core/protocol.py",
    "relay_core/store.py", "relay_runtime/__init__.py", "relay_runtime/enrollment.py",
    "relay_runtime/admission.py", "relay_runtime/confinement.py", "relay_runtime/cli.py",
    "relay_runtime/provider.py", "relay_runtime/codex_peer.py", "relay_runtime/claude_peer.py",
    "relay_runtime/native_io.py", "relay_runtime/peer_control.py",
    "relay_runtime/setup.py", "relay_runtime/update.py",
})
_RELEASE_PAYLOAD_SETS = (_LEGACY_PAYLOAD_FILES, _PEER_PAYLOAD_FILES,
                         _SETUP_PAYLOAD_FILES, _PUBLISHED_V0415_PAYLOAD_FILES, PAYLOAD_FILES)
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_MEMBER = 1024 * 1024


class BootstrapError(RuntimeError):
    """Installation state is unavailable, ambiguous, or not approved."""


def default_install_root():
    if sys.platform != "linux" or os.getuid() != os.geteuid():
        raise BootstrapError("only ordinary Linux account execution is supported")
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not home.is_absolute():
        raise BootstrapError("the OS account has no absolute home")
    return home / ".local" / "share" / "relay" / "runtime"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BootstrapError("duplicate metadata field")
        result[key] = value
    return result


def _json(payload):
    try:
        value = json.loads(payload, object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as exc:
        raise BootstrapError("invalid installation metadata") from exc
    if not isinstance(value, dict) or _canonical(value) != payload:
        raise BootstrapError("installation metadata must be a canonical object")
    return value


def manifest_for(payload):
    """Release-build helper, NOT approval of arbitrary source at install time."""
    return _payload_manifest(payload)


def _payload_manifest(payload, *, release_management=False):
    accepted = _RELEASE_PAYLOAD_SETS if release_management else (PAYLOAD_FILES,)
    if set(payload) not in accepted:
        raise BootstrapError("payload does not match the closed module set")
    members = {}
    for name, body in payload.items():
        if type(body) is not bytes or not body or len(body) > _MAX_MEMBER:
            raise BootstrapError("payload member must contain bounded bytes")
        members[name] = {"sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
    return _canonical({"format": 1, "members": members})


def _validate_manifest(manifest, *, release_management=False):
    if type(manifest) is not bytes or len(manifest) > 65536:
        raise BootstrapError("manifest must contain bounded approved bytes")
    value = _json(manifest)
    accepted = _RELEASE_PAYLOAD_SETS if release_management else (PAYLOAD_FILES,)
    if (set(value) != {"format", "members"} or type(value["format"]) is not int
            or value["format"] != 1 or not isinstance(value["members"], dict)
            or set(value["members"]) not in accepted):
        raise BootstrapError("unsupported runtime manifest")
    for entry in value["members"].values():
        if (not isinstance(entry, dict) or set(entry) != {"sha256", "size"}
                or type(entry["size"]) is not int or not 0 < entry["size"] <= _MAX_MEMBER
                or not isinstance(entry["sha256"], str) or not _HEX.fullmatch(entry["sha256"])):
            raise BootstrapError("invalid runtime manifest member")
    return value


def _same(first, second):
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _directory_info(info, *, private):
    if not stat.S_ISDIR(info.st_mode):
        raise BootstrapError("expected an installation directory")
    if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise BootstrapError("installation directories must be private and account-owned")


def _trusted_ancestor(info, path):
    sticky_temp = (path in (Path("/tmp"), Path("/var/tmp"))
                   and info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
    if (info.st_uid not in (0, os.getuid())
            or (stat.S_IMODE(info.st_mode) & 0o022 and not sticky_temp)):
        raise BootstrapError("installation ancestry is not trusted")


class _Directory:
    """Retain each traversed directory; use relative no-follow operations."""

    def __init__(self, path, *, create=False, secure=True, private_leaf=True):
        path = Path(path)
        if not path.is_absolute():
            path = Path.cwd() / path
        if ".." in path.parts:
            raise BootstrapError("installation paths containing parent traversal are unsupported")
        self.path, self.secure, self.handles, self.links = path, secure, [], []
        try:
            current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.handles.append(current)
            walked = Path("/")
            for name in path.parts[1:]:
                walked /= name
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                    os.fsync(current)
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                self.handles.append(child)
                self.links.append((current, name, child, walked, walked == path and private_leaf))
                info = os.fstat(child)
                _directory_info(info, private=False)
                if secure:
                    _trusted_ancestor(info, walked)
                current = child
            self.fd = current
            _directory_info(os.fstat(current), private=secure and private_leaf)
        except BaseException:
            self.close()
            raise

    def child(self, parent, name, *, create=False):
        if not name or "/" in name or name in (".", ".."):
            raise BootstrapError("invalid relative installation name")
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            os.fsync(parent)
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        self.handles.append(fd)
        parent_path = self.path if parent == self.fd else next(link[3] for link in self.links if link[2] == parent)
        self.links.append((parent, name, fd, parent_path / name, True))
        _directory_info(os.fstat(fd), private=self.secure)
        return fd

    def recheck(self):
        for parent, name, child, path, private in self.links:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not _same(current, os.fstat(child)) or not stat.S_ISDIR(current.st_mode):
                raise BootstrapError("installation namespace changed during operation")
            _directory_info(current, private=self.secure and private)
            if self.secure:
                _trusted_ancestor(current, path)

    def retain_file(self, fd):
        """Own a prepared publication descriptor until this custody closes."""
        self.handles.append(fd)
        return fd

    def close(self):
        for fd in reversed(self.handles):
            os.close(fd)
        self.handles = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _read(directory, name, *, private=True, limit=_MAX_MEMBER):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit
                or (private and (before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077))):
            raise BootstrapError("runtime member is not an eligible regular file")
        body = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(body) > limit or not _same(before, after)
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise BootstrapError("runtime member changed while reading")
    return body


def _write_new(directory, name, body, *, retain=False):
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
        if retain:
            return fd
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)


def _verify_publication(directory, name, held_fd, body):
    named = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if not _same(named, os.fstat(held_fd)) or _read(directory, name, limit=65536) != body:
        raise BootstrapError("activation publication does not match its retained record")
    if not _same(os.stat(name, dir_fd=directory, follow_symlinks=False), os.fstat(held_fd)):
        raise BootstrapError("activation record changed during publication verification")


def snapshot_source(source, approved_manifest):
    """Capture only approved members before creating any installation state."""
    _validate_manifest(approved_manifest)
    bodies = {}
    with _Directory(source, secure=False) as directory:
        packages = {}
        for name in sorted(PAYLOAD_FILES):
            package, leaf = name.split("/")
            if package not in packages:
                packages[package] = directory.child(directory.fd, package)
            bodies[name] = _read(packages[package], leaf, private=False)
        directory.recheck()
    if manifest_for(bodies) != approved_manifest:
        raise BootstrapError("source bytes do not match the approved release manifest")
    return MappingProxyType(bodies)


@dataclass(frozen=True)
class Activation:
    digest: str
    activation_id: str
    previous_id: str | None


def _active(directory):
    try:
        value = _json(_read(directory, "active.json", limit=65536))
    except FileNotFoundError:
        return None
    if (set(value) != {"format", "digest", "activation_id", "previous_id"}
            or type(value["format"]) is not int or value["format"] != 1
            or not isinstance(value["digest"], str) or not _HEX.fullmatch(value["digest"])
            or not isinstance(value["activation_id"], str) or not _ID.fullmatch(value["activation_id"])
            or (value["previous_id"] is not None and (not isinstance(value["previous_id"], str)
                or not _ID.fullmatch(value["previous_id"])))):
        raise BootstrapError("invalid active runtime record")
    return Activation(value["digest"], value["activation_id"], value["previous_id"])


def _inspect_lock(directory):
    """Check existing lock readiness without creating, locking or repairing it."""
    directory.recheck()
    try:
        fd = os.open("activation.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory.fd)
    except FileNotFoundError:
        if not os.fstat(directory.fd).st_mode & stat.S_IWUSR:
            raise BootstrapError("missing activation lock cannot be created in this read-only directory")
        return
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_mode & 0o600 != 0o600):
            raise BootstrapError("activation lock is not an account-readable/writable private regular file")
        named = os.stat("activation.lock", dir_fd=directory.fd, follow_symlinks=False)
        if not _same(info, named) or info.st_mode != named.st_mode:
            raise BootstrapError("activation lock changed during inspection")
        directory.recheck()
    finally:
        os.close(fd)


@contextmanager
def _lock(directory, *, allow_uninstall=False):
    directory.recheck()
    fd = os.open("activation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory.fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise BootstrapError("activation lock is not a private regular file")
        os.fsync(directory.fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        def verify_lock():
            directory.recheck()
            current = os.stat("activation.lock", dir_fd=directory.fd, follow_symlinks=False)
            if (not _same(info, current) or not stat.S_ISREG(current.st_mode)
                    or current.st_uid != os.getuid() or current.st_nlink != 1
                    or stat.S_IMODE(current.st_mode) & 0o077):
                raise BootstrapError("activation lock was replaced")
        verify_lock()
        if not allow_uninstall and any(not row["completed"] for row in _uninstall_records(directory).values()):
            raise BootstrapError("installed-code uninstall is incomplete; resume it before management writes")
        yield verify_lock
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class Installation:
    """Internal test root injection; a public launcher must use for_account()."""

    def __init__(self, root):
        self.root = Path(root)

    @classmethod
    def for_account(cls):
        return cls(default_install_root())

    def install(self, source, approved_manifest):
        bodies = snapshot_source(source, approved_manifest)
        digest = hashlib.sha256(approved_manifest).hexdigest()
        with _Directory(self.root, create=True) as directory, _lock(directory) as verify_lock:
            generations = directory.child(directory.fd, "generations", create=True)
            try:
                os.mkdir(digest, mode=0o700, dir_fd=generations)
            except FileExistsError:
                self._verify(directory, digest)
                verify_lock()
                return digest
            os.fsync(generations)
            generation = directory.child(generations, digest)
            packages = {}
            for name in sorted(bodies):
                package, leaf = name.split("/")
                if package not in packages:
                    packages[package] = directory.child(generation, package, create=True)
                _write_new(packages[package], leaf, bodies[name])
            verify_lock()
            _write_new(generation, "manifest.json", approved_manifest)
            os.fsync(generations)
            self._verify(directory, digest)
            verify_lock()
        return digest

    def _verify(self, directory, digest):
        if not isinstance(digest, str) or not _HEX.fullmatch(digest):
            raise BootstrapError("invalid generation identity")
        generations = directory.child(directory.fd, "generations")
        generation = directory.child(generations, digest)
        if set(os.listdir(generation)) != {"manifest.json", "relay_core", "relay_runtime"}:
            raise BootstrapError("generation contains an incomplete or unknown member set")
        manifest = _read(generation, "manifest.json", limit=65536)
        _validate_manifest(manifest)
        if hashlib.sha256(manifest).hexdigest() != digest:
            raise BootstrapError("generation manifest does not match its approved digest")
        bodies = {}
        for package in ("relay_core", "relay_runtime"):
            child = directory.child(generation, package)
            expected = {name.split("/")[1] for name in PAYLOAD_FILES if name.startswith(package + "/")}
            if set(os.listdir(child)) != expected:
                raise BootstrapError("generation package has unknown or missing members")
            for leaf in sorted(expected):
                bodies[f"{package}/{leaf}"] = _read(child, leaf)
        if manifest_for(bodies) != manifest:
            raise BootstrapError("generation bytes failed verification")
        directory.recheck()
        return VerifiedRuntime(digest, self.root / "generations" / digest, MappingProxyType(bodies))

    def activate(self, digest, *, expected_activation):
        with _Directory(self.root) as directory, _lock(directory) as verify_lock:
            runtime = self._verify(directory, digest)
            current = _active(directory.fd)
            current_id = current.activation_id if current else None
            if current_id != expected_activation:
                raise BootstrapError("activation changed; stale activation or rollback refused")
            new = Activation(runtime.digest, uuid.uuid4().hex, current_id)
            body = _canonical({"format": 1, "digest": new.digest,
                               "activation_id": new.activation_id, "previous_id": new.previous_id})
            temporary = f".activation-{uuid.uuid4().hex}"
            pending_fd = _write_new(directory.fd, temporary, body, retain=True)
            try:
                verify_lock()
                _verify_publication(directory.fd, temporary, pending_fd, body)
                os.replace(temporary, "active.json", src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
                os.fsync(directory.fd)
                verify_lock()
                _verify_publication(directory.fd, "active.json", pending_fd, body)
            except (OSError, BootstrapError) as exc:
                raise BootstrapError("activation outcome is uncertain; inspect the active record before retrying") from exc
            finally:
                try:
                    named = os.stat(temporary, dir_fd=directory.fd, follow_symlinks=False)
                    if _same(named, os.fstat(pending_fd)):
                        os.unlink(temporary, dir_fd=directory.fd)
                except FileNotFoundError:
                    pass
                finally:
                    os.close(pending_fd)
            return new

    def active(self):
        with _Directory(self.root) as directory:
            active = _active(directory.fd)
            if active is None:
                raise BootstrapError("no runtime has been activated")
            directory.recheck()
            return active

    def load_active(self):
        with _Directory(self.root) as directory:
            active = _active(directory.fd)
            if active is None:
                raise BootstrapError("no runtime has been activated")
            return self._verify(directory, active.digest)


@dataclass(frozen=True)
class VerifiedRuntime:
    digest: str
    origin: Path
    bodies: object

    def install_importer(self):
        if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
            raise BootstrapError("runtime execution requires Python -I -S -B")
        if set(self.bodies) != PAYLOAD_FILES:
            raise BootstrapError("runtime execution requires the current closed module set")
        roots = ("relay_core", "relay_runtime")
        if any(name == root or name.startswith(root + ".") for name in sys.modules for root in roots):
            raise BootstrapError("a Multithread module was loaded before generation verification")
        expected_finders = [importlib.machinery.BuiltinImporter, importlib.machinery.FrozenImporter,
                            importlib.machinery.PathFinder]
        if sys.meta_path != expected_finders:
            raise BootstrapError("unexpected Python import hook before runtime execution")
        stdlib = Path(sysconfig.get_path("stdlib"))
        allowed_paths = {str(stdlib), str(stdlib / "lib-dynload"),
                         str(stdlib.parent / f"python{sys.version_info.major}{sys.version_info.minor}.zip")}
        if any(path not in allowed_paths for path in sys.path):
            raise BootstrapError("unexpected Python module search path")
        finder = _RetainedFinder(self)
        sys.meta_path.insert(0, finder)
        return finder


class _RetainedFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, runtime):
        self.runtime = runtime

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in PAYLOAD_MODULES:
            if any(fullname == root or fullname.startswith(root + ".") for root in ("relay_core", "relay_runtime")):
                raise ModuleNotFoundError("module is outside the verified Multithread closure")
            return None
        package = PAYLOAD_MODULES[fullname].endswith("/__init__.py")
        spec = importlib.machinery.ModuleSpec(fullname, self, origin=str(self.runtime.origin / PAYLOAD_MODULES[fullname]), is_package=package)
        if package:
            spec.submodule_search_locations = []
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = PAYLOAD_MODULES[module.__name__]
        origin = str(self.runtime.origin / name)
        module.__file__ = origin
        if name.endswith("/__init__.py"):
            module.__path__ = []
        exec(compile(self.runtime.bodies[name], origin, "exec", dont_inherit=True), module.__dict__)


# Public release interface. The reviewed bootstrap is the initial trust root.
# A release digest is an integrity/selection pin, not a publisher signature.
_RELEASE_VERSION = re.compile(r"[0-9][A-Za-z0-9.+-]{0,63}\Z")
_LAUNCHER_PROTOCOL = 1

# Protocol-one launcher: execute retained bootstrap bytes, never hash then import
# a mutable filename. This template is compatibility-sensitive across releases.
_LOADER = r"""
import hashlib, os, pathlib, pwd, stat, sys, types
root, release_id, bootstrap_sha, activation_id = sys.argv[1:5]
args = sys.argv[5:]
expected = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/share/relay/installation"
if os.getuid() != os.geteuid() or pathlib.Path(root) != expected:
    raise SystemExit("relay: launcher account identity mismatch")
path = expected / "releases" / release_id / "bootstrap.py"
held = []
try:
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    held.append(fd)
    walked = pathlib.Path("/")
    for part in path.parts[1:-1]:
        walked /= part
        fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        held.append(fd)
        info = os.fstat(fd)
        temporary = str(walked) in ("/tmp", "/var/tmp") and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if info.st_uid not in (0, os.getuid()) or (stat.S_IMODE(info.st_mode) & 0o022 and not temporary):
            raise RuntimeError("unsafe launcher ancestry")
    source = os.open("bootstrap.py", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    held.append(source)
    info = os.fstat(source)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("unsafe installed bootstrap")
    chunks, size = [], 0
    while True:
        chunk = os.read(source, 65536)
        if not chunk:
            break
        size += len(chunk)
        if size > 1048576:
            raise RuntimeError("installed bootstrap exceeds bound")
        chunks.append(chunk)
    body = b"".join(chunks)
    if hashlib.sha256(body).hexdigest() != bootstrap_sha:
        raise RuntimeError("installed bootstrap failed release verification")
    module = types.ModuleType("_relay_verified_bootstrap")
    module.__file__ = str(path)
    module.__dict__["__relay_bootstrap_sha256__"] = bootstrap_sha
    sys.modules[module.__name__] = module
    exec(compile(body, str(path), "exec", dont_inherit=True), module.__dict__)
    result = module.run_installed(release_id, activation_id, args)
except (OSError, RuntimeError) as exc:
    print("relay: verified launcher unavailable: " + str(exc), file=sys.stderr)
    result = 1
finally:
    for fd in reversed(held):
        os.close(fd)
raise SystemExit(result)
"""


def _launcher_body(root, release_id, bootstrap_sha, activation_id):
    import shlex
    arguments = ["/usr/bin/python3", "-I", "-S", "-B", "-c", _LOADER,
                 str(root), release_id, bootstrap_sha, activation_id]
    return ("#!/bin/sh\nexec " + " ".join(shlex.quote(arg) for arg in arguments)
            + ' "$@"\n').encode()


@dataclass(frozen=True)
class Release:
    digest: str
    version: str
    record: bytes
    bootstrap: bytes
    runtime_manifest: bytes
    bodies: object

    @property
    def bootstrap_sha(self):
        return hashlib.sha256(self.bootstrap).hexdigest()


def _release_record(version, bootstrap, bodies, *, release_management=False):
    if not isinstance(version, str) or not _RELEASE_VERSION.fullmatch(version):
        raise BootstrapError("release version must be a bounded filename-safe label")
    if not bootstrap or len(bootstrap) > _MAX_MEMBER:
        raise BootstrapError("bootstrap size is unsupported")
    return _canonical({
        "format": 1, "launcher_protocol": _LAUNCHER_PROTOCOL, "version": version,
        "bootstrap": {"sha256": hashlib.sha256(bootstrap).hexdigest(), "size": len(bootstrap)},
        "runtime": _json(_payload_manifest(bodies, release_management=release_management)),
    })


def _release_from_directory(directory, expected_digest, *, private):
    if not isinstance(expected_digest, str) or not _HEX.fullmatch(expected_digest):
        raise BootstrapError("a complete independently approved release SHA256 is required")
    if set(_bounded_names(directory.fd)) != {"release.json", "bootstrap.py", "payload"}:
        raise BootstrapError("release contains unknown or missing members")
    record = _read(directory.fd, "release.json", private=private, limit=65536)
    if hashlib.sha256(record).hexdigest() != expected_digest:
        raise BootstrapError("release record does not match the approved digest")
    value = _json(record)
    if (set(value) != {"format", "launcher_protocol", "version", "bootstrap", "runtime"}
            or type(value["format"]) is not int or value["format"] != 1
            or type(value["launcher_protocol"]) is not int
            or value["launcher_protocol"] != _LAUNCHER_PROTOCOL):
        raise BootstrapError("unsupported release or launcher protocol")
    runtime_manifest = _canonical(value["runtime"])
    manifest = _validate_manifest(runtime_manifest, release_management=True)
    bootstrap = _read(directory.fd, "bootstrap.py", private=private)
    payload = directory.child(directory.fd, "payload")
    if set(_bounded_names(payload)) != {"relay_core", "relay_runtime"}:
        raise BootstrapError("release payload has unknown package members")
    bodies = {}
    for package in ("relay_core", "relay_runtime"):
        folder = directory.child(payload, package)
        leaves = {name.split("/")[1] for name in manifest["members"] if name.startswith(package + "/")}
        if set(_bounded_names(folder)) != leaves:
            raise BootstrapError("release package has unknown or missing members")
        for leaf in sorted(leaves):
            bodies[f"{package}/{leaf}"] = _read(folder, leaf, private=private)
    if _release_record(value["version"], bootstrap, bodies, release_management=True) != record:
        raise BootstrapError("release bytes failed bootstrap/runtime verification")
    directory.recheck()
    return Release(expected_digest, value["version"], record, bootstrap,
                   runtime_manifest, MappingProxyType(bodies))


def read_release(path, expected_digest, *, private=False):
    with _Directory(path, secure=private) as directory:
        return _release_from_directory(directory, expected_digest, private=private)


def _write_release(directory, release):
    _write_new(directory.fd, "bootstrap.py", release.bootstrap)
    payload = directory.child(directory.fd, "payload", create=True)
    packages = {}
    for name, body in sorted(release.bodies.items()):
        package, leaf = name.split("/")
        if package not in packages:
            packages[package] = directory.child(payload, package, create=True)
        _write_new(packages[package], leaf, body)
    directory.recheck()
    _write_new(directory.fd, "release.json", release.record)
    _release_from_directory(directory, release.digest, private=True)


def build_release(source, output, version, *, bootstrap_path=None):
    """Build a closed local bundle; printing its digest does not approve it."""
    source = Path(source)
    bootstrap_path = Path(bootstrap_path or __file__)
    with _Directory(source, secure=False) as directory:
        bodies = {}
        for package in ("relay_core", "relay_runtime"):
            folder = directory.child(directory.fd, package)
            for name in sorted(PAYLOAD_FILES):
                if name.startswith(package + "/"):
                    bodies[name] = _read(folder, name.split("/")[1], private=False)
        directory.recheck()
    with _Directory(bootstrap_path.parent, secure=False) as directory:
        bootstrap = _read(directory.fd, bootstrap_path.name, private=False)
        directory.recheck()
    record = _release_record(version, bootstrap, bodies)
    release = Release(hashlib.sha256(record).hexdigest(), version, record, bootstrap,
                      manifest_for(bodies), MappingProxyType(bodies))
    output = Path(os.path.abspath(output))
    with _Directory(output.parent, create=True, private_leaf=False) as parent:
        # Exclusive directory creation: never fill/adopt an existing output.
        os.mkdir(output.name, 0o700, dir_fd=parent.fd)
        os.fsync(parent.fd)
        parent.recheck()
    with _Directory(output) as directory:
        if os.listdir(directory.fd):
            raise BootstrapError("new release destination is not empty")
        _write_release(directory, release)
    return release


def _checkpoint(stage):
    """Internal abrupt-exit/publication test seam."""


def _rename_operation(directory, first, second, flags):
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise BootstrapError("atomic launcher exchange is unsupported") from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                         ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(directory, os.fsencode(first), directory, os.fsencode(second), flags) != 0:
        failure = ctypes.get_errno()
        raise OSError(failure, os.strerror(failure))


def _rename_exchange(directory, first, second):
    _rename_operation(directory, first, second, 2)


def _rename_noreplace(directory, first, second):
    _rename_operation(directory, first, second, 1)


def _bounded_names(directory, limit=256):
    names = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(names) == limit:
                raise BootstrapError("inventory exceeds its bounded entry limit; no complete inventory claimed")
            names.append(entry.name)
    return sorted(names)



# Installed-code removal is a closed-layout operation, not a general purge API.
_UNINSTALL_RECEIPT = re.compile(r"uninstall-([0-9a-f]{64})\.json\Z")
_UNINSTALL_DONE = re.compile(r"uninstalled-([0-9a-f]{64})\.json\Z")
_UNINSTALL_LIMIT = 2048


def _uninstall_kind(area, relative):
    parts = relative.split("/")
    if area == "bin":
        if relative == "multithread":
            return "alias"
        if relative == "relay" or re.fullmatch(r"\.relay-(switch|disabled)-[0-9a-f]{32}", relative):
            return "selector"
    elif area == "installation":
        if relative in ("releases", "launches"):
            return "directory"
        if len(parts) >= 2 and parts[0] == "releases" and _HEX.fullmatch(parts[1]):
            tail = "/".join(parts[2:])
            if tail in ("", "payload", "payload/relay_core", "payload/relay_runtime"):
                return "directory"
            if tail in ("bootstrap.py", "release.json") or tail.removeprefix("payload/") in PAYLOAD_FILES and tail.startswith("payload/"):
                return "file"
        if len(parts) >= 2 and parts[0] == "launches" and _ID.fullmatch(parts[1]):
            if len(parts) == 2:
                return "directory"
            if len(parts) == 3 and parts[2] in ("activation.json", "relay"):
                return "file"
    raise BootstrapError("unrecognized uninstall target; preserved: " + relative)


def _uninstall_identity(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid]


def _uninstall_object(parent, name, kind):
    """Observe one no-follow object; ctime is excluded because staging renames it."""
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    value = {"identity": _uninstall_identity(info)}
    if kind == "directory":
        _directory_info(info, private=True)
        if info.st_mode & 0o300 != 0o300:
            raise BootstrapError("uninstall directory is not owner-writable/searchable")
    elif kind in ("selector", "alias"):
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise BootstrapError("uninstall selector is not an owned single-link symlink")
        value.update(target=os.readlink(name, dir_fd=parent), mtime_ns=info.st_mtime_ns)
    else:
        value.update(sha256=hashlib.sha256(_read(parent, name)).hexdigest(),
                     size=info.st_size, mtime_ns=info.st_mtime_ns)
    after = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (not _same(info, after) or info.st_mode != after.st_mode or info.st_uid != after.st_uid
            or info.st_size != after.st_size or info.st_mtime_ns != after.st_mtime_ns
            or info.st_ctime_ns != after.st_ctime_ns):
        raise BootstrapError("uninstall target changed during observation")
    return value


def _uninstall_records(directory):
    """Validate immutable, account-owned receipts. Never execute candidate bytes."""
    names = _bounded_names(directory.fd)
    records = {}
    for name in names:
        match = _UNINSTALL_RECEIPT.fullmatch(name)
        if match is None:
            continue
        body = _read(directory.fd, name)
        value = _json(body)
        if (hashlib.sha256(body).hexdigest() != match[1]
                or set(value) != {"format", "installation", "bin_directory", "root_identity", "targets"}
                or type(value["format"]) is not int or value["format"] != 1
                or value["installation"] != str(directory.path)
                or value["root_identity"] != _uninstall_identity(os.fstat(directory.fd))
                or not isinstance(value["bin_directory"], str)
                or not Path(value["bin_directory"]).is_absolute()
                or type(value["targets"]) is not list or not 0 < len(value["targets"]) <= _UNINSTALL_LIMIT):
            raise BootstrapError("invalid uninstall receipt; preserved: " + name)
        seen = set()
        for entry in value["targets"]:
            if (not isinstance(entry, dict) or set(entry) != {"area", "path", "kind", "object"}
                    or not isinstance(entry["area"], str) or not isinstance(entry["path"], str)
                    or entry["kind"] != _uninstall_kind(entry["area"], entry["path"])
                    or not isinstance(entry["object"], dict)):
                raise BootstrapError("invalid uninstall receipt target")
            key = (entry["area"], entry["path"])
            if key in seen:
                raise BootstrapError("duplicate uninstall receipt target")
            seen.add(key)
            info = entry["object"]
            fields = {"identity"} | ({"target", "mtime_ns"} if entry["kind"] in ("selector", "alias")
                                    else {"sha256", "size", "mtime_ns"} if entry["kind"] == "file" else set())
            if (set(info) != fields or type(info["identity"]) is not list
                    or len(info["identity"]) != 4
                    or any(type(number) is not int or number < 0 for number in info["identity"])
                    or info["identity"][3] != os.getuid()):
                raise BootstrapError("invalid uninstall object identity")
            if entry["kind"] == "file" and (not isinstance(info["sha256"], str)
                    or not _HEX.fullmatch(info["sha256"]) or type(info["size"]) is not int
                    or not 0 <= info["size"] <= _MAX_MEMBER):
                raise BootstrapError("invalid uninstall content identity")
            if entry["kind"] != "directory" and type(info["mtime_ns"]) is not int:
                raise BootstrapError("invalid uninstall object timestamp")
            if entry["kind"] in ("selector", "alias") and not isinstance(info["target"], str):
                raise BootstrapError("invalid uninstall selector target")
        done = "uninstalled-" + match[1] + ".json"
        completed = done in names
        if completed and _read(directory.fd, done) != b"":
            raise BootstrapError("invalid uninstall completion marker; preserved: " + done)
        records[match[1]] = {"receipt": value, "completed": completed}
    for name in names:
        match = _UNINSTALL_DONE.fullmatch(name)
        if match and match[1] not in records:
            raise BootstrapError("uninstall completion has no matching receipt")
    if sum(not row["completed"] for row in records.values()) > 1:
        raise BootstrapError("multiple incomplete uninstalls require explicit review")
    return records


def _uninstall_stage(operation, index, relative):
    return str(Path(relative).parent / (".relay-uninstall-" + operation + "-" + str(index)))


def _uninstall_inventory(distribution, directory, records, pending=None):
    """Capture only closed-layout objects; unknowns block before removal."""
    metadata = {"activation.lock"}
    for identity, record in records.items():
        metadata.add("uninstall-" + identity + ".json")
        if record["completed"]:
            metadata.add("uninstalled-" + identity + ".json")
    stages = {}
    if pending:
        for index, entry in enumerate(records[pending]["receipt"]["targets"]):
            if entry["kind"] != "directory":
                stages[(entry["area"], _uninstall_stage(pending, index, entry["path"]))] = entry["kind"]
    entries = []
    def capture(area, parent, name, relative):
        kind = stages.get((area, relative)) or _uninstall_kind(area, relative)
        entries.append({"area": area, "path": relative, "kind": kind,
                        "object": _uninstall_object(parent, name, kind)})
        if len(entries) > _UNINSTALL_LIMIT:
            raise BootstrapError("uninstall exceeds its bounded target limit")
        if kind == "directory":
            child = directory.child(parent, name)
            for leaf in _bounded_names(child):
                capture(area, child, leaf, relative + "/" + leaf)
    for name in _bounded_names(directory.fd):
        if name not in metadata:
            capture("installation", directory.fd, name, name)
    try:
        bin_directory = _Directory(distribution.bin_directory, private_leaf=False)
    except FileNotFoundError:
        bin_directory = None
    if bin_directory:
        with bin_directory:
            for name in _bounded_names(bin_directory.fd):
                if (name in ("relay", "multithread") or name.startswith((".relay-switch-", ".relay-disabled-", ".relay-uninstall-"))):
                    capture("bin", bin_directory.fd, name, name)
            bin_directory.recheck()
    directory.recheck()
    return entries


def _uninstall_verify_complete(distribution, entries):
    """Bind the initial removal set to verified release and launch bytes."""
    objects = {(entry["area"], entry["path"]): entry for entry in entries}
    releases = {entry["path"].split("/")[1] for entry in entries
                if entry["area"] == "installation" and entry["path"].startswith("releases/")}
    launches = {entry["path"].split("/")[1] for entry in entries
                if entry["area"] == "installation" and entry["path"].startswith("launches/")}
    expected = {}
    for identity in releases:
        release = distribution.release(identity)
        base = "releases/" + identity + "/"
        expected[base + "bootstrap.py"] = release.bootstrap
        expected[base + "release.json"] = release.record
        expected.update({base + "payload/" + name: body for name, body in release.bodies.items()})
    for identity in launches:
        launch = distribution._read_launch(identity)
        record = {key: launch[key] for key in ("format", "activation_id", "previous_id", "release_id")}
        release = distribution.release(launch["release_id"])
        base = "launches/" + identity + "/"
        expected[base + "activation.json"] = _canonical(record)
        expected[base + "relay"] = _launcher_body(distribution.root, release.digest, release.bootstrap_sha, identity)
    for entry in entries:
        if entry["kind"] == "file":
            body = expected.get(entry["path"])
            if body is None or entry["object"]["sha256"] != hashlib.sha256(body).hexdigest():
                raise BootstrapError("uninstall code changed after release verification")
        elif entry["kind"] == "alias":
            if entry["object"]["target"] != str(distribution.bin_directory / "relay"):
                raise BootstrapError("unverified multithread command; preserved")
        elif entry["kind"] == "selector":
            target = Path(entry["object"]["target"])
            if (str(target) != entry["object"]["target"] or target.name != "relay"
                    or target.parent.parent != distribution.root / "launches"
                    or target.parent.name not in launches):
                raise BootstrapError("unverified retained selector; preserved: " + entry["path"])
    if any(("installation", name) not in objects for name in expected):
        raise BootstrapError("uninstall capture omitted a verified member")


class Distribution:
    """Public release pair installation. Root injection is internal tests only."""

    def __init__(self, root, bin_directory):
        self.root = Path(root)
        self.bin_directory = Path(bin_directory)

    @classmethod
    def for_account(cls):
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        if sys.platform != "linux" or os.getuid() != os.geteuid() or not home.is_absolute():
            raise BootstrapError("only an ordinary Linux account is supported")
        return cls(home / ".local/share/relay/installation", home / ".local/bin")

    def release(self, digest):
        if not isinstance(digest, str) or not _HEX.fullmatch(digest):
            raise BootstrapError("invalid installed release identity")
        return read_release(self.root / "releases" / digest, digest, private=True)

    def _command_alias(self):
        """Inspect the exact owned entry without following or executing its target.

        relay remains protocol one's single activation selector. The preferred
        name follows it, so upgrades cannot select two different runtimes.
        """
        try:
            directory = _Directory(self.bin_directory, private_leaf=False)
        except FileNotFoundError:
            return None
        with directory:
            try:
                info = os.stat("multithread", dir_fd=directory.fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if (not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1):
                raise BootstrapError("existing multithread command is not an owned Multithread entry; preserved")
            target = os.readlink("multithread", dir_fd=directory.fd)
            if target != str(self.bin_directory / "relay"):
                raise BootstrapError("existing multithread symlink is not the recognized command entry; preserved")
            after = os.stat("multithread", dir_fd=directory.fd, follow_symlinks=False)
            if (any(getattr(info, field) != getattr(after, field) for field in
                    ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"))
                    or os.readlink("multithread", dir_fd=directory.fd) != target):
                raise BootstrapError("multithread command changed during inspection")
            directory.recheck()
            return {"path": str(self.bin_directory / "multithread"), "target": target,
                    "device": info.st_dev, "inode": info.st_ino}

    def _ensure_command_alias(self, directory):
        if self._command_alias() is None:
            # No replacement: a concurrently created foreign entry is preserved.
            os.symlink(str(self.bin_directory / "relay"), "multithread", dir_fd=directory.fd)
            os.fsync(directory.fd)
            _checkpoint("command-alias-published")
        if self._command_alias() is None:
            raise BootstrapError("multithread command publication is unavailable; inspect before retrying")

    def _raw_selector(self):
        """Recognize only our exact selector shape without executing/requiring code."""
        try:
            directory = _Directory(self.bin_directory, private_leaf=False)
        except FileNotFoundError:
            return None
        with directory:
            try:
                info = os.stat("relay", dir_fd=directory.fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise BootstrapError("existing relay command is not an owned Multithread launcher; preserved")
            target = os.readlink("relay", dir_fd=directory.fd)
            path = Path(target)
            if (not path.is_absolute() or str(path) != target or path.name != "relay"
                    or path.parent.parent != self.root / "launches"
                    or not _ID.fullmatch(path.parent.name)):
                raise BootstrapError("existing relay symlink is not a recognized installation; preserved")
            after = os.stat("relay", dir_fd=directory.fd, follow_symlinks=False)
            if (any(getattr(info, field) != getattr(after, field) for field in
                    ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"))
                    or os.readlink("relay", dir_fd=directory.fd) != target):
                raise BootstrapError("launcher changed during inspection")
            directory.recheck()
            value = {"activation_id": path.parent.name, "device": info.st_dev,
                     "inode": info.st_ino, "target": target,
                     "mode": info.st_mode, "uid": info.st_uid, "links": info.st_nlink,
                     "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}
            return {**value, "observation": hashlib.sha256(_canonical(value)).hexdigest()}

    def _read_launch(self, identity):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise BootstrapError("invalid launcher identity")
        with _Directory(self.root / "launches" / identity) as launch:
            if set(_bounded_names(launch.fd)) != {"activation.json", "relay"}:
                raise BootstrapError("launcher has unknown or missing members")
            activation = _json(_read(launch.fd, "activation.json", limit=65536))
            if (set(activation) != {"format", "activation_id", "previous_id", "release_id"}
                    or type(activation["format"]) is not int or activation["format"] != 1
                    or activation["activation_id"] != identity
                    or not isinstance(activation["release_id"], str)
                    or not _HEX.fullmatch(activation["release_id"])
                    or (activation["previous_id"] is not None
                        and (not isinstance(activation["previous_id"], str)
                             or not _ID.fullmatch(activation["previous_id"])))):
                raise BootstrapError("launcher activation identity is invalid")
            release = self.release(activation["release_id"])
            body = _read(launch.fd, "relay")
            expected = _launcher_body(self.root, release.digest, release.bootstrap_sha, identity)
            mode = os.stat("relay", dir_fd=launch.fd, follow_symlinks=False).st_mode
            if body != expected or stat.S_IMODE(mode) != 0o700:
                raise BootstrapError("launcher bytes or execution mode changed")
            launch.recheck()
            return {**activation, "version": release.version}

    def _inspect(self):
        selector = self._raw_selector()
        if selector is None:
            return None
        activation = self._read_launch(selector["activation_id"])
        after = self._raw_selector()
        if after is None or after["observation"] != selector["observation"]:
            raise BootstrapError("launcher changed during inspection")
        return {**activation, **{name: selector[name] for name in ("target", "device", "inode")}}

    def status(self):
        active = self._inspect()
        alias = self._command_alias()
        return {"installed": active is not None, "activation": active,
                "launcher": str(self.bin_directory / "multithread"),
                "compatibility_launcher": str(self.bin_directory / "relay"),
                "preferred_command_available": alias is not None and active is not None,
                "enrollment_changed": False, "hooks_changed": False}

    def inspect(self):
        """Read-only diagnosis; invalid state is degraded, never empty."""
        result = {"state": "absent", "selector": None, "activation": None,
                  "releases": [], "launches": [], "retained_selectors": [],
                  "issues": [], "recovery_options": [],
                  "launcher": str(self.bin_directory / "multithread"),
                  "compatibility_launcher": str(self.bin_directory / "relay"),
                  "command_alias": None,
                  "installation": str(self.root), "writes": [],
                  "enrollment_changed": False, "hooks_changed": False}
        def issue(scope, exc):
            result["issues"].append({"scope": scope, "code": "unverified",
                                     "detail": str(exc)[:512]})
        try:
            result["command_alias"] = self._command_alias()
        except (OSError, BootstrapError) as exc:
            issue("command-alias", exc)
        try:
            result["selector"] = self._raw_selector()
            if result["selector"] is not None:
                result["activation"] = self._inspect()
        except (OSError, BootstrapError) as exc:
            issue("launcher", exc)
        try:
            bin_directory = _Directory(self.bin_directory, private_leaf=False)
        except FileNotFoundError:
            bin_directory = None
        except (OSError, BootstrapError) as exc:
            bin_directory = None
            issue("retained-selectors", exc)
        if bin_directory is not None:
            with bin_directory:
                try:
                    for name in _bounded_names(bin_directory.fd):
                        if not name.startswith((".relay-switch-", ".relay-disabled-")):
                            continue
                        info = os.stat(name, dir_fd=bin_directory.fd, follow_symlinks=False)
                        entry = {"path": str(self.bin_directory / name), "state": "retained-unverified",
                                 "device": info.st_dev, "inode": info.st_ino,
                                 "kind": "symlink" if stat.S_ISLNK(info.st_mode) else "other"}
                        if stat.S_ISLNK(info.st_mode):
                            entry["target"] = os.readlink(name, dir_fd=bin_directory.fd)
                        result["retained_selectors"].append(entry)
                    bin_directory.recheck()
                except (OSError, BootstrapError) as exc:
                    issue("retained-selectors", exc)
        try:
            root = _Directory(self.root)
        except FileNotFoundError:
            root = None
        except (OSError, BootstrapError) as exc:
            root = None
            issue("installation", exc)
        if root is not None:
            result["state"] = "inactive"
            with root:
                try:
                    _inspect_lock(root)
                except (OSError, BootstrapError) as exc:
                    issue("activation-lock", exc)
                try:
                    uninstall_records = _uninstall_records(root)
                    result["uninstall_operations"] = [
                        {"receipt": str(self.root / ("uninstall-" + identity + ".json")),
                         "completed": row["completed"]} for identity, row in sorted(uninstall_records.items())]
                    uninstall_metadata = {"uninstall-" + identity + ".json" for identity in uninstall_records}
                    uninstall_metadata.update("uninstalled-" + identity + ".json"
                                              for identity, row in uninstall_records.items() if row["completed"])
                    if any(not row["completed"] for row in uninstall_records.values()):
                        issue("uninstall", BootstrapError("installed-code removal is incomplete; use uninstall-plan"))
                    for name in _bounded_names(root.fd):
                        if name not in {"activation.lock", "releases", "launches"} | uninstall_metadata:
                            issue("installation/" + name, BootstrapError("unknown member preserved"))
                    for folder, key, pattern in (("releases", "release_id", _HEX),
                                                 ("launches", "activation_id", _ID)):
                        try:
                            child = root.child(root.fd, folder)
                        except FileNotFoundError:
                            continue
                        except (OSError, BootstrapError) as exc:
                            issue(folder, exc)
                            continue
                        for name in _bounded_names(child):
                            entry = {key: name, "state": "unverified"}
                            try:
                                if not pattern.fullmatch(name):
                                    raise BootstrapError("unknown member name; preserved")
                                if folder == "releases":
                                    release = self.release(name)
                                    entry["version"] = release.version
                                else:
                                    launch = self._read_launch(name)
                                    entry["release_id"] = launch["release_id"]
                                entry["state"] = "verified"
                            except (OSError, BootstrapError) as exc:
                                issue(folder + "/" + name, exc)
                            result[folder].append(entry)
                    root.recheck()
                except (OSError, BootstrapError) as exc:
                    issue("inventory", exc)
        if result["activation"] is not None:
            result["state"] = "active"
            if result["command_alias"] is None:
                issue("command-alias", BootstrapError("preferred multithread entry is unavailable; use the reviewed release installer to restore it"))
        if result["issues"]:
            result["state"] = "degraded"
        # These are choices, not automatic retries or an authorization receipt.
        selector = result["selector"]
        launcher_unknown = selector is None and any(
            row["scope"] == "launcher" for row in result["issues"])
        lock_unavailable = any(row["scope"] in ("activation-lock", "uninstall") for row in result["issues"])
        if not launcher_unknown and root is not None and not lock_unavailable:
            for release in result["releases"]:
                if release["state"] != "verified":
                    continue
                option = {"release_id": release["release_id"]}
                if selector is not None and result["activation"] is None:
                    option.update(command="recover", expected_selector=selector["observation"])
                else:
                    option.update(command="activate", expected_activation=(
                        result["activation"]["activation_id"] if result["activation"] else None))
                result["recovery_options"].append(option)
        # Inventory is bounded, best-effort diagnosis rather than a transaction.
        result["snapshot_is_authorization"] = False
        return result

    def uninstall_plan(self):
        """Read-only exact installed-code plan, including resumable remaining objects."""
        result = {"scope": "installed-code", "state": "blocked", "can_uninstall": False,
                  "uninstalled": False, "installation": str(self.root),
                  "launcher": str(self.bin_directory / "multithread"),
                  "compatibility_launcher": str(self.bin_directory / "relay"), "targets": [], "observed": [],
                  "pending_receipt": None, "root_identity": None, "retained_metadata": [],
                  "issues": [], "writes": [], "enrollment_changed": False, "hooks_changed": False,
                  "stops_running_commands": False, "artifact_purge_complete": False}
        try:
            try:
                root = _Directory(self.root)
            except FileNotFoundError:
                root = None
            if root is None:
                if self._raw_selector() is not None or self._command_alias() is not None:
                    raise BootstrapError("installation root is missing; selector is preserved")
                try:
                    with _Directory(self.bin_directory, private_leaf=False) as folder:
                        if any(name.startswith((".relay-switch-", ".relay-disabled-", ".relay-uninstall-"))
                               for name in _bounded_names(folder.fd)):
                            raise BootstrapError("retained selectors cannot be verified without their installation")
                except FileNotFoundError:
                    pass
                result.update(state="absent", can_uninstall=True, uninstalled=True)
            else:
                with root:
                    _inspect_lock(root)
                    # Keeping this stable lock avoids creating a second management
                    # lock domain while code is removed or a retry is inspected.
                    os.stat("activation.lock", dir_fd=root.fd, follow_symlinks=False)
                    if os.fstat(root.fd).st_mode & 0o300 != 0o300:
                        raise BootstrapError("installation root is not owner-writable/searchable")
                    records = _uninstall_records(root)
                    for record in records.values():
                        if record["receipt"]["bin_directory"] != str(self.bin_directory):
                            raise BootstrapError("uninstall receipt belongs to another command directory")
                    pending = next((key for key, row in records.items() if not row["completed"]), None)
                    observed = _uninstall_inventory(self, root, records, pending)
                    if pending:
                        targets = records[pending]["receipt"]["targets"]
                        allowed = {}
                        for index, entry in enumerate(targets):
                            allowed[(entry["area"], entry["path"])] = (index, entry)
                            if entry["kind"] != "directory":
                                allowed[(entry["area"], _uninstall_stage(pending, index, entry["path"]))] = (index, entry)
                        seen = set()
                        for entry in observed:
                            approved = allowed.get((entry["area"], entry["path"]))
                            if (approved is None or approved[0] in seen
                                    or entry["kind"] != approved[1]["kind"]
                                    or entry["object"] != approved[1]["object"]):
                                raise BootstrapError("uninstall remaining object changed; preserved: " + entry["path"])
                            seen.add(approved[0])
                    else:
                        _uninstall_verify_complete(self, observed)
                        targets = sorted(observed, key=lambda row: (
                            0 if row["kind"] in ("selector", "alias") else 1 if row["kind"] == "file" else 2,
                            -row["path"].count("/"), row["area"], row["path"]))
                    result.update(state="in_progress" if pending else "ready" if targets else "complete",
                                  can_uninstall=True, uninstalled=not targets and not pending,
                                  targets=targets, observed=sorted(observed, key=lambda row: (row["area"], row["path"])),
                                  pending_receipt=pending, root_identity=_uninstall_identity(os.fstat(root.fd)))
                    result["retained_metadata"] = [str(self.root / "activation.lock")]
                    for identity, row in sorted(records.items()):
                        result["retained_metadata"].append(str(self.root / ("uninstall-" + identity + ".json")))
                        if row["completed"]:
                            result["retained_metadata"].append(str(self.root / ("uninstalled-" + identity + ".json")))
        except (OSError, BootstrapError) as exc:
            result.update(state="blocked", can_uninstall=False, uninstalled=False)
            result["issues"].append({"scope": "uninstall", "code": "blocked", "detail": str(exc)[:1024]})
        result["expected_plan"] = hashlib.sha256(_canonical(result)).hexdigest() if result["can_uninstall"] else None
        return result

    def uninstall(self, *, expected_plan):
        """Remove approved installed code; never recurse over an arbitrary path."""
        def checked():
            plan = self.uninstall_plan()
            if (not isinstance(expected_plan, str) or not _HEX.fullmatch(expected_plan)
                    or not plan["can_uninstall"] or plan["expected_plan"] != expected_plan):
                raise BootstrapError("uninstall plan changed or is blocked; run uninstall-plan again")
            return plan
        plan = checked()
        if plan["uninstalled"]:
            return plan
        removed = []
        with _Directory(self.root) as root, _lock(root, allow_uninstall=True) as verify_lock:
            plan = checked()
            operation = plan["pending_receipt"]
            if operation is None:
                record = {"format": 1, "installation": str(self.root), "bin_directory": str(self.bin_directory),
                          "root_identity": plan["root_identity"], "targets": plan["targets"]}
                body = _canonical(record)
                if len(body) > _MAX_MEMBER:
                    raise BootstrapError("uninstall receipt exceeds its bounded size")
                operation = hashlib.sha256(body).hexdigest()
                _write_new(root.fd, "uninstall-" + operation + ".json", body)
                _uninstall_records(root)
            try:
                _checkpoint("uninstall-receipt-written")
                for index, entry in enumerate(plan["targets"]):
                    verify_lock()
                    base = self.root if entry["area"] == "installation" else self.bin_directory
                    relative = Path(entry["path"])
                    try:
                        parent = _Directory(base / relative.parent, private_leaf=entry["area"] == "installation")
                    except FileNotFoundError:
                        continue  # A prior completed removal can include this parent.
                    with parent:
                        name = relative.name
                        if entry["kind"] != "directory":
                            staged = Path(_uninstall_stage(operation, index, entry["path"])).name
                            try:
                                original = _uninstall_object(parent.fd, name, entry["kind"])
                            except FileNotFoundError:
                                original = None
                            if original is not None:
                                if original != entry["object"]:
                                    raise BootstrapError("uninstall target changed; preserved: " + str(base / relative))
                                _checkpoint("before-uninstall-stage")
                                _rename_noreplace(parent.fd, name, staged)
                                os.fsync(parent.fd)
                                _checkpoint("uninstall-staged")
                            name = staged
                        try:
                            observed = _uninstall_object(parent.fd, name, entry["kind"])
                        except FileNotFoundError:
                            continue
                        if observed != entry["object"]:
                            raise BootstrapError("uninstall staged object changed; preserved: " + str(base / relative.parent / name))
                        _checkpoint("before-uninstall-remove")
                        parent.recheck()
                        if _uninstall_object(parent.fd, name, entry["kind"]) != entry["object"]:
                            raise BootstrapError("uninstall object changed before removal; preserved: " + str(base / relative.parent / name))
                        if entry["kind"] == "directory":
                            os.rmdir(name, dir_fd=parent.fd)  # Empty only; never recursive.
                        else:
                            os.unlink(name, dir_fd=parent.fd)
                        os.fsync(parent.fd)
                        removed.append(str(base / relative))
                        _checkpoint("uninstall-object-removed")
                verify_lock()
                remaining = self.uninstall_plan()
                if not remaining["can_uninstall"] or remaining["observed"]:
                    raise BootstrapError("uninstall has changed or remaining objects; inspect the retained receipt")
                _checkpoint("uninstall-before-complete")
                # Empty completion marker cannot be left with a partially written body.
                _write_new(root.fd, "uninstalled-" + operation + ".json", b"")
                _checkpoint("uninstall-completed")
                verify_lock()
                result = self.uninstall_plan()
                if not result["uninstalled"]:
                    raise BootstrapError("installation changed before completion; inspect before retrying")
                result["removed"] = removed
                return result
            except (OSError, BootstrapError) as exc:
                return {"scope": "installed-code", "state": "partial", "uninstalled": False,
                        "removed": removed, "receipt": str(self.root / ("uninstall-" + operation + ".json")),
                        "detail": str(exc), "next_action": "run uninstall-plan using the reviewed source bootstrap",
                        "enrollment_changed": False, "hooks_changed": False,
                        "artifact_purge_complete": False, "stops_running_commands": False}


    def disable_plan(self):
        selector = self._raw_selector()
        issues = []
        if selector is not None:
            try:
                with _Directory(self.root) as root:
                    _inspect_lock(root)
            except (OSError, BootstrapError) as exc:
                issues.append({"scope": "installation-lock", "code": "unavailable", "detail": str(exc)[:512]})
        return {"can_disable": selector is not None and not issues, "issues": issues,
                "expected_selector": selector["observation"] if selector else None,
                "launcher": str(self.bin_directory / "relay"),
                "preferred_launcher": str(self.bin_directory / "multithread"),
                "preferred_alias_removed": False,
                "retained_selector_pattern": str(self.bin_directory / ".relay-disabled-<id>"),
                "retains": [str(self.root)], "removes_release_files": False,
                "enrollment_changed": False, "hooks_changed": False,
                "stops_running_commands": False, "writes": []}

    def _require_selector(self, observation):
        if not isinstance(observation, str) or not _HEX.fullmatch(observation):
            raise BootstrapError("a complete selector observation from inspect or disable-plan is required")
        current = self._raw_selector()
        if current is None or current["observation"] != observation:
            raise BootstrapError("selector changed or is absent; inspect before retrying")
        return current

    def disable(self, *, expected_selector):
        """Remove only the command selector, retaining all code and project data."""
        self._require_selector(expected_selector)
        with _Directory(self.root) as root, _lock(root) as verify_lock:
            current = self._require_selector(expected_selector)
            retained = ".relay-disabled-" + uuid.uuid4().hex
            with _Directory(self.bin_directory, private_leaf=False) as directory:
                verify_lock()
                self._require_selector(expected_selector)
                _checkpoint("before-launcher-disable")
                _rename_noreplace(directory.fd, "relay", retained)
                _checkpoint("launcher-disabled")
                os.fsync(directory.fd)
                moved = os.stat(retained, dir_fd=directory.fd, follow_symlinks=False)
                if ((moved.st_dev, moved.st_ino) != (current["device"], current["inode"])
                        or not stat.S_ISLNK(moved.st_mode)
                        or os.readlink(retained, dir_fd=directory.fd) != current["target"]):
                    raise BootstrapError("disable outcome is uncertain; displaced object preserved as " + retained)
                verify_lock()
                directory.recheck()
                try:
                    os.stat("relay", dir_fd=directory.fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise BootstrapError("launcher reappeared; inspect before retrying; retained as " + retained)
                return {"disabled": True, "retained_selector": str(self.bin_directory / retained),
                        "previous_selector": current, "retained_installation": str(self.root),
                        "removes_release_files": False, "enrollment_changed": False,
                        "hooks_changed": False, "stops_running_commands": False}

    def recover(self, digest, *, expected_selector):
        """Explicitly replace a recognized damaged launcher; never repair its code."""
        release = self.release(digest)
        self._command_alias()
        current = self._require_selector(expected_selector)
        with _Directory(self.root) as root, _lock(root) as verify_lock:
            return self._activate(root, verify_lock, release, current["activation_id"],
                                  recovery_selector=expected_selector)

    def _recovery_destination(self, root, release):
        """Read-only readiness; a damaged digest directory is never adopted."""
        _inspect_lock(root)
        if any(not row["completed"] for row in _uninstall_records(root).values()):
            raise BootstrapError("installed-code uninstall is incomplete; resume it before recovery")
        try:
            root.child(root.fd, "launches")
        except FileNotFoundError:
            pass  # A missing container can be reserved during explicit apply.
        try:
            releases = root.child(root.fd, "releases")
        except FileNotFoundError:
            root.recheck()
            return False
        try:
            os.stat(release.digest, dir_fd=releases, follow_symlinks=False)
        except FileNotFoundError:
            present = False
        else:
            try:
                self.release(release.digest)
            except (OSError, BootstrapError) as exc:
                raise BootstrapError("recovery release destination is damaged or incomplete; preserved; "
                                     "use a separately reviewed bundle with a distinct release identity") from exc
            present = True
        root.recheck()
        return present

    def recovery_plan(self, source, approved_digest):
        release = read_release(source, approved_digest)
        self._command_alias()
        current = self._raw_selector()
        if current is None:
            raise BootstrapError("recovery requires a recognized selector; inspect before choosing normal install")
        with _Directory(self.root) as root:
            present = self._recovery_destination(root, release)
            self._require_selector(current["observation"])
            root.recheck()
        return {
            "release_id": release.digest, "version": release.version,
            "bootstrap_sha256": release.bootstrap_sha,
            "runtime_sha256": hashlib.sha256(release.runtime_manifest).hexdigest(),
            "expected_selector": current["observation"], "release_already_installed": present,
            "launcher": str(self.bin_directory / "multithread"),
            "compatibility_launcher": str(self.bin_directory / "relay"),
            "writes": [str(self.root), str(self.bin_directory / "relay"), str(self.bin_directory / "multithread")],
            "retained_selector_pattern": str(self.bin_directory / ".relay-switch-<activation-id>"),
            "repairs_damaged_files": False, "enrollment_changed": False,
            "hooks_changed": False, "network_access": False, "stops_running_commands": False,
        }

    def recover_install(self, source, approved_digest, *, expected_selector):
        """Stage an approved external pair, preserving all damaged install objects."""
        release = read_release(source, approved_digest)  # capture before destination writes
        self._command_alias()
        current = self._require_selector(expected_selector)
        with _Directory(self.root) as root:
            self._recovery_destination(root, release)
            with _lock(root) as verify_lock:
                self._require_selector(expected_selector)
                self._recovery_destination(root, release)
                self._stage_release(root, verify_lock, release)
                result = self._activate(root, verify_lock, release, current["activation_id"],
                                        recovery_selector=expected_selector)
                return {**result, "recovered_from_bundle": True, "repairs_damaged_files": False,
                        "enrollment_changed": False, "hooks_changed": False,
                        "stops_running_commands": False}

    def plan(self, source, approved_digest):
        release = read_release(source, approved_digest)
        self._command_alias()
        current = self._inspect()
        # A corrupt/partial destination is never an empty installation.
        try:
            root = _Directory(self.root)
        except FileNotFoundError:
            root = None
        if root is not None:
            with root:
                _inspect_lock(root)
                try:
                    destination = root.child(root.fd, "releases")
                except FileNotFoundError:
                    destination = None
                if destination is not None:
                    try:
                        os.stat(release.digest, dir_fd=destination, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        try:
                            self.release(release.digest)
                        except FileNotFoundError as exc:
                            raise BootstrapError("installed release is incomplete; preserved for recovery") from exc
                root.recheck()
        return {
            "release_id": release.digest, "version": release.version,
            "bootstrap_sha256": release.bootstrap_sha,
            "runtime_sha256": hashlib.sha256(release.runtime_manifest).hexdigest(),
            "expected_activation": current["activation_id"] if current else None,
            "launcher": str(self.bin_directory / "multithread"),
            "compatibility_launcher": str(self.bin_directory / "relay"),
            "writes": [str(self.root), str(self.bin_directory / "relay"), str(self.bin_directory / "multithread")],
            "retained_selector_pattern": str(self.bin_directory / ".relay-switch-<activation-id>") if current else None,
            "enrolls_projects": False, "changes_hooks": False, "network_access": False,
        }

    def install(self, source, approved_digest, *, expected_activation):
        release = read_release(source, approved_digest)  # before destination writes
        self._command_alias()  # refuse a foreign preferred entry before staging
        self._check_expected(self._inspect(), expected_activation)
        with _Directory(self.root, create=True) as root, _lock(root) as verify_lock:
            self._check_expected(self._inspect(), expected_activation)
            self._stage_release(root, verify_lock, release)
            return self._activate(root, verify_lock, release, expected_activation)

    def _stage_release(self, root, verify_lock, release):
        releases = root.child(root.fd, "releases", create=True)
        try:
            os.mkdir(release.digest, 0o700, dir_fd=releases)
        except FileExistsError:
            self.release(release.digest)
        else:
            os.fsync(releases)
            _checkpoint("release-reserved")
            with _Directory(self.root / "releases" / release.digest) as destination:
                if os.listdir(destination.fd):
                    raise BootstrapError("reserved release destination is not empty")
                _write_release(destination, release)
        verify_lock()
        _checkpoint("release-installed")

    @staticmethod
    def _check_expected(current, expected):
        if expected is not None and (not isinstance(expected, str) or not _ID.fullmatch(expected)):
            raise BootstrapError("expected activation must be the exact ID or explicit none")
        actual = current["activation_id"] if current else None
        if actual != expected:
            raise BootstrapError("launcher activation changed; stale installation or rollback refused")

    def activate(self, digest, *, expected_activation):
        self._command_alias()
        release = self.release(digest)
        self._check_expected(self._inspect(), expected_activation)
        with _Directory(self.root) as root, _lock(root) as verify_lock:
            return self._activate(root, verify_lock, release, expected_activation)

    def _activate(self, root, verify_lock, release, expected, *, recovery_selector=None):
        def observe():
            return (self._require_selector(recovery_selector) if recovery_selector is not None
                    else self._inspect())
        current = observe()
        self._command_alias()
        self._check_expected(current, expected)
        launches = root.child(root.fd, "launches", create=True)
        identity = uuid.uuid4().hex
        os.mkdir(identity, 0o700, dir_fd=launches)
        os.fsync(launches)
        launch = root.child(launches, identity)
        activation = {"format": 1, "activation_id": identity,
                      "previous_id": expected, "release_id": release.digest}
        record = _canonical(activation)
        record_fd = root.retain_file(_write_new(launch, "activation.json", record, retain=True))
        body = _launcher_body(self.root, release.digest, release.bootstrap_sha, identity)
        fd = root.retain_file(_write_new(launch, "relay", body, retain=True))
        os.fchmod(fd, 0o700)
        os.fsync(fd)
        os.fsync(launch)
        def verify_prepared():
            _verify_publication(launch, "activation.json", record_fd, record)
            _verify_publication(launch, "relay", fd, body)
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                raise BootstrapError("prepared launcher execution mode changed")
        verify_prepared()
        verify_lock()
        _checkpoint("launcher-prepared")
        verify_prepared()
        target = str(self.root / "launches" / identity / "relay")
        retained_selector = None
        with _Directory(self.bin_directory, create=True, private_leaf=False) as bin_directory:
            self._check_expected(observe(), expected)
            verify_lock()
            verify_prepared()
            if current is None:
                # Atomic no-replace first publication; preserve any occupant.
                os.symlink(target, "relay", dir_fd=bin_directory.fd)
            else:
                temporary = ".relay-switch-" + identity
                os.symlink(target, temporary, dir_fd=bin_directory.fd)
                _checkpoint("before-launcher-exchange")
                verify_prepared()
                # Preserve the displaced object atomically. A same-account
                # namespace race cannot make an unknown file disappear.
                _rename_exchange(bin_directory.fd, temporary, "relay")
                retained_selector = str(self.bin_directory / temporary)
                displaced = os.stat(temporary, dir_fd=bin_directory.fd, follow_symlinks=False)
                if ((displaced.st_dev, displaced.st_ino) != (current["device"], current["inode"])
                        or not stat.S_ISLNK(displaced.st_mode)
                        or os.readlink(temporary, dir_fd=bin_directory.fd) != current["target"]):
                    os.fsync(bin_directory.fd)
                    raise BootstrapError("launcher exchange is uncertain; displaced object preserved as " + temporary)
                # Retain the displaced selector. Check-then-unlink could
                # delete a different object substituted after validation.
            # Publish the verified selector before exposing its preferred alias.
            # An interrupted first publication can be inspected through relay.
            self._ensure_command_alias(bin_directory)
            _checkpoint("launcher-published")
            os.fsync(bin_directory.fd)
            verify_lock()
            verify_prepared()
            bin_directory.recheck()
            observed = self._inspect()
            if self._command_alias() is None:
                raise BootstrapError("preferred command disappeared during activation; inspect before retrying")
            if (observed is None or observed["activation_id"] != identity
                    or observed["release_id"] != release.digest or observed["target"] != target):
                raise BootstrapError("launcher publication changed; inspect before retrying")
            return {"installed": True, "activation": observed, "enrolls_projects": False,
                    "changes_hooks": False, "network_access": False, "retained_selector": retained_selector}


def run_installed(release_id, activation_id, argv):
    installation = Distribution.for_account()
    current = installation._inspect()
    if current is None or (current["release_id"], current["activation_id"]) != (release_id, activation_id):
        raise BootstrapError("this launcher is no longer active; use the current multithread command")
    release = installation.release(release_id)
    if globals().get("__relay_bootstrap_sha256__") != release.bootstrap_sha:
        raise BootstrapError("installed execution requires the retained verified launcher")
    if argv == ["--version"]:
        print("Multithread " + release.version)
        return 0
    if argv and argv[0] == "runtime":
        return main(argv[1:])
    runtime = VerifiedRuntime(hashlib.sha256(release.runtime_manifest).hexdigest(),
                              installation.root / "releases" / release_id / "payload", release.bodies)
    runtime.install_importer()
    from relay_runtime.cli import main as dispatch
    return dispatch(argv, command_alias_check=installation._command_alias)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="multithread runtime", description="Offline, explicit Multithread release installation.")
    commands = parser.add_subparsers(dest="command", required=True)
    if not globals().get("__relay_bootstrap_sha256__"):
        build = commands.add_parser("build-release", help="build an unapproved closed local bundle")
        build.add_argument("--output", required=True)
        build.add_argument("--version", required=True)
    for name in ("plan", "install", "recover-plan", "recover-install"):
        action = commands.add_parser(name)
        action.add_argument("--release", required=True)
        action.add_argument("--approve-sha256", required=True)
        if name == "install":
            action.add_argument("--expected-activation", required=True)
        elif name == "recover-install":
            action.add_argument("--expected-selector", required=True)
    commands.add_parser("status")
    commands.add_parser("inspect", help="read-only bounded installation and recovery diagnosis")
    commands.add_parser("disable-plan", help="plan reversible command removal without writes")
    commands.add_parser("uninstall-plan", help="read-only exact installed-code removal plan")
    uninstall = commands.add_parser("uninstall", help="remove verified installed code, retaining data and receipt metadata")
    uninstall.add_argument("--expected-plan", required=True)
    disable = commands.add_parser("disable", help="retain code and data; remove only the recognized command selector")
    disable.add_argument("--expected-selector", required=True)
    recover = commands.add_parser("recover", help="explicitly select a verified retained release after launcher damage")
    recover.add_argument("--release-sha256", required=True)
    recover.add_argument("--expected-selector", required=True)
    rollback = commands.add_parser("activate", help="activate a retained release, including explicit rollback")
    rollback.add_argument("--release-sha256", required=True)
    rollback.add_argument("--expected-activation", required=True)
    args = parser.parse_args(argv)
    try:
        if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
            raise BootstrapError("run the reviewed bootstrap with /usr/bin/python3 -I -S -B")
        if args.command == "build-release":
            if globals().get("__relay_bootstrap_sha256__"):
                raise BootstrapError("build-release is a source-development command; use the reviewed source bootstrap")
            release = build_release(Path(__file__).parent, args.output, args.version)
            result = {"release_id": release.digest, "version": release.version,
                      "output": str(Path(args.output).absolute()), "approved": False}
        else:
            installation = Distribution.for_account()
            if args.command == "status":
                result = installation.status()
            elif args.command == "inspect":
                result = installation.inspect()
            elif args.command == "disable-plan":
                result = installation.disable_plan()
            elif args.command == "uninstall-plan":
                result = installation.uninstall_plan()
            elif args.command == "uninstall":
                result = installation.uninstall(expected_plan=args.expected_plan)
            elif args.command == "disable":
                result = installation.disable(expected_selector=args.expected_selector)
            elif args.command == "recover":
                result = installation.recover(args.release_sha256, expected_selector=args.expected_selector)
            elif args.command == "recover-plan":
                result = installation.recovery_plan(args.release, args.approve_sha256)
            elif args.command == "recover-install":
                result = installation.recover_install(args.release, args.approve_sha256,
                                                     expected_selector=args.expected_selector)
            elif args.command == "plan":
                result = installation.plan(args.release, args.approve_sha256)
            elif args.command == "install":
                result = installation.install(args.release, args.approve_sha256,
                                              expected_activation=None if args.expected_activation == "none" else args.expected_activation)
            else:
                result = installation.activate(args.release_sha256,
                                               expected_activation=None if args.expected_activation == "none" else args.expected_activation)
        print(json.dumps(result, sort_keys=True))
        if args.command == "uninstall" and not result["uninstalled"]:
            return 1
        return 0
    except (BootstrapError, OSError) as exc:
        print("multithread runtime: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
