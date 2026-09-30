"""Explicit account-owned workspace enrollment, without ledger initialization.

Internal Linux primitive: the OS account and installed bootstrap are trusted.
Repository contents and process environment cannot grant enrollment.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import stat
import subprocess
from typing import Any
import uuid


class EnrollmentError(RuntimeError):
    """Enrollment is unavailable or ambiguous; do not access a ledger."""


_REQUIREMENTS = {False: "group/other write access is not allowed",
                 True: "group/other access is not allowed for a private directory"}


class UnsafeDirectory(EnrollmentError):
    """A directory enrollment relies on, whose mode would let other users change it."""

    def __init__(self, path: Path, mode: int, private: bool):
        super().__init__("enrollment directory permissions are unsafe at "
                         f"{json.dumps(str(path), ensure_ascii=True)} "
                         f"(observed mode {mode:04o}); {_REQUIREMENTS[private]}")
        self.path, self.mode, self.private = path, mode, private


class NotEnrolled(EnrollmentError):
    """Positive absence: no account enrollment for this checkout, and no Multithread state in it."""

    # sysexits EX_CONFIG. Setup reads this status as a checkout not yet enrolled.
    exit_code = 78

    def __init__(self, root: Path):
        super().__init__("this checkout is not enrolled: " + json.dumps(str(root), ensure_ascii=True))
        self.root = root


def command_text(argv: list[str]) -> str:
    """A command to paste, or JSON when an argument could disturb the terminal."""
    if all(argument.isprintable() for argument in argv):
        return shlex.join(argv)
    return json.dumps(argv, ensure_ascii=True) + " (JSON argument list)"


_GIT = "/usr/bin/git"
_GIT_ENV = {
    "PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}
_MAX_JSON = 8192
_ID = re.compile(r"[0-9a-f]{32}\Z")
_AUTHORITY = re.compile(r"[0-9a-f]{64}\.json\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RETIREMENT = re.compile(r"retired-binding-([0-9a-f]{32})-([1-9][0-9]{0,3})-([0-9a-f]{64})\.json\Z")
_MAX_RETIREMENTS = 128
_MAX_REGISTRY_ENTRIES = 4096
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True)
class Workspace:
    root: Path
    common: Path
    common_device: int
    common_inode: int

    @property
    def state(self) -> Path:
        return self.common.parent / ".relay"


@dataclass(frozen=True)
class Enrollment:
    enrollment_id: str
    generation: int
    workspace: Workspace
    state_device: int
    state_inode: int


def default_registry_root() -> Path:
    """Use the OS account, never an ambient HOME/XDG override."""
    if os.getuid() != os.geteuid():
        raise EnrollmentError("set-user-ID execution is unsupported")
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not account_home.is_absolute():
        raise EnrollmentError("the OS account has no absolute home directory")
    return account_home / ".local" / "share" / "relay" / "enrollments"


def _absolute(path: str | os.PathLike[str]) -> Path:
    # Inspect before normalization: alias/../project must not erase a symlink.
    result = Path(path)
    if not result.is_absolute():
        result = Path.cwd() / result
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for index, part in enumerate(result.parts[1:], 1):
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if ".." in result.parts[index:]:
                    raise EnrollmentError("missing path before parent traversal")
                break
            os.close(fd)
            fd = child
    except OSError as exc:
        raise EnrollmentError("symlinked or unavailable enrollment path") from exc
    finally:
        os.close(fd)
    return Path(os.path.abspath(result))


def _unsafe_mode(path: Path, info: os.stat_result, private: bool) -> bool:
    """The one mode rule: no group/other write, and no group/other access when private.

    Root's sticky /tmp and /var/tmp are allowed as ancestors of a shared directory.
    """
    system_temporary = (path in (Path("/tmp"), Path("/var/tmp"))
                        and info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
    return (bool(stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022))
            and not (system_temporary and not private))


def _open_chain(path: Path, opened: dict[Path, int]) -> list[Path]:
    """Open path's directories from /, each through its opened parent without following a symlink.

    Returns the directories reached; it ends early at a missing path, a symlink
    or a file. Descriptors stay in opened, shared by every chain, so each
    directory is judged once through the parent it was reached from.
    """
    if Path("/") not in opened:
        opened[Path("/")] = os.open("/", _DIRECTORY_FLAGS)
    chain = [Path("/")]
    for part in path.parts[1:]:
        child = chain[-1] / part
        if child not in opened:
            try:
                opened[child] = os.open(part, _DIRECTORY_FLAGS, dir_fd=opened[chain[-1]])
            except OSError:
                break
        chain.append(child)
    return chain


def _has_entry(directory: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _read_entry(directory: int, name: str) -> str | None:
    """A small regular file in an opened directory, read without following a symlink."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    except OSError:
        return None
    try:
        body = os.read(fd, _MAX_JSON + 1) if stat.S_ISREG(os.fstat(fd).st_mode) else b""
    finally:
        os.close(fd)
    return os.fsdecode(body).rstrip("\n") if 0 < len(body) <= _MAX_JSON else None


def _git_chain(root: Path, opened: dict[Path, int]) -> tuple[list[Path], Path | None]:
    """The Git directories enrollment checks for this checkout, and its state if present.

    A linked worktree counts only when its .git pointer and Git's backlink name
    each other, as enrollment requires; both are read from opened directories.
    """
    try:
        marker = os.stat(".git", dir_fd=opened[root], follow_symlinks=False)
    except OSError:
        return [], None
    if stat.S_ISDIR(marker.st_mode):
        git_dir = common = root / ".git"
    else:
        pointer = _read_entry(opened[root], ".git") if stat.S_ISREG(marker.st_mode) else None
        if pointer is None or not pointer.startswith("gitdir: "):
            return [], None
        target = Path(pointer[len("gitdir: "):])
        git_dir = Path(os.path.normpath(target if target.is_absolute() else root / target))
        common = git_dir.parent.parent
        if git_dir.parent.name != "worktrees" or common.name != ".git":
            return [], None
    chain = _open_chain(git_dir, opened)
    if chain[-1] != git_dir or (git_dir != common and _read_entry(opened[git_dir], "gitdir") != str(root / ".git")):
        return [], None
    state = common.parent / ".relay"
    return chain, state if _open_chain(state, opened)[-1] == state else None


def unsafe_directories(repo: str | os.PathLike[str], registry_root: Path) -> list[tuple[Path, int, bool]]:
    """Every existing directory enrollment would refuse for its mode, in checking order.

    A diagnostic, so one refusal can name every fix. It writes, runs and
    decides nothing: enrollment still validates each directory it holds. Each
    directory is opened through its opened parent without following a symlink,
    and judged by that descriptor. The requested path must open whole before any
    Git metadata inside it is read; otherwise the diagnostic adds nothing.
    """
    requested = Path(os.path.abspath(Path.cwd() / repo))
    opened: dict[Path, int] = {}
    try:
        chain = _open_chain(requested, opened)
        if chain[-1] != requested:
            return []
        root = next((path for path in reversed(chain) if _has_entry(opened[path], ".git")), None)
        git, state = _git_chain(root, opened) if root is not None else ([], None)
        # The registry path comes from the account, not the checkout; a missing tail is normal.
        private = {registry_root, state}
        found, seen = [], set()
        for path in (*chain, *git, *([state] if state else []), *_open_chain(registry_root, opened)):
            if path in seen:
                continue
            seen.add(path)
            info = os.fstat(opened[path])
            if info.st_uid in (0, os.getuid()) and _unsafe_mode(path, info, path in private):
                found.append((path, stat.S_IMODE(info.st_mode), path in private))
        return found
    finally:
        for fd in opened.values():
            os.close(fd)


def permission_refusal(repo: str | os.PathLike[str], registry_root: Path,
                       refused: UnsafeDirectory) -> EnrollmentError:
    """One refusal naming every unsafe directory with its exact fix; the rule itself is unchanged."""
    try:
        found = unsafe_directories(repo, registry_root)
    except (OSError, ValueError):
        found = []
    if all(path != refused.path for path, _, _ in found):
        found.insert(0, (refused.path, refused.mode, refused.private))
    lines = ["enrollment directory permissions are unsafe: other users could change "
             + ("1 directory" if len(found) == 1 else f"{len(found)} directories")
             + " this checkout's enrollment relies on, so Multithread refuses it. Run each command "
             "below (it changes only the directory it names), then check again:"]
    for path, mode, private in found:
        fix = command_text(["chmod", "g-rwx,o-rwx" if private else "g-w,o-w", str(path)])
        lines.append(f"  {fix}   (observed mode {mode:04o}; {_REQUIREMENTS[private]})")
    if any(not private and mode & 0o020 for _, mode, private in found):
        lines.append("A umask of 002 creates directories with group write, so a fresh clone can start this way.")
    return EnrollmentError("\n".join(lines))


def _held_unless_absent(path: Path, custody: _Custody, *, private: bool = False) -> _Directory | None:
    """Hold path through controlled ancestry, or None where a component is positively absent."""
    directory = custody.get(Path("/"))
    for part in path.parts[1:]:
        if not directory.exists(part):
            return None
        directory = custody.get(directory.path / part)
    return custody.get(path, private=private)


def _unenrolled(workspace: Workspace, custody: _Custody) -> None:
    """Refuse an absent record by what the checkout holds; absence never means empty state."""
    if (custody.get(workspace.state.parent).exists(workspace.state.name)
            or custody.get(workspace.common).exists("relay-enrollment.json")):
        raise EnrollmentError("this checkout holds Multithread state, but this account has no enrollment "
                              "for its path; preserve that state and inspect it before enrolling (a checkout "
                              "moved on the same filesystem can use rebind-plan)")
    raise NotEnrolled(workspace.root)


def _fingerprint(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _file_stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _checkpoint(stage: str) -> None:
    """Internal deterministic abrupt-exit seam; never a recovery mechanism."""


def _inventory_names(directory, prefix):
    names = []
    with os.scandir(directory.fd) as entries:
        for count, entry in enumerate(entries, 1):
            if count > _MAX_REGISTRY_ENTRIES:
                raise EnrollmentError("account binding inventory exceeds its supported bound")
            if entry.name.startswith(prefix):
                names.append(entry.name)
                if len(names) > _MAX_RETIREMENTS:
                    raise EnrollmentError("binding retirement history exceeds its supported bound")
    return tuple(sorted(names))


@dataclass
class _Directory:
    custody: _Custody
    path: Path
    fd: int
    parent: _Directory | None
    initial: os.stat_result
    private: bool = False
    account: bool = False

    def validate(self, info: os.stat_result) -> None:
        if (not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in (0, os.getuid())
                or (self.account and info.st_uid != os.getuid())):
            raise EnrollmentError("directory ancestry is not controlled by this account or root")
        if _unsafe_mode(self.path, info, self.private):
            raise UnsafeDirectory(self.path, stat.S_IMODE(info.st_mode), self.private)

    def exists(self, name: str) -> bool:
        self.custody.verify()
        try:
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False


class _Custody:
    """Retain every ancestry descriptor until the operation and final checks end.

    Names are checked relative to retained parents; actual reads/writes/fsyncs
    never reopen an absolute pathname. A concurrent rename can leave an orphan
    in the original held directory, but cannot redirect writes through its
    replacement. This does not lock the namespace against the trusted account.
    """

    def __init__(self):
        self.directories: dict[Path, _Directory] = {}
        self.files: list[tuple[_Directory, str, int, tuple[int, ...]]] = []
        self.inventories = {}

    def __enter__(self) -> _Custody:
        return self

    def __exit__(self, *_args) -> None:
        for _, _, fd, _ in reversed(self.files):
            os.close(fd)
        for directory in reversed(list(self.directories.values())):
            os.close(directory.fd)

    def get(self, path: Path, *, create: bool = False,
            private: bool = False, account: bool = False) -> _Directory:
        if path in self.directories:
            directory = self.directories[path]
            directory.private |= private
            directory.account |= account or private
            self.verify()
            return directory
        parent = None if path == Path("/") else self.get(path.parent, create=create)
        created = False
        self.verify()
        try:
            fd = os.open(path.name if parent else "/", _DIRECTORY_FLAGS,
                         dir_fd=parent.fd if parent else None)
        except FileNotFoundError:
            if not create or parent is None:
                raise
            try:
                os.mkdir(path.name, mode=0o700, dir_fd=parent.fd)
            except FileExistsError:
                pass
            fd = os.open(path.name, _DIRECTORY_FLAGS, dir_fd=parent.fd)
            created = True
        directory = _Directory(self, path, fd, parent, os.fstat(fd),
                               private or created, account or private or created)
        self.directories[path] = directory
        self.verify()
        if created:
            _checkpoint("registry-directory-created")
            _fsync_directory(parent)
        return directory

    def verify(self) -> None:
        for directory in self.directories.values():
            info = os.fstat(directory.fd)
            directory.validate(info)
            named = (os.stat(directory.path.name, dir_fd=directory.parent.fd,
                             follow_symlinks=False) if directory.parent
                     else os.stat("/", follow_symlinks=False))
            if (_fingerprint(info) != _fingerprint(directory.initial)
                    or _fingerprint(named) != _fingerprint(info)
                    or not stat.S_ISDIR(named.st_mode)):
                raise EnrollmentError("enrollment directory changed during operation")
        for directory, name, fd, expected in self.files:
            named = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
            if _file_stamp(os.fstat(fd)) != expected or _file_stamp(named) != expected:
                raise EnrollmentError("enrollment identity file changed during operation")
        for (_, prefix), (directory, expected) in self.inventories.items():
            if _inventory_names(directory, prefix) != expected:
                raise EnrollmentError("binding retirement inventory changed during operation")

    def watch_inventory(self, directory, prefix):
        self.verify()
        names = _inventory_names(directory, prefix)
        key = (directory.path, prefix)
        if key in self.inventories and self.inventories[key][1] != names:
            raise EnrollmentError("binding retirement inventory changed during observation")
        self.inventories[key] = (directory, names)
        self.verify()
        return names

    def renamed_files(self, directory, moves, *, published_guard=None):
        """Transfer only verified intentional renames; retain every old descriptor."""
        updated = []
        for parent, name, fd, expected in self.files:
            if parent is directory and name in moves:
                destination, payload = moves[name]
                held = os.fstat(fd)
                named = os.stat(destination, dir_fd=directory.fd, follow_symlinks=False)
                # Rename may change ctime, but no other observed file property.
                if (_file_stamp(held)[:-1] != expected[:-1]
                        or _file_stamp(named) != _file_stamp(held)
                        or os.pread(fd, _MAX_JSON + 1, 0) != payload):
                    raise EnrollmentError("rebind publication changed; retained objects require inspection")
                updated.append((parent, destination, fd, _file_stamp(held)))
            else:
                updated.append((parent, name, fd, expected))
        self.files = updated
        if published_guard is not None:
            prefix, name = published_guard
            key = (directory.path, prefix)
            prior = self.inventories[key][1]
            expected = tuple(sorted((*prior, name)))
            if name in prior or _inventory_names(directory, prefix) != expected:
                raise EnrollmentError("retirement publication inventory is ambiguous")
            self.inventories[key] = (directory, expected)
        self.verify()


def _read_bytes(directory: _Directory, name: str, *, private: bool = True) -> bytes:
    directory.custody.verify()
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                 dir_fd=directory.fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_JSON
                or (private and (info.st_uid != os.getuid()
                                 or stat.S_IMODE(info.st_mode) & 0o077
                                 or info.st_nlink != 1))):
            raise EnrollmentError("enrollment identity is not a supported regular file")
        chunks = []
        remaining = _MAX_JSON + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > _MAX_JSON:
            raise EnrollmentError("enrollment identity file is too large")
    except BaseException:
        os.close(fd)
        raise
    directory.custody.files.append((directory, name, fd, _file_stamp(info)))
    directory.custody.verify()
    return payload


def _git_identity(directory: _Directory) -> tuple[Path, Path, Path]:
    directory.custody.verify()
    try:
        result = subprocess.run(
            [_GIT, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-C", f"/proc/self/fd/{directory.fd}", "rev-parse", "--path-format=absolute",
             "--git-common-dir", "--show-toplevel", "--absolute-git-dir", "--is-bare-repository"],
            env=_GIT_ENV, input="", capture_output=True,
            pass_fds=(directory.fd,), text=True, timeout=10, check=True,
        )
    except subprocess.CalledProcessError as exc:
        if "not a git repository" in (exc.stderr or "").lower():
            raise EnrollmentError(f"{directory.path} is not a Git checkout: run from an enrolled checkout "
                                  "or pass --repo <checkout>") from exc
        raise EnrollmentError(f"Git could not inspect {directory.path} as a checkout: run from an enrolled "
                              "checkout or pass --repo <checkout>") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise EnrollmentError(f"cannot resolve a supported Git workspace at {directory.path} "
                              f"({exc.__class__.__name__})") from exc
    directory.custody.verify()
    lines = result.stdout.splitlines()
    if len(lines) != 4 or lines[3] != "false":
        raise EnrollmentError("bare or ambiguous Git workspaces are unsupported")
    common, root, git_dir = (_absolute(value) for value in lines[:3])
    return common, root, git_dir


def _resolve_workspace(repo: str | os.PathLike[str], custody: _Custody) -> Workspace:
    requested = custody.get(_absolute(repo))
    common, root, git_dir = _git_identity(requested)
    if common.name != ".git":
        raise EnrollmentError("separate Git directories are unsupported")
    common_directory = custody.get(common, account=True)
    root_directory = custody.get(root)
    owner_common, owner_root, owner_git_dir = _git_identity(common_directory.parent)
    if owner_common != common or owner_root != common.parent or owner_git_dir != common:
        raise EnrollmentError("Git common directory has no unambiguous owner")
    if root == common.parent:
        if git_dir != common:
            raise EnrollmentError("main checkout has an unexpected Git directory")
    else:
        if git_dir.parent != common / "worktrees":
            raise EnrollmentError("requested directory is not a registered linked worktree")
        backlink = os.fsdecode(_read_bytes(custody.get(git_dir), "gitdir", private=False)).rstrip("\n")
        pointer = os.fsdecode(_read_bytes(root_directory, ".git", private=False)).rstrip("\n")
        if backlink != str(root / ".git") or not pointer.startswith("gitdir: "):
            raise EnrollmentError("Git worktree backlinks do not match")
        forward = Path(pointer[len("gitdir: "):])
        if _absolute(forward if forward.is_absolute() else root / forward) != git_dir:
            raise EnrollmentError("Git worktree forward pointer changed")
    custody.verify()
    info = common_directory.initial
    return Workspace(root, common, info.st_dev, info.st_ino)


def resolve_workspace(repo: str | os.PathLike[str]) -> Workspace:
    try:
        with _Custody() as custody:
            return _resolve_workspace(repo, custody)
    except OSError as exc:
        raise EnrollmentError("Git workspace identity is unavailable") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EnrollmentError("duplicate enrollment JSON field")
        result[key] = value
    return result


def _read_json(directory: _Directory, name: str) -> dict[str, Any]:
    payload = _read_bytes(directory, name)
    try:
        result = json.loads(payload, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as exc:
        raise EnrollmentError("enrollment record is invalid JSON") from exc
    if not isinstance(result, dict):
        raise EnrollmentError("enrollment record must be an object")
    return result


def _fsync_directory(directory: _Directory) -> None:
    directory.custody.verify()
    os.fsync(directory.fd)
    directory.custody.verify()


def _canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _rename_record(directory, source, destination, flags):
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        operation = libc.renameat2
    except AttributeError as exc:
        raise EnrollmentError("atomic binding publication is unsupported") from exc
    operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                          ctypes.c_char_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    if operation(directory.fd, os.fsencode(source), directory.fd,
                 os.fsencode(destination), flags) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _prepare_record(directory, name, value):
    directory.custody.verify()
    payload = _canonical(value)
    if len(payload) > _MAX_JSON:
        raise EnrollmentError("binding record is too large")
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory.fd)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if not written:
                raise OSError("short binding record write")
            offset += written
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        raise  # Preserve any partial object; never guess that it is disposable.
    directory.custody.files.append((directory, name, fd, _file_stamp(os.fstat(fd))))
    directory.custody.verify()
    return payload


def _absent(path, custody):
    """Require absence through held, controlled ancestry, even if a parent moved."""
    directory = custody.get(Path("/"))
    for part in path.parts[1:]:
        if not directory.exists(part):
            return
        directory = custody.get(directory.path / part)
    raise EnrollmentError("old repository path is occupied; preserve it and inspect")


def _check_rebind_lock(directory, name):
    if directory.exists(name):
        payload = _read_bytes(directory, name)
        fd = directory.custody.files[-1][2]
        if payload or stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise EnrollmentError("binding lock is not an empty private writable file")


@contextmanager
def _rebind_lock(directory, name):
    directory.custody.verify()
    created = False
    try:
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory.fd)
        created = True
    except FileExistsError:
        fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=directory.fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size):
            raise EnrollmentError("binding lock is unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A second retained descriptor lets custody verify the named lock, while
        # this descriptor independently retains the cooperative lock itself.
        _check_rebind_lock(directory, name)
        if _fingerprint(os.fstat(directory.custody.files[-1][2])) != _fingerprint(info):
            raise EnrollmentError("binding lock changed")
        if created:
            os.fsync(fd)
            _fsync_directory(directory)
        yield
        directory.custody.verify()
    finally:
        os.close(fd)


def _publish_new(directory: _Directory, name: str, value: dict[str, Any], *, stage: str) -> None:
    """Publish complete bytes exclusively in the held directory; never replace."""
    directory.custody.verify()
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temporary = f".pending-{uuid.uuid4().hex}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory.fd)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written == 0:
                raise OSError("short enrollment record write")
            offset += written
        os.fsync(fd)
        directory.custody.verify()
        if _file_stamp(os.stat(temporary, dir_fd=directory.fd, follow_symlinks=False)) != _file_stamp(os.fstat(fd)):
            raise EnrollmentError("pending enrollment record was replaced")
        os.link(temporary, name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd,
                follow_symlinks=False)
        _checkpoint(f"{stage}-linked")
        named = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        if _fingerprint(named) != _fingerprint(os.fstat(fd)):
            raise EnrollmentError("published enrollment record was replaced")
        os.unlink(temporary, dir_fd=directory.fd)
        _checkpoint(f"{stage}-unlinked")
        _fsync_directory(directory)
        _checkpoint(f"{stage}-published")
    finally:
        # Never delete a replacement pending entry or an account record on error.
        try:
            pending = os.stat(temporary, dir_fd=directory.fd, follow_symlinks=False)
            if _fingerprint(pending) == _fingerprint(os.fstat(fd)):
                os.unlink(temporary, dir_fd=directory.fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(fd)


class Registry:
    """Internal account registry. A public CLI must use for_account().

    No database is opened. Interrupted reservations require separate explicit
    recovery; ordinary lookup/enroll never adopt or delete an orphan.
    """

    def __init__(self, root: Path):
        self.root = _absolute(root)

    @classmethod
    def for_account(cls) -> Registry:
        return cls(default_registry_root())

    def _record_path(self, workspace: Workspace) -> Path:
        key = hashlib.sha256(os.fsencode(workspace.common)).hexdigest()
        return self.root / f"{key}.json"

    @staticmethod
    def _validate_head(record):
        required = {"v", "enrollment_id", "generation", "common", "common_device",
                    "common_inode", "state", "state_device", "state_inode"}
        if record.get("v") == 2:
            required.add("binding_id")
        if set(record) != required:
            raise EnrollmentError("enrollment record has unsupported fields")
        for key in ("v", "generation", "common_device", "common_inode", "state_device", "state_inode"):
            if type(record[key]) is not int or record[key] < 0:
                raise EnrollmentError("enrollment record has an invalid numeric field")
        identity = record["enrollment_id"]
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise EnrollmentError("enrollment version or identity is unsupported")
        if record["v"] == 1:
            if record["generation"] != 1:
                raise EnrollmentError("enrollment version or identity is unsupported")
        elif record["v"] == 2:
            if (record["generation"] < 2 or not isinstance(record["binding_id"], str)
                    or not _ID.fullmatch(record["binding_id"])):
                raise EnrollmentError("binding generation or identity is unsupported")
        else:
            raise EnrollmentError("enrollment version or identity is unsupported")
        if any(not isinstance(record[key], str) or not Path(record[key]).is_absolute()
               for key in ("common", "state")):
            raise EnrollmentError("binding paths must be absolute")

    def _retirements(self, registry, identity, authority):
        """A complete, hash-named sequence authorizes exactly one successor."""
        prefix = "retired-binding-" + identity
        names = registry.custody.watch_inventory(registry, prefix)
        rows = {}
        for name in names:
            match = _RETIREMENT.fullmatch(name)
            if match is None or match[1] != identity:
                raise EnrollmentError("malformed binding retirement name; preserve and inspect")
            generation = int(match[2])
            record = _read_json(registry, name)
            fd = registry.custody.files[-1][2]
            payload = os.pread(fd, _MAX_JSON + 1, 0)
            if (hashlib.sha256(payload).hexdigest() != match[3] or _canonical(record) != payload
                    or set(record) != {"v", "kind", "enrollment_id", "prior", "successor"}
                    or type(record["v"]) is not int or record["v"] != 1
                    or record["kind"] != "binding-retirement" or record["enrollment_id"] != identity
                    or not isinstance(record["prior"], dict) or not isinstance(record["successor"], dict)):
                raise EnrollmentError("binding retirement is invalid; preserve and inspect")
            if generation in rows:
                raise EnrollmentError("competing binding retirements; preserve and inspect")
            prior, successor = record["prior"], record["successor"]
            self._validate_head(prior)
            self._validate_head(successor)
            fixed = ("enrollment_id", "common_device", "common_inode", "state_device", "state_inode")
            if (prior["enrollment_id"] != identity or prior["generation"] != generation
                    or successor["v"] != 2 or successor["generation"] != generation + 1
                    or any(prior[key] != successor[key] for key in fixed)
                    or prior["common"] == successor["common"]):
                raise EnrollmentError("binding retirement transition is unsupported")
            for head in (prior, successor):
                common = Path(head["common"])
                if (common.name != ".git" or str(common) != os.path.normpath(head["common"])
                        or head["state"] != str(common.parent / ".relay")):
                    raise EnrollmentError("binding retirement paths are unsupported")
            rows[generation] = (name, record)
        ordered = []
        binding_ids = set()
        for generation in sorted(rows):
            name, record = rows[generation]
            if generation != len(ordered) + 1:
                raise EnrollmentError("binding retirement history has a gap")
            if ordered:
                if record["prior"] != ordered[-1][1]["successor"]:
                    raise EnrollmentError("binding retirement history diverges")
            elif (record["prior"]["v"] != 1
                  or hashlib.sha256(os.fsencode(record["prior"]["common"])).hexdigest() + ".json" != authority):
                raise EnrollmentError("binding retirement does not start at the original authority")
            binding_id = record["successor"]["binding_id"]
            if binding_id in binding_ids:
                raise EnrollmentError("binding retirement reuses a successor identity")
            binding_ids.add(binding_id)
            ordered.append((name, record))
        registry.custody.verify()
        return ordered

    def _route(self, registry, name, *, allow_retired=False):
        record = _read_json(registry, name)
        authority = name
        alias_identity = None
        if record.get("kind") == "binding-alias":
            if (set(record) != {"v", "kind", "authority", "enrollment_id"}
                    or type(record["v"]) is not int or record["v"] != 2
                    or not isinstance(record["authority"], str)
                    or not _AUTHORITY.fullmatch(record["authority"])
                    or record["authority"] == name
                    or not isinstance(record["enrollment_id"], str)
                    or not _ID.fullmatch(record["enrollment_id"])):
                raise EnrollmentError("unsupported binding alias")
            authority, alias_identity = record["authority"], record["enrollment_id"]
            record = _read_json(registry, authority)
        self._validate_head(record)  # A second alias is never followed.
        if alias_identity is not None and alias_identity != record["enrollment_id"]:
            raise EnrollmentError("binding alias identity differs from its authority")
        history = self._retirements(registry, record["enrollment_id"], authority)
        if history:
            latest = history[-1][1]
            if record != latest["successor"] and not (allow_retired and record == latest["prior"]):
                raise EnrollmentError("selected binding is retired or not the authorized successor")
        elif record["v"] != 1:
            raise EnrollmentError("version-two binding has no retirement authorization")
        return record, authority

    @staticmethod
    def _local_identity(record, workspace, custody):
        if (record["common_device"] != workspace.common_device
                or record["common_inode"] != workspace.common_inode):
            raise EnrollmentError("Git directory identity differs from enrollment")
        identity = record["enrollment_id"]
        state = custody.get(workspace.state, private=True)
        state_info = os.fstat(state.fd)
        if _fingerprint(state_info) != (record["state_device"], record["state_inode"]):
            raise EnrollmentError("enrolled state directory was replaced")
        marker = _read_json(state, "enrollment.json")
        if marker != {"v": 1, "enrollment_id": identity} or type(marker.get("v")) is not int:
            raise EnrollmentError("state marker does not match account enrollment")
        git_marker = _read_json(custody.get(workspace.common), "relay-enrollment.json")
        if git_marker != marker or type(git_marker.get("v")) is not int:
            raise EnrollmentError("Git enrollment marker is missing or changed")
        return state_info

    def _lookup(self, repo: str | os.PathLike[str], workspace: Workspace,
                custody: _Custody) -> Enrollment:
        name = self._record_path(workspace).name
        registry = _held_unless_absent(self.root, custody, private=True)
        if registry is None or not registry.exists(name):
            _unenrolled(workspace, custody)
        record, _ = self._route(registry, name)
        if (record["common"] != str(workspace.common)
                or record["common_device"] != workspace.common_device
                or record["common_inode"] != workspace.common_inode
                or record["state"] != str(workspace.state)):
            raise EnrollmentError("workspace identity changed; explicit recovery is required")
        state_info = self._local_identity(record, workspace, custody)
        if workspace != _resolve_workspace(repo, custody):
            raise EnrollmentError("workspace identity changed during validation")
        custody.verify()
        return Enrollment(record["enrollment_id"], record["generation"], workspace,
                          state_info.st_dev, state_info.st_ino)

    def _rebind_details(self, repo, from_repo, custody):
        from .admission import RebindIdentity
        workspace = _resolve_workspace(repo, custody)
        if workspace.root != workspace.common.parent:
            raise EnrollmentError("rebind requires the moved main checkout, not a linked worktree")
        previous = _absolute(from_repo)
        if previous == workspace.root:
            raise EnrollmentError("rebind requires a different former repository path")
        _absent(previous, custody)
        registry = custody.get(self.root, private=True)
        old_name = hashlib.sha256(os.fsencode(previous / ".git")).hexdigest() + ".json"
        head, authority = self._route(registry, old_name, allow_retired=True)
        history = self._retirements(registry, head["enrollment_id"], authority)
        pending = history[-1] if history and head == history[-1][1]["prior"] else None
        if pending is None and len(history) >= _MAX_RETIREMENTS:
            raise EnrollmentError("binding retirement history is full; no automatic pruning is supported")
        if head["common"] != str(previous / ".git") or head["state"] != str(previous / ".relay"):
            raise EnrollmentError("former path is not the current binding; inspect before retrying")
        self._local_identity(head, workspace, custody)
        if pending is not None:
            successor = pending[1]["successor"]
            if successor["common"] != str(workspace.common) or successor["state"] != str(workspace.state):
                raise EnrollmentError("retirement pins a different successor; resume only its exact target")
        proposed = (pending[1]["successor"] if pending else {
            **head, "v": 2, "binding_id": "0" * 32, "generation": head["generation"] + 1,
            "common": str(workspace.common), "state": str(workspace.state)})
        if len(_canonical({"v": 1, "kind": "binding-retirement", "enrollment_id": head["enrollment_id"],
                           "prior": head, "successor": proposed})) > _MAX_JSON:
            raise EnrollmentError("binding retirement exceeds the supported record bound")
        ledger = RebindIdentity(custody, workspace, head["enrollment_id"], registry)
        try:
            ledger.verify()
            target = self._record_path(workspace).name
            alias = {"v": 2, "kind": "binding-alias", "authority": authority,
                     "enrollment_id": head["enrollment_id"]}
            present = registry.exists(target)
            if target != authority and present:
                observed_alias = _read_json(registry, target)
                if observed_alias != alias or type(observed_alias.get("v")) is not int:
                    raise EnrollmentError("destination binding route is occupied; preserve it and inspect")
            lock = "rebind-lock-" + head["enrollment_id"] + ".lock"
            _check_rebind_lock(registry, lock)
            if stat.S_IMODE(os.fstat(registry.fd).st_mode) & 0o300 != 0o300:
                raise EnrollmentError("account binding directory is not writable")
            fd = next(fd for parent, name, fd, _ in custody.files
                      if parent is registry and name == authority)
            payload = os.pread(fd, _MAX_JSON + 1, 0)
            observation = {"authority": authority, "head": head,
                           "head_file": _file_stamp(os.fstat(fd)),
                           "target": {"common": str(workspace.common), "state": str(workspace.state),
                                      "common_device": workspace.common_device,
                                      "common_inode": workspace.common_inode,
                                      "state_device": head["state_device"],
                                      "state_inode": head["state_inode"], "ledger": ledger.identities}}
            token = hashlib.sha256(_canonical(observation)).hexdigest()
            custody.verify()
            return {"workspace": workspace, "previous": previous, "registry": registry,
                    "head": head, "authority": authority, "old_payload": payload,
                    "target": target, "alias": alias, "alias_present": present and target != authority,
                    "lock": lock, "ledger": ledger, "expected_binding": token, "pending": pending}
        except BaseException:
            ledger.close()
            raise

    def _rebind_plan_value(self, details):
        head = details["head"]
        writes = [] if details["target"] == details["authority"] or details["alias_present"] else [
            str(self.root / details["target"])]
        writes.append(str(self.root / details["authority"]))
        return {"enrollment_id": head["enrollment_id"], "generation": head["generation"],
                "next_generation": head["generation"] + 1,
                "from_repo": str(details["previous"]), "repo": str(details["workspace"].root),
                "authority": details["authority"], "expected_binding": details["expected_binding"],
                "alias_already_published": details["alias_present"], "requires_quiescence": True,
                "writes": [str(self.root)],
                "planned_targets": [*writes, str(self.root / details["lock"])],
                "prepared_alias_pattern": str(self.root / ".pending-rebind-alias-<id>"),
                "retained_binding_pattern": str(self.root / ".retained-rebind-<id>.json"),
                "prepared_guard_pattern": str(self.root / ".pending-rebind-guard-<id>"),
                "retirement_guard_pattern": str(self.root / (
                    "retired-binding-" + head["enrollment_id"] + "-<generation>-<sha256>.json")),
                "retirement_pending": details["pending"] is not None,
                "next_binding_id": details["pending"][1]["successor"]["binding_id"] if details["pending"] else None,
                "retirement_guard": str(self.root / details["pending"][0]) if details["pending"] else None,
                "ledger_changed": False, "enrollment_id_changed": False}

    def rebind_plan(self, repo, from_repo):
        """Read-only identity plan for an already moved, initialized repository."""
        from relay_core.protocol import StateError
        try:
            with _Custody() as custody:
                details = self._rebind_details(repo, from_repo, custody)
                try:
                    return self._rebind_plan_value(details)
                finally:
                    details["ledger"].close()
        except (OSError, StateError) as exc:
            raise EnrollmentError("rebind identity is unavailable; no binding was changed") from exc

    def rebind(self, repo, from_repo, *, expected_binding, confirm_quiescent=False):
        """Change only account binding metadata after an operator-quiesced move.

        The lock coordinates rebind calls, not old workers or external renames.
        Retained generations/aliases are evidence, never independent authority.
        """
        from relay_core.protocol import StateError
        if confirm_quiescent is not True:
            raise EnrollmentError("explicit confirmation that Multithread users are quiescent is required")
        if not isinstance(expected_binding, str) or not _DIGEST.fullmatch(expected_binding):
            raise EnrollmentError("a complete expected binding from rebind-plan is required")
        started = False
        retained = None
        try:
            with _Custody() as custody:
                details = self._rebind_details(repo, from_repo, custody)
                try:
                    if details["expected_binding"] != expected_binding:
                        raise EnrollmentError("binding observation is stale; plan again before retrying")
                    registry, head = details["registry"], details["head"]
                    def verify():
                        custody.verify()
                        _absent(details["previous"], custody)
                        if _resolve_workspace(repo, custody) != details["workspace"]:
                            raise EnrollmentError("moved workspace changed")
                        details["ledger"].verify()
                    started = True  # Lock creation is an explicit account write.
                    with _rebind_lock(registry, details["lock"]):
                        verify()
                        target = details["target"]
                        if target != details["authority"]:
                            if registry.exists(target):
                                observed_alias = _read_json(registry, target)
                                if (observed_alias != details["alias"]
                                        or type(observed_alias.get("v")) is not int):
                                    raise EnrollmentError("destination alias changed; preserve it and inspect")
                            else:
                                temporary = ".pending-rebind-alias-" + uuid.uuid4().hex
                                payload = _prepare_record(registry, temporary, details["alias"])
                                _checkpoint("rebind-alias-prepared")
                                verify()
                                _rename_record(registry, temporary, target, 1)
                                custody.renamed_files(registry, {temporary: (target, payload)})
                                _checkpoint("rebind-alias-published")
                                _fsync_directory(registry)
                        if details["pending"]:
                            replacement = details["pending"][1]["successor"]
                            identity = replacement["binding_id"]
                        else:
                            identity = uuid.uuid4().hex
                            replacement = {**head, "v": 2, "binding_id": identity,
                                           "generation": head["generation"] + 1,
                                           "common": str(details["workspace"].common),
                                           "state": str(details["workspace"].state)}
                        # Retrying a pinned successor retains prior preparations;
                        # the publication filename is independent of binding_id.
                        retained = ".retained-rebind-" + uuid.uuid4().hex + ".json"
                        payload = _prepare_record(registry, retained, replacement)
                        _checkpoint("rebind-head-prepared")
                        verify()
                        if details["pending"]:
                            guard_name = details["pending"][0]
                        else:
                            guard = {"v": 1, "kind": "binding-retirement",
                                     "enrollment_id": head["enrollment_id"],
                                     "prior": head, "successor": replacement}
                            prefix = "retired-binding-" + head["enrollment_id"]
                            guard_name = (prefix + "-" + str(head["generation"]) + "-"
                                          + hashlib.sha256(_canonical(guard)).hexdigest() + ".json")
                            prepared_guard = ".pending-rebind-guard-" + uuid.uuid4().hex
                            guard_payload = _prepare_record(registry, prepared_guard, guard)
                            _checkpoint("rebind-guard-prepared")
                            verify()
                            _rename_record(registry, prepared_guard, guard_name, 1)
                            custody.renamed_files(registry, {prepared_guard: (guard_name, guard_payload)},
                                                  published_guard=(prefix, guard_name))
                            _checkpoint("rebind-guard-published")
                        guard_fd = next(fd for parent, name, fd, _ in custody.files
                                        if parent is registry and name == guard_name)
                        os.fsync(guard_fd)
                        _fsync_directory(registry)
                        _checkpoint("rebind-guard-synced")
                        verify()
                        _checkpoint("rebind-before-exchange")
                        verify()
                        _rename_record(registry, retained, details["authority"], 2)
                        _checkpoint("rebind-head-exchanged")
                        custody.renamed_files(registry, {
                            retained: (details["authority"], payload),
                            details["authority"]: (retained, details["old_payload"]),
                        })
                        _fsync_directory(registry)
                        _checkpoint("rebind-head-synced")
                        verify()
                        entry = self._lookup(repo, details["workspace"], custody)
                        result = self._rebind_plan_value(details)
                        result.update(rebound=True, generation=entry.generation,
                                      binding_id=identity, retained_binding=str(self.root / retained),
                                      retirement_pending=False, next_binding_id=identity,
                                      retirement_guard=str(self.root / guard_name))
                        return result
                finally:
                    details["ledger"].close()
        except (OSError, StateError, EnrollmentError) as exc:
            if started:
                suffix = ("; retained publication object: " + str(self.root / retained)) if retained else ""
                raise EnrollmentError("rebind outcome may be uncertain; inspect before retrying" + suffix) from exc
            if isinstance(exc, EnrollmentError):
                raise
            raise EnrollmentError("rebind identity is unavailable; no binding was changed") from exc

    def lookup(self, repo: str | os.PathLike[str]) -> Enrollment:
        """Read-only validation; absence and corruption are errors, never empty."""
        try:
            with _Custody() as custody:
                return self._lookup(repo, _resolve_workspace(repo, custody), custody)
        except OSError as exc:
            raise EnrollmentError("enrollment identity is unavailable; no state was opened") from exc

    def enroll(self, repo: str | os.PathLike[str]) -> Enrollment:
        """Explicitly reserve one workspace; never adopt existing state."""
        publication_started = False
        try:
            with _Custody() as custody:
                workspace = _resolve_workspace(repo, custody)
                parent = custody.get(workspace.state.parent)
                common = custody.get(workspace.common)
                try:
                    registry = custody.get(self.root, private=True)
                except FileNotFoundError:
                    registry = None
                name = self._record_path(workspace).name
                if registry and registry.exists(name):
                    return self._lookup(repo, workspace, custody)
                if parent.exists(workspace.state.name):
                    raise EnrollmentError("existing Multithread state is not enrolled; explicit recovery is required")
                if common.exists("relay-enrollment.json"):
                    raise EnrollmentError("existing Git enrollment marker has no account anchor")
                publication_started = True
                registry = custody.get(self.root, create=True, private=True)
                custody.verify()
                os.mkdir(workspace.state.name, mode=0o700, dir_fd=parent.fd)
                state = custody.get(workspace.state, private=True)
                if os.listdir(state.fd):
                    raise EnrollmentError("reserved state is not empty; explicit recovery is required")
                _checkpoint("state-directory-created")
                _fsync_directory(parent)
                identity = uuid.uuid4().hex
                marker = {"v": 1, "enrollment_id": identity}
                _publish_new(state, "enrollment.json", marker, stage="state-marker")
                _publish_new(common, "relay-enrollment.json", marker, stage="git-marker")
                if workspace != _resolve_workspace(repo, custody):
                    raise EnrollmentError("workspace changed during enrollment; reserved state was not adopted")
                record = {
                    "v": 1, "enrollment_id": identity, "generation": 1,
                    "common": str(workspace.common), "common_device": workspace.common_device,
                    "common_inode": workspace.common_inode, "state": str(workspace.state),
                    "state_device": state.initial.st_dev, "state_inode": state.initial.st_ino,
                }
                _publish_new(registry, name, record, stage="account-record")
                return self._lookup(repo, workspace, custody)
        except (OSError, EnrollmentError) as exc:
            if publication_started:
                raise EnrollmentError("enrollment outcome is uncertain; inspect account record and reserved state before retrying") from exc
            if isinstance(exc, EnrollmentError):
                raise
            raise EnrollmentError("enrollment identity is unavailable; no state was opened") from exc
