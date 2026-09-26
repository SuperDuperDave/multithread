"""Create an explicitly scoped, private Git diff packet for read-only review."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys


_MAX_DIFF = 1024 * 1024


class PacketError(Exception):
    pass


def _git(repo, *arguments):
    environment = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C.UTF-8",
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                   "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
                   "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1"}
    command = ["/usr/bin/git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
               "-C", str(repo), *arguments]
    try:
        result = subprocess.run(command, env=environment, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise PacketError("Git inspection was unavailable; no packet was written.") from None
    if result.returncode != 0:
        raise PacketError("Git refused the selected revision or paths; no packet was written.")
    return result.stdout


def _path(value):
    if (not value or len(value.encode("utf-8")) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or PurePosixPath(value).is_absolute() or any(part in ("", ".", "..", ".git")
                                                   for part in value.split("/"))):
        raise argparse.ArgumentTypeError("select a relative path inside the Git root, without control characters or traversal")
    return value


def packet_main(argv=None):
    parser = argparse.ArgumentParser(prog="multithread peer packet",
        description="Freeze an explicit tracked-path Git diff in a private review packet; inspect it before sharing.")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="exact Git worktree root")
    parser.add_argument("--base", default="HEAD", help="base commit or ref; default HEAD")
    parser.add_argument("--path", action="append", type=_path, required=True,
                        help="tracked relative path to include; repeat for each file or directory")
    parser.add_argument("--output-file", required=True, type=Path,
                        help="new private UTF-8 packet file, ideally outside the repository")
    parser.add_argument("--json", action="store_true", help="print only packet metadata as JSON")
    args = parser.parse_args(argv)
    try:
        repo = args.repo.resolve(strict=True)
        root = Path(_git(repo, "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
        if repo != root:
            raise PacketError("--repo must be the exact Git worktree root; no packet was written.")
        if args.base.startswith("-") or any(ord(c) < 33 or ord(c) > 126 for c in args.base):
            raise PacketError("--base must be a printable Git revision; no packet was written.")
        base = _git(repo, "rev-parse", "--verify", "--end-of-options", args.base + "^{commit}").decode().strip()
        head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        paths = sorted(set(args.path))
        selected = [":(literal)" + path for path in paths]
        tracked = _git(repo, "ls-files", "--cached", "--", *selected)
        if not tracked:
            raise PacketError("No tracked files matched the selected paths; no packet was written.")
        status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all", "--", *selected)
        if any(line.startswith(b"?? ") for line in status.splitlines()):
            raise PacketError("Selected paths include untracked files that a Git diff would omit; no packet was written.")
        options = ("diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames")
        numstat = _git(repo, *options, "--numstat", base, "--", *selected)
        if any(line.startswith(b"-\t-\t") for line in numstat.splitlines()):
            raise PacketError("Selected changes include binary files; review them separately. No packet was written.")
        diff = _git(repo, *options, "--full-index", base, "--", *selected)
        if not diff:
            raise PacketError("The selected paths have no diff from the base commit; no packet was written.")
        if any(line.startswith((b"Binary files ", b"GIT binary patch")) for line in diff.splitlines()):
            raise PacketError("Selected changes include binary files; review them separately. No packet was written.")
        if len(diff) > _MAX_DIFF or b"\0" in diff:
            raise PacketError("The diff exceeds 1 MiB or contains NUL bytes; narrow the selection. No packet was written.")
        try:
            decoded = diff.decode("utf-8")
        except UnicodeError:
            raise PacketError("The selected diff is not UTF-8; review it separately. No packet was written.") from None
        # Git reads the worktree across several commands. Refuse ordinary
        # concurrent edits rather than labeling inconsistent observations a
        # single frozen review packet.
        if (head != _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
                or status != _git(repo, "status", "--porcelain=v1", "--untracked-files=all", "--", *selected)
                or diff != _git(repo, *options, "--full-index", base, "--", *selected)):
            raise PacketError("Selected Git state changed during inspection; no packet was written.")
        digest = hashlib.sha256(diff).hexdigest()
        header = ("Multithread read-only review packet\n"
                  + "Base commit: " + base + "\n"
                  + "Head commit: " + head + "\n"
                  + "Selected paths: " + json.dumps(paths, ensure_ascii=True) + "\n"
                  + "Diff bytes: " + str(len(diff)) + "\n"
                  + "Diff SHA-256: " + digest + "\n"
                  + "Scope: tracked text changes from the base commit through the observed working tree. "
                    "Untracked and binary content is excluded. No secret scan or provider permission change is performed.\n"
                  + "Review this private file before sharing it with a peer.\n"
                  + "----- BEGIN DIFF -----\n")
        body = (header + decoded + "----- END DIFF -----\n").encode("utf-8")
        target = args.output_file.absolute()
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        summary = {"state": "packet_written", "path": str(target), "diff_sha256": digest,
                   "diff_bytes": len(diff), "base_commit": base, "head_commit": head,
                   "selected_paths": paths, "review_before_sharing": True}
        if args.json:
            print(json.dumps(summary, sort_keys=True))
        else:
            print(f"Private review packet: {target}\nDiff SHA-256: {digest}\nReview the file before sharing it.")
        return 0
    except (PacketError, OSError, UnicodeError) as exc:
        message = str(exc) if isinstance(exc, PacketError) else "A selected path or output file is unavailable; no packet was written."
        if args.json:
            print(json.dumps({"state": "unavailable", "message": message}))
        else:
            print("multithread peer packet: " + message, file=sys.stderr)
        return 1
