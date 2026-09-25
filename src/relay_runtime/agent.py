"""Optional account-owned adapters for independently running agents.

This is outside the confined coordination worker. An adapter is selected by
explicit account registration, never by a checkout, prompt, or environment
variable. The initial adapter kind is Meta Muse.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile


_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_TASK_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_MAX_REGISTRY = 64 * 1024
_MAX_CLIENT = 1024 * 1024
_MAX_RESPONSE = 16 * 1024 * 1024
_MAX_ERROR_TAIL = 4096
_ADAPTER_LOADER = """import os, sys
descriptor, path = int(sys.argv[1]), sys.argv[2]
with os.fdopen(descriptor, "rb") as source:
    body = source.read()
sys.argv = [path, *sys.argv[3:]]
exec(compile(body, path, "exec"), {"__name__": "__main__", "__file__": path})
"""


class AgentError(Exception):
    pass


def _registry_path():
    if os.getuid() != os.geteuid():
        raise AgentError("set-user-ID execution is unsupported")
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/share/relay/agents.json"


def _private_parent(path, *, create=False):
    parent = path.parent
    if create:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = parent.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise AgentError("agent registry parent is not a private account directory")
    return True


def _load(path):
    if not _private_parent(path):
        return {"schema": 1, "agents": {}}
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"schema": 1, "agents": {}}
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > _MAX_REGISTRY):
        raise AgentError("agent registry is not a private regular file")
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError, UnicodeError) as exc:
        raise AgentError("agent registry is unreadable or malformed") from exc
    if (not isinstance(data, dict) or set(data) != {"schema", "agents"}
            or data["schema"] != 1 or not isinstance(data["agents"], dict)):
        raise AgentError("agent registry schema is unsupported")
    for key, item in data["agents"].items():
        if (not isinstance(key, str) or not _NAME.fullmatch(key) or key != key.lower()
                or not isinstance(item, dict)
                or set(item) != {"kind", "nickname", "client", "sha256"}
                or item["kind"] != "muse" or not isinstance(item["nickname"], str)
                or item["nickname"].lower() != key or not _NAME.fullmatch(item["nickname"])
                or not isinstance(item["client"], str) or not Path(item["client"]).is_absolute()
                or not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise AgentError("agent registry contains an invalid entry")
    return data


def _save(path, data):
    _private_parent(path, create=True)
    payload = (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()
    if len(payload) > _MAX_REGISTRY:
        raise AgentError("agent registry would exceed its size limit")
    fd, temporary = tempfile.mkstemp(prefix=".agents-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _client_bytes(raw):
    path = Path(raw)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise AgentError("client must be an absolute path without symlink components")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > _MAX_CLIENT:
            raise AgentError("client must be a bounded regular file owned by this account")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            body = stream.read(_MAX_CLIENT + 1)
        if len(body) > _MAX_CLIENT:
            raise AgentError("client exceeds the adapter size limit")
        return body
    finally:
        os.close(descriptor)


def _nickname(raw):
    if not _NAME.fullmatch(raw):
        raise AgentError("nickname must start with a letter and contain only letters, digits, hyphens or underscores")
    return raw.lower()


def _parser():
    parser = argparse.ArgumentParser(prog="multithread agent", description="Optional account-owned agent adapters; no agent is required for ordinary Multithread use")
    kinds = parser.add_subparsers(dest="kind", required=True)
    muse = kinds.add_parser("muse", help="Meta Muse agent selected by nickname")
    actions = muse.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="list registered Muse nicknames")
    register = actions.add_parser("register", help="record one reviewed account-owned client; no network call")
    register.add_argument("nickname")
    register.add_argument("--client", type=Path, required=True, help="absolute path to reviewed Python adapter")
    register.add_argument("--dry-run", action="store_true", help="show identity and digest without writing")
    inspect = actions.add_parser("inspect", help="show registered adapter path and pinned digest")
    inspect.add_argument("nickname")
    remove = actions.add_parser("remove", help="remove a registration; does not alter the adapter or remote tasks")
    remove.add_argument("nickname")
    for name in ("project", "check", "prepare", "send", "status", "replies"):
        action = actions.add_parser(name, help="run the registered agent's " + name + " operation")
        action.add_argument("nickname")
        action.add_argument("--repo", type=Path, default=Path.cwd())
        if name in {"prepare", "send", "status", "replies"}:
            action.add_argument("value", help={"prepare": "UTF-8 task file", "send": "retained packet path", "status": "server task UUID", "replies": "server task UUID"}[name])
    return parser


def _show_adapter_errors(errors):
    size = errors.tell()
    errors.seek(max(0, size - _MAX_ERROR_TAIL))
    tail = errors.read(_MAX_ERROR_TAIL).decode("utf-8", errors="replace")
    if tail:
        print("adapter diagnostics (private, bounded tail):\n" + tail, file=sys.stderr)


def _operate(item, args):
    body = _client_bytes(item["client"])
    if hashlib.sha256(body).hexdigest() != item["sha256"]:
        raise AgentError("registered adapter bytes changed; inspect and register the reviewed client again")
    if args.action in {"status", "replies"} and not _TASK_ID.fullmatch(args.value):
        raise AgentError("expected an exact server task UUID")
    command = [sys.executable, "-I", "-S", "-B", "-c", _ADAPTER_LOADER,
               None, item["client"], "--repo", str(args.repo.absolute()), args.action]
    if hasattr(args, "value"):
        command.append(args.value)
    try:
        with tempfile.TemporaryFile() as held, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            held.write(body)
            held.seek(0)
            command[6] = str(held.fileno())
            try:
                result = subprocess.run(command, pass_fds=(held.fileno(),), stdout=output,
                                        stderr=errors, timeout=300, check=False)
            except subprocess.TimeoutExpired:
                _show_adapter_errors(errors)
                raise
            size = output.tell()
            output.seek(0)
            response = output.read(_MAX_RESPONSE + 1) if size <= _MAX_RESPONSE else None
            if result.returncode:
                _show_adapter_errors(errors)
    except (OSError, subprocess.TimeoutExpired) as exc:
        state = "uncertain" if args.action == "send" else "unavailable"
        return {"kind": "muse", "nickname": item["nickname"], "operation": args.action,
                "state": state, "message": "adapter outcome unavailable; inspect the retained packet and remote task before any retry",
                "error_type": type(exc).__name__}, 1
    if result.returncode:
        state = "uncertain" if args.action == "send" else "unavailable"
        return {"kind": "muse", "nickname": item["nickname"], "operation": args.action,
                "state": state, "adapter_exit_code": result.returncode,
                "message": "adapter did not return a valid result; inspect its own diagnostics before retrying"}, 1
    try:
        parsed = json.loads(response) if response is not None else None
    except (ValueError, UnicodeError):
        parsed = None
    if not isinstance(parsed, dict):
        return {"kind": "muse", "nickname": item["nickname"], "operation": args.action,
                "state": "uncertain" if args.action == "send" else "unavailable",
                "message": "adapter returned missing, oversized or malformed JSON; inspect its exact remote task before retrying"}, 1
    if args.action == "send" and not _TASK_ID.fullmatch(str(parsed.get("task_id", ""))):
        return {"kind": "muse", "nickname": item["nickname"], "operation": args.action,
                "state": "uncertain", "message": "adapter returned no exact task ID; inspect remote state before retrying"}, 1
    return {"kind": "muse", "nickname": item["nickname"], "operation": args.action,
            "result": parsed}, 0


def agent_main(argv=None, *, registry_path=None):
    args = _parser().parse_args(argv)
    path = Path(registry_path) if registry_path is not None else _registry_path()
    try:
        data = _load(path)
        if args.action == "list":
            result = {"kind": "muse", "agents": [data["agents"][key]["nickname"] for key in sorted(data["agents"])]}
        else:
            key = _nickname(args.nickname)
            if args.action == "register":
                digest = hashlib.sha256(_client_bytes(str(args.client))).hexdigest()
                proposed = {"kind": "muse", "nickname": args.nickname, "client": str(args.client), "sha256": digest}
                prior = data["agents"].get(key)
                if prior is not None and prior != proposed:
                    raise AgentError("nickname already registered; remove it explicitly before replacing its adapter")
                if not args.dry_run and prior is None:
                    data["agents"][key] = proposed
                    _save(path, data)
                result = {"kind": "muse", "nickname": args.nickname, "client": str(args.client),
                          "sha256": digest, "registered": not args.dry_run, "changed": not args.dry_run and prior is None}
            elif args.action == "inspect":
                result = data["agents"].get(key)
                if result is None:
                    raise AgentError("Muse nickname is not registered")
            elif args.action == "remove":
                if key not in data["agents"]:
                    raise AgentError("Muse nickname is not registered")
                removed = data["agents"].pop(key)
                _save(path, data)
                result = {"kind": "muse", "nickname": removed["nickname"], "removed": True,
                          "note": "remote tasks and adapter files were not changed"}
            else:
                item = data["agents"].get(key)
                if item is None:
                    raise AgentError("Muse nickname is not registered")
                result, exit_code = _operate(item, args)
                print(json.dumps(result, sort_keys=True))
                return exit_code
        print(json.dumps(result, sort_keys=True))
        return 0
    except (AgentError, OSError, ValueError) as exc:
        print(json.dumps({"kind": "muse", "state": "unavailable", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(agent_main())
