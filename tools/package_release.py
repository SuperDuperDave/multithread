#!/usr/bin/env python3
"""Build deterministic public assets from one reviewed, immutable Git commit.

This offline tool executes the selected commit's bootstrap build command. The
repository and selected source must already be trusted and reviewed for release.
It does not publish assets, inspect account files, or export repository history.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile


REPOSITORY_URL = "https://github.com/SuperDuperDave/multithread"
RUNTIME_FILES = frozenset({
    "relay_core/__init__.py", "relay_core/cli.py", "relay_core/protocol.py",
    "relay_core/store.py", "relay_runtime/__init__.py",
    "relay_runtime/admission.py", "relay_runtime/cli.py",
    "relay_runtime/confinement.py", "relay_runtime/enrollment.py",
    "relay_runtime/provider.py", "relay_runtime/codex_peer.py", "relay_runtime/peer_control.py",
    "relay_runtime/agent.py",
    "relay_runtime/claude_peer.py", "relay_runtime/native_io.py",
    "relay_runtime/review_packet.py",
    "relay_runtime/setup.py", "relay_runtime/update.py",
})
SOURCE_FILES = frozenset({"LICENSE", "src/relay_bootstrap.py"}) | {
    "src/" + name for name in RUNTIME_FILES
}
MAX_MEMBER = 1024 * 1024
MAX_TOTAL = 32 * MAX_MEMBER
MAX_ARCHIVE = 8 * MAX_MEMBER
VERSION = re.compile(r"(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\Z")
INSTALLER_MARKER = b"PINNED_RELEASE = None"


class PackageError(RuntimeError):
    """A bounded release build could not be completed."""


def sha256(body):
    return hashlib.sha256(body).hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


class CommittedSource:
    """Read only explicitly selected blobs; never consult working-tree content."""

    def __init__(self, repository, revision, temporary):
        self.repository = Path(repository).resolve(strict=True)
        self.temporary = temporary
        self.env = {
            "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0", "PYTHONDONTWRITEBYTECODE": "1",
        }
        try:
            if not stat.S_ISDIR((self.repository / ".git").lstat().st_mode):
                raise PackageError("release packaging requires an independent Git repository")
        except OSError:
            raise PackageError("independent Git repository unavailable") from None
        for name in ("shallow", "info/grafts", "objects/info/alternates"):
            if os.path.lexists(self.repository / ".git" / name):
                raise PackageError("shallow, grafted or alternate Git storage is unsupported")
        root = self.git("rev-parse", "--show-toplevel", bound=4096).decode().strip()
        common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir", bound=4096).decode().strip()
        if root != str(self.repository) or common != str(self.repository / ".git"):
            raise PackageError("unexpected Git root or common directory")
        algorithm = self.git("rev-parse", "--show-object-format", bound=64).strip()
        length = {b"sha1": 40, b"sha256": 64}.get(algorithm)
        if length is None or not re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", revision):
            raise PackageError("a full lowercase commit identity is required")
        if self.git("cat-file", "-t", revision, bound=64).strip() != b"commit":
            raise PackageError("revision must identify a commit")
        self.revision = revision
        timestamp = self.git("show", "--no-patch", "--format=%ct", revision, bound=64).strip()
        if not timestamp.isdigit() or not 0 <= int(timestamp) <= 0xFFFFFFFF:
            raise PackageError("commit timestamp is outside the archive format")
        self.timestamp = int(timestamp)

    def git(self, *arguments, bound=MAX_MEMBER):
        with tempfile.TemporaryFile(dir=self.temporary) as output, tempfile.TemporaryFile(dir=self.temporary) as errors:
            try:
                result = subprocess.run(
                    ["/usr/bin/git", "--no-replace-objects", "-c", "protocol.allow=never",
                     "-c", "core.hooksPath=/dev/null", *arguments],
                    cwd=self.repository, env=self.env, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=errors, timeout=20, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise PackageError("bounded Git read failed") from None
            if result.returncode != 0 or output.tell() > bound:
                raise PackageError("Git read failed or exceeded the release bound")
            output.seek(0)
            return output.read(bound + 1)

    def files(self):
        listing = self.git("ls-tree", "-r", "-z", "--full-tree", self.revision,
                           "--", *sorted(SOURCE_FILES), bound=65536)
        leaves = {}
        try:
            for item in listing.split(b"\0"):
                if not item:
                    continue
                header, raw_name = item.split(b"\t", 1)
                mode, kind, oid = header.split(b" ")
                name = raw_name.decode("ascii")
                if (name not in SOURCE_FILES or name in leaves or kind != b"blob"
                        or mode not in {b"100644", b"100755"}):
                    raise PackageError("release source contains an ineligible Git member")
                leaves[name] = oid.decode("ascii")
        except (UnicodeError, ValueError):
            raise PackageError("invalid selected Git tree") from None
        if set(leaves) != SOURCE_FILES:
            raise PackageError("commit is missing required release source files")
        bodies = {}
        total = 0
        for name, oid in sorted(leaves.items()):
            size = self.git("cat-file", "-s", oid, bound=64).strip()
            if not size.isdigit() or not 0 < int(size) <= MAX_MEMBER:
                raise PackageError("selected source member exceeds the release bound")
            body = self.git("show", self.revision + ":" + name)
            if len(body) != int(size):
                raise PackageError("selected source member has an inconsistent size")
            total += len(body)
            if total > MAX_TOTAL:
                raise PackageError("selected source exceeds the aggregate release bound")
            bodies[name] = body
        return bodies


def build_runtime(source, temporary, version, committed):
    staging = temporary / "source"
    staging.mkdir(mode=0o700)
    for name, body in source.items():
        target = staging / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(body)
    runtime = temporary / "runtime"
    with tempfile.TemporaryFile(dir=temporary) as output, tempfile.TemporaryFile(dir=temporary) as errors:
        try:
            result = subprocess.run(
                ["/usr/bin/python3", "-I", "-S", "-B", str(staging / "src/relay_bootstrap.py"),
                 "build-release", "--output", str(runtime), "--version", version],
                cwd=staging, env=committed.env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=errors, timeout=60, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise PackageError("committed bootstrap build failed") from None
        if result.returncode != 0 or output.tell() > 65536:
            raise PackageError("committed bootstrap refused the release build")
        output.seek(0)
        try:
            result = json.loads(output.read())
        except (ValueError, UnicodeError):
            raise PackageError("committed bootstrap returned invalid build metadata") from None
    expected = {"bootstrap.py", "release.json"} | {"payload/" + name for name in RUNTIME_FILES}
    members = {}
    for path in runtime.rglob("*"):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        name = path.relative_to(runtime).as_posix()
        if (name not in expected or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1 or not 0 < info.st_size <= MAX_MEMBER):
            raise PackageError("built runtime contains an ineligible member")
        members[name] = path.read_bytes()
    if set(members) != expected:
        raise PackageError("built runtime differs from the closed release layout")
    if (members["bootstrap.py"] != source["src/relay_bootstrap.py"]
            or any(members["payload/" + name] != source["src/" + name] for name in RUNTIME_FILES)):
        raise PackageError("built runtime does not match the selected source")
    release_id = sha256(members["release.json"])
    if not isinstance(result, dict) or result.get("release_id") != release_id or result.get("version") != version:
        raise PackageError("built runtime identity does not match its build metadata")
    return members, release_id


def archive_bytes(members, timestamp):
    output = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=timestamp, compresslevel=9) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, body in sorted(members.items()):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(body), 0o644, timestamp
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(body))
    return output.getvalue()


def package_release(repository, revision, version, output):
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise PackageError("release version must be a bare X.Y.Z version without leading zeroes")
    output = Path(output).absolute()
    if os.path.lexists(output):
        raise PackageError("release output must be a new directory")
    if not output.parent.is_dir():
        raise PackageError("release output parent must already exist")
    with tempfile.TemporaryDirectory(prefix="relay-package-") as name:
        temporary = Path(name)
        committed = CommittedSource(repository, revision, temporary)
        source = committed.files()
        runtime, release_id = build_runtime(source, temporary, version, committed)
        prefix = "relay-" + version
        archive_name = prefix + "-linux-x86_64.tar.gz"
        permalink = REPOSITORY_URL + "/blob/" + revision
        readme = (
            f"# Multithread {version}\n\n"
            "For the supported Linux x86-64 profile with Python 3.12 at `/usr/bin/python3`.\n\n"
            f"[Source commit]({REPOSITORY_URL}/tree/{revision}) · "
            f"[Setup]({permalink}/docs/SETUP.md) · "
            f"[Help and recovery]({permalink}/docs/SUPPORT.md)\n\n"
            f"[Version-pinned installer]({REPOSITORY_URL}/releases/download/v{version}/install.py)\n\n"
            f"Runtime release SHA256: `{release_id}`\n\n"
            "The release installer verifies this archive and its exact file set before "
            "installation. See Setup for installation, updates, and the offline workflow.\n"
        ).encode("utf-8")
        members = {prefix + "/LICENSE": source["LICENSE"], prefix + "/README.md": readme}
        members.update({prefix + "/runtime/" + name: body for name, body in runtime.items()})
        checksums = "".join(sha256(body) + "  " + name[len(prefix) + 1:] + "\n"
                            for name, body in sorted(members.items())).encode("ascii")
        members[prefix + "/SHA256SUMS"] = checksums
        archive = archive_bytes(members, committed.timestamp)
        if len(archive) > MAX_ARCHIVE:
            raise PackageError("archive exceeds the installer download bound")
        metadata = {
            "schema": 1, "version": version, "source_commit": revision,
            "archive": archive_name, "archive_sha256": sha256(archive), "release_id": release_id,
            "files": {name: sha256(body) for name, body in sorted(members.items())},
        }
        installer = source["src/relay_runtime/update.py"]
        if installer.count(INSTALLER_MARKER) != 1:
            raise PackageError("committed installer must contain exactly one release metadata marker")
        installer = installer.replace(INSTALLER_MARKER, b"PINNED_RELEASE = " + repr(metadata).encode("ascii"))
        try:
            compile(installer, "install.py", "exec")
        except (SyntaxError, UnicodeError, ValueError):
            raise PackageError("generated installer is not valid Python") from None
        assets = {archive_name: archive, "relay-release.json": canonical_json(metadata), "install.py": installer}
        assets.update({name + ".sha256": (sha256(body) + "  " + name + "\n").encode("ascii")
                       for name, body in list(assets.items())})
        try:
            output.mkdir(mode=0o700)
        except FileExistsError:
            raise PackageError("release output must be a new directory") from None
        for name, body in sorted(assets.items()):
            with (output / name).open("xb") as stream:
                stream.write(body)
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True, help="full reviewed commit identity")
    parser.add_argument("--version", required=True, help="bare release version, for example 0.2.0")
    parser.add_argument("--output", required=True, help="new output directory; its parent must exist")
    args = parser.parse_args(argv)
    try:
        metadata = package_release(Path.cwd(), args.revision, args.version, args.output)
    except PackageError as exc:
        print("Release packaging failed: " + str(exc) + ".", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("Release packaging failed: source or output filesystem is unavailable.", file=sys.stderr)
        return 1
    print(canonical_json(metadata).decode("ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
