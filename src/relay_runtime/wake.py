"""Wake an existing Codex conversation or Claude Code session.

`bind` names what a role reaches: a Codex conversation, through the shared
app-server daemon, or a Claude Code session, through its private inbox socket.
`wake` sends it one short, attributed pointer. Both run outside the confined
ledger worker, because they reach those sockets and run `codex queue`; every
ledger step goes through the installed launcher. An attempt is recorded before its transport runs
and concluded once afterwards, so an outcome nobody observed stays visible and
is never repeated under the same message id. The daemon accepting a message is
not the recipient reading it: acknowledgement stays the recipient's own act.
"""

import argparse
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

from relay_core.protocol import (ValidationError, canonical_agent, canonical_wake_expectation,
                                 canonical_wake_message_id, canonical_wake_project, canonical_wake_ref,
                                 canonical_wake_role, canonical_wake_sender, canonical_wake_thread,
                                 wake_ref_sequence)
from . import account_launcher, hooks
from .enrollment import EnrollmentError, NotEnrolled, Registry

SOCKET = Path("app-server-control") / "app-server-control.sock"
EXIT_CODES = {"STEERED": 0, "QUEUED": 0, "DELIVERED TO INBOX": 0, "DRY RUN": 0, "STATUS": 0,
              "BOUND": 0, "ALREADY BOUND": 0,
              "ALREADY SENT": 3, "NOT SENT": 4, "NOT BOUND": 4, "NOT RUNNING": 4, "UNCERTAIN": 5}
_CLIENT_INFO = {"name": "multithread-wake", "title": "Multithread wake", "version": "1"}
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_MESSAGE = 1024 * 1024
_DAEMON_TIMEOUT = 10
_STEER_TIMEOUT = 20
_READINESS_TIMEOUT = 2
_QUEUE_TIMEOUT = 60
_LEDGER_TIMEOUT = 30
_INBOX_TIMEOUT = 10
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_WAIT = "Nothing. Wait for the recipient's acknowledgement."
_INVALID_REQUEST = -32600
# Codex 0.159.2 (openai/codex rust-v0.159.2, app-server turn_steer_inner) answers
# these, with -32600, only when it did not submit the steer's input: the only
# errors after which queueing cannot deliver the same wake twice.
_STEER_DECLINED = (
    (re.compile(r"no active turn to steer"), "turn_ended",
     "The turn ended while sending (Codex: {}), so it was queued instead."),
    (re.compile(r"expected active turn id `[^`]*` but found `[^`]*`"), "turn_changed",
     "Another turn was running when the steer arrived (Codex: {}), so it was queued instead."),
    (re.compile(r"cannot steer a (?:review|compact) turn"), "turn_not_steerable",
     "The running turn can't take a steer (Codex: {}), so it was queued instead."),
)
# `codex queue` (same release: cli main.rs and queue_cmd.rs, tui lib.rs and
# session_queue_commands.rs) reports a failure as one stderr line, "Error: "
# and its error chain joined by ": ", then exits 1. The outermost context says
# where it stopped. With --remote, as wake passes, only these three stop before
# thread/queue/add is accepted: no Codex home, no connection, or a server that
# rejected the method itself. Any other output may follow an accepted request.
_QUEUE_REFUSED_BEFORE_SENDING = re.compile(
    r"Error: (?:failed to find Codex home|failed to connect to remote app server|the remote app server does not "
    r"support thread/queue/add; update or restart the remote app server: failed to queue session message)"
    r"(?:: [^\n]*)?\n?")


# --- The daemon: JSON-RPC over a WebSocket on its Unix control socket --------

class DaemonUnavailable(Exception):
    """The daemon could not be reached, so nothing was asked of it."""


class Refused(Exception):
    """The daemon answered with a well-formed JSON-RPC error: its code, message and data."""

    def __init__(self, code, message, data=None):
        self.code, self.raw, self.data = code, message, data
        self.message = _line(message) or "no message"
        super().__init__(f"Codex {code}: {self.message}")


class Unusable(Exception):
    """A reply to this request came back, but it is not a readable answer."""


class NoAnswer(Exception):
    """A request went out and no answer came back: its effect is unknown."""


def _line(text, limit=200):
    """Diagnostic text as one printable line, bounded."""
    return "".join(c for c in " ".join(str(text).split()) if c.isprintable())[:limit]


def _detail(exc):
    text = str(exc) or exc.__class__.__name__
    return text if text.isprintable() and len(text) <= 200 else exc.__class__.__name__


def _observe_runtime(daemon, thread_id):
    """A bounded non-loading observation, never permission to start queued work."""
    observation = {"status": "unknown", "active_flags": [], "source": "codex_thread_read",
                   "phase": "before_queue"}
    try:
        reply = daemon.call("thread/read", {"threadId": thread_id, "includeTurns": False},
                            timeout=_READINESS_TIMEOUT)
    except (Refused, Unusable, NoAnswer):
        observation["unavailable_reason"] = "read_failed"
    else:
        thread = reply.get("thread") if isinstance(reply, dict) else None
        status = thread.get("status") if isinstance(thread, dict) else None
        kind = status.get("type") if isinstance(status, dict) else None
        flags = status.get("activeFlags") if isinstance(status, dict) else None
        if (isinstance(thread, dict) and thread.get("id") == thread_id
                and kind in ("notLoaded", "idle", "systemError", "active")
                and (kind != "active" or isinstance(flags, list) and all(
                    isinstance(flag, str) and flag in ("waitingOnApproval", "waitingOnUserInput")
                    for flag in flags))):
            observation["status"] = kind
            observation["active_flags"] = list(flags) if kind == "active" else []
        else:
            observation["unavailable_reason"] = "unusable_reply"
    observation["observed_at"] = datetime.now(timezone.utc).isoformat()
    return observation


def _queued_readiness(base, native_id):
    """Qualify admission without inferring pickup from an earlier runtime snapshot."""
    runtime = base["recipient_runtime"]
    kind = runtime["status"]
    if base["recipient_state"]["turn_state"] == "interrupted":
        sentence = " The latest observed turn was interrupted; automatic pickup may remain suppressed."
        next_step = (f"Leave original queue entry {native_id} for the recipient's deliberate continuation. "
                     "Do not load, force-start or resend it merely to clear the queue.")
    elif kind == "notLoaded":
        sentence = " Before submission, Codex reported the conversation was not loaded."
        next_step = ("Ask the recipient's owner to load the existing conversation with its execution settings "
                     f"preserved, then inspect original queue entry {native_id}. Do not resend it.")
    elif runtime["active_flags"]:
        sentence = " Before submission, Codex reported an approval or user-input wait."
        next_step = (f"Wait for the recipient to resolve its wait; inspect original queue entry {native_id} "
                     "if pickup remains unobserved. Do not force-start or resend it.")
    elif kind == "systemError":
        sentence = " Before submission, Codex reported a runtime error."
        next_step = (f"Ask the recipient's owner to inspect its error and original queue entry {native_id}. "
                     "Do not resend blindly.")
    elif kind == "unknown":
        sentence = " Runtime readiness was unavailable."
        next_step = (f"Wait for acknowledgement. If none arrives, inspect original queue entry {native_id} "
                     "with the recipient; do not resend blindly.")
    else:
        return "", _WAIT, None
    detail = (f"before_queue runtime={kind}; latest_turn={base['recipient_state']['turn_state']}; "
              f"flags={','.join(runtime['active_flags'])}; observed_at={runtime['observed_at']}")
    return sentence, next_step, detail


class Daemon:
    """One connection to the shared daemon, initialized; requests run one at a time."""

    def __init__(self, path, timeout=_DAEMON_TIMEOUT, *, experimental_api=False, overall_deadline=None):
        self.sock = None
        self.buffer = b""
        self.next_id = 0
        self.deadline = None
        self.overall_deadline = overall_deadline
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.deadline = time.monotonic() + timeout
            self.sock.settimeout(timeout)
            self._check_deadline()
            self.sock.connect(str(path))
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            self.sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                               "Connection: Upgrade\r\nSec-WebSocket-Key: " + key
                               + "\r\nSec-WebSocket-Version: 13\r\n\r\n").encode("ascii"))
            while b"\r\n\r\n" not in self.buffer:
                self._check_deadline()
                chunk = self.sock.recv(4096)
                if not chunk or len(self.buffer) > 16384:
                    raise ConnectionError("the daemon closed the connection during the handshake")
                self.buffer += chunk
            head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
            lines = head.split(b"\r\n")
            accept = base64.b64encode(hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest())
            if not lines[0].startswith(b"HTTP/1.1 101") or not any(
                    name.strip().lower() == b"sec-websocket-accept" and value.strip() == accept
                    for name, _, value in (line.partition(b":") for line in lines[1:])):
                raise ConnectionError("the daemon refused the WebSocket upgrade")
            self.deadline = None
            params = {"clientInfo": _CLIENT_INFO}
            if experimental_api:
                params["capabilities"] = {"experimentalApi": True}
            if not isinstance(self.call("initialize", params, timeout=timeout), dict):
                raise Unusable("its initialize answer was not an object")
            self._send(json.dumps({"method": "initialized", "params": {}}).encode("utf-8"))
        except (OSError, ValueError, Refused, Unusable, NoAnswer) as exc:
            self.close()
            raise DaemonUnavailable(_detail(exc)) from None

    def call(self, method, params, *, timeout=_DAEMON_TIMEOUT):
        """The result, whatever its shape: each caller checks its own receipt.

        Refused: a well-formed error. Unusable: a reply that is neither a
        result nor a well-formed error. NoAnswer: no reply at all.
        """
        self.next_id += 1
        pending = self.next_id
        self.deadline = time.monotonic() + timeout
        try:
            self._send(json.dumps({"id": pending, "method": method, "params": params}).encode("utf-8"))
            while True:
                self._check_deadline()
                message = self._message()
                if "method" in message or message.get("id") != pending:
                    continue  # Notifications and other traffic are not this answer.
                break
        except (OSError, ValueError) as exc:
            raise NoAnswer(_detail(exc)) from None
        finally:
            self.deadline = None
        has_result, has_error = "result" in message, "error" in message
        if has_result and not has_error:
            return message["result"]
        error = message.get("error")
        if (has_error and not has_result and isinstance(error, dict) and type(error.get("code")) is int
                and isinstance(error.get("message"), str)):
            raise Refused(error["code"], error["message"], error.get("data"))
        raise Unusable("its reply held " + ("both a result and an error" if has_result and has_error
                                            else "a malformed error" if has_error
                                            else "neither a result nor an error"))

    def close(self):
        if self.sock is None:
            return
        try:
            self._send(b"", 8)
        except OSError:
            pass
        self.sock.close()
        self.sock = None

    def _check_deadline(self):
        deadlines = [value for value in (self.deadline, self.overall_deadline) if value is not None]
        if deadlines:
            remaining = min(deadlines) - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("the daemon request deadline expired")
            self.sock.settimeout(remaining)

    def _send(self, data, opcode=1):
        self._check_deadline()
        mask = os.urandom(4)
        size = len(data)
        header = bytes([0x80 | opcode]) + (
            bytes([0x80 | size]) if size < 126 else bytes([0x80 | 126]) + struct.pack(">H", size)
            if size < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", size))
        self.sock.sendall(header + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(data)))

    def _take(self, size):
        self._check_deadline()
        while len(self.buffer) < size:
            self._check_deadline()
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("the daemon closed the connection")
            self.buffer += chunk
        taken, self.buffer = self.buffer[:size], self.buffer[size:]
        return taken

    def _message(self):
        body = b""
        while True:
            first, second = self._take(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._take(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._take(8))[0]
            mask = self._take(4) if second & 0x80 else None
            if len(body) + size > _MAX_MESSAGE:
                raise ValueError("the daemon's message exceeded its bound")
            payload = self._take(size)
            if mask is not None:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            opcode = first & 0x0F
            if opcode == 9:
                self._send(payload, 10)
                continue
            if opcode == 10:
                continue
            if opcode == 8:
                raise ConnectionError("the daemon closed the connection")
            body += payload
            if first & 0x80:
                try:
                    value = json.loads(body)
                except RecursionError:
                    raise ValueError("the daemon's JSON exceeded its nesting bound") from None
                if not isinstance(value, dict):
                    raise ValueError("the daemon sent a message that is not an object")
                return value


def daemon_socket():
    """The control socket of the daemon serving this account's Codex home."""
    return hooks.provider_home("codex") / SOCKET


# --- A Claude Code session's inbox: one JSON line on its private socket -------

class InboxRefused(Exception):
    """The inbox socket could not be connected, so nothing was sent."""


def inbox_problem(path):
    """Why this is not this user's private Claude Code inbox socket, as (reason, why), or None."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "inbox_missing", "it doesn't exist"
    except OSError as exc:
        return "inbox_unsafe", f"it can't be inspected ({_detail(exc)})"
    if stat.S_ISLNK(info.st_mode):
        return "inbox_unsafe", "it is a symbolic link"
    if not stat.S_ISSOCK(info.st_mode):
        return "inbox_unsafe", "it isn't a socket"
    if info.st_uid != os.getuid():
        return "inbox_unsafe", "another user owns it"
    if stat.S_IMODE(info.st_mode) != 0o600:
        return "inbox_unsafe", f"its mode is {stat.S_IMODE(info.st_mode):o}, not 600"
    return None


def _listener(connection):
    """(pid, uid) of the process listening on a connected Unix socket, as the kernel recorded it."""
    pid, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                             struct.calcsize("3i")))
    return pid, uid


class InboxNotOwner(Exception):
    """The connected inbox belongs to a process other than the bound session's, so nothing was sent."""


def deliver(path, text, timeout=_INBOX_TIMEOUT, owner=None):
    """Write the one user line Claude Code reads; the line is ready before connecting. With owner (pid, boot id,
    start), the process listening on the connected socket must be exactly that one before anything is sent: the
    path was checked earlier, and a process id can come round again in between."""
    line = (json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=True)
            + "\n").encode("ascii")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        try:
            connection.connect(str(path))
        except OSError as exc:
            raise InboxRefused(_detail(exc)) from None
        if owner is not None:
            try:
                pid, uid = _listener(connection)
            except OSError as exc:
                raise InboxNotOwner(f"its listener couldn't be identified ({_detail(exc)})") from None
            if uid != os.getuid() or pid != owner[0] or _process(pid) != owner[1:]:
                raise InboxNotOwner(f"process {pid} is listening on it, not the session's process {owner[0]}")
        try:
            connection.sendall(line)
        except OSError as exc:
            raise NoAnswer(_detail(exc)) from None
    finally:
        connection.close()


# --- The ledger: one admitted step per installed launcher run ----------------

def launcher_ledger(repo, *arguments, timeout=None):
    """Run one ledger step: (exit code or None when it didn't answer, result, message)."""
    timeout = _LEDGER_TIMEOUT if timeout is None else timeout
    command = [str(account_launcher()), "--repo", str(repo), "--json", *arguments]
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, None, f"it didn't answer within {timeout:g} s"
    except (OSError, UnicodeError) as exc:
        return 1, None, f"{command[0]} could not run ({_detail(exc)})"
    if completed.returncode != 0:
        lines = (completed.stderr or "").strip().splitlines()
        text = lines[-1].removeprefix("multithread: ") if lines else f"exit {completed.returncode}"
        return completed.returncode, None, text[:300]
    try:
        return 0, json.loads(completed.stdout), ""
    except ValueError:
        return 1, None, "it returned output that is not JSON"


# --- Output ------------------------------------------------------------------

def _outcome(status, happened, next_step, **details):
    return {"schema": 1, "status": status, "exit_code": EXIT_CODES[status],
            "happened": happened, "next": next_step, "consumption_state": "unknown",
            "recipient_state": {"reachability": "unknown", "turn_state": "unknown", "source": "unobserved"},
            **details}


def _printable(text):
    """Text for the terminal: a control character in a path or reply is shown escaped, never run."""
    return "".join(character if character.isprintable() else json.dumps(character)[1:-1]
                   for character in str(text))


def _emit(result, as_json):
    if as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(_printable(f"{result['status']}: {result['happened']}"))
        if result.get("text") and result["status"] in ("STEERED", "QUEUED", "DELIVERED TO INBOX", "UNCERTAIN",
                                                        "DRY RUN"):
            print(_printable(f"Message {result['message_id']}: {json.dumps(result['text'], ensure_ascii=False)}"))
        if result.get("warning"):
            print(_printable("Warning: " + result["warning"]))
        if result["status"] == "STATUS":
            history = result["history"]
            for attempt in history.get("attempts", []):
                print(_printable(f"Attempt {attempt['seq']}: {attempt['outcome']}; recipient "
                                 f"{attempt['recipient']} under binding {attempt['generation']}; "
                                 f"consumption {attempt['consumption_state']}"))
            if history.get("has_more"):
                print(_printable(f"More attempts remain; read wake-ledger history {result['role']} "
                                 f"--before {history['next_before']}."))
        print(_printable("Next: " + result["next"]))
    return result["exit_code"]


def _actor(args, *, session_required=True):
    agent = args.agent or os.environ.get("RELAY_AGENT")
    session = args.session or os.environ.get("RELAY_SESSION")
    if not agent or (session_required and not session):
        raise ValidationError("pass --agent and --session (or set RELAY_AGENT and RELAY_SESSION)")
    canonical_agent(agent)
    if session:
        canonical_agent(session)
    return agent, session


def _launcher(*arguments):
    return shlex.join([str(account_launcher()), *arguments])


def _is_checkout(repo):
    """Whether Git sees a work tree here; unknown counts as yes, so no false advice follows."""
    try:
        completed = subprocess.run(
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(repo),
             "rev-parse", "--is-inside-work-tree"], env={"PATH": "/usr/bin:/bin", "LC_ALL": "C",
             "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True
    return completed.returncode == 0 or "not a git repository" not in completed.stderr.lower()


def _ledger_next(code, repo, again):
    """The one step that helps after a ledger refusal."""
    if not _is_checkout(repo):
        return "Run it from an enrolled checkout, or pass --repo <checkout>."
    if code == NotEnrolled.exit_code:
        return (f"If this is the checkout you want Multithread in, enroll it with "
                f"`{_launcher('setup', '--repo', str(repo), '--apply')}`, then {again}.")
    if code in (64, 73):
        return "Correct that and run this again."
    return f"Check the ledger with `{_launcher('--repo', str(repo), 'doctor')}`, then {again}."


# --- observe: bounded native diagnostics --------------------------------------

_OBSERVE_TIMEOUT = 15
_OBSERVE_RPC_TIMEOUT = 2
_OBSERVE_PAGES = 3
_OBSERVE_PAGE_SIZE = 20
_OBSERVE_EXIT_CODES = {"OBSERVED": 0, "PARTIAL": 3, "UNAVAILABLE": 4, "STALE": 5}


def _observed_at():
    return datetime.now(timezone.utc).isoformat()


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("observation deadline expired")
    return remaining


def _observation_binding(ledger, repo, role, expected, deadline):
    """Read the binding, never history or an inferred latest attempt."""
    code, reply, _ = ledger(repo, "wake-ledger", "show", role, timeout=_remaining(deadline))
    _remaining(deadline)
    if code != 0 or not isinstance(reply, dict):
        raise Unusable("binding_unavailable")
    bindings = reply.get("bindings")
    if (not isinstance(bindings, list) or len(bindings) != 1 or not isinstance(bindings[0], dict)
            or bindings[0].get("role") != role):
        raise Unusable("binding_malformed")
    binding = bindings[0]
    if binding.get("state") not in ("active", "paused", "unbound"):
        raise Unusable("binding_malformed")
    if binding["state"] != "active":
        return None
    if (type(binding.get("generation")) is not int or binding["generation"] <= 0
            or binding.get("provider") not in ("codex", "claude")):
        raise Unusable("binding_malformed")
    if (binding["generation"] != expected["generation"] or binding["provider"] != expected["provider"]
            or binding.get("thread") != expected["thread"]):
        return None
    endpoint = binding.get("endpoint")
    if (not isinstance(endpoint, str) or not endpoint.startswith("unix://") or not endpoint.isprintable()
            or len(endpoint) > 4096 or not Path(endpoint.removeprefix("unix://")).is_absolute()):
        raise Unusable("binding_malformed")
    # History may advance independently of a binding; it is neither a target nor a fence.
    return {key: value for key, value in binding.items() if key != "last_attempt"}


def _observation_runtime(reply, thread_id):
    thread = reply.get("thread") if isinstance(reply, dict) else None
    status = thread.get("status") if isinstance(thread, dict) else None
    kind = status.get("type") if isinstance(status, dict) else None
    flags = status.get("activeFlags") if isinstance(status, dict) else None
    if (not isinstance(thread, dict) or thread.get("id") != thread_id
            or kind not in ("notLoaded", "idle", "systemError", "active")
            or kind == "active" and (not isinstance(flags, list) or not all(
                isinstance(flag, str) and flag in ("waitingOnApproval", "waitingOnUserInput")
                for flag in flags))):
        raise Unusable("runtime_malformed")
    return {"thread_id": thread_id, "status": kind,
            "active_flags": list(flags) if kind == "active" else [], "observed_at": _observed_at()}


def _observation_turns(reply):
    turns = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(turns, list) or len(turns) > 1:
        raise Unusable("turns_malformed")
    latest = None
    if turns:
        turn = turns[0]
        if (not isinstance(turn, dict) or not isinstance(turn.get("id"), str)
                or not turn["id"] or len(turn["id"]) > 1024 or not turn["id"].isprintable()
                or turn.get("status") not in ("inProgress", "completed", "interrupted", "failed")):
            raise Unusable("turns_malformed")
        latest = {"id": turn["id"], "status": turn["status"]}
    return {"latest": latest, "observed_at": _observed_at()}


def _observation_page(reply, seen_ids, seen_cursors):
    if (not isinstance(reply, dict) or not isinstance(reply.get("data"), list)
            or len(reply["data"]) > _OBSERVE_PAGE_SIZE or "nextCursor" not in reply):
        raise Unusable("queue_malformed")
    cursor = reply["nextCursor"]
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 1024
                               or not cursor.isprintable() or cursor in seen_cursors):
        raise Unusable("queue_cursor_malformed")
    entries, page_ids = [], set()
    for entry in reply["data"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                or not re.fullmatch(_UUID, entry["id"]) or entry["id"] in seen_ids | page_ids
                or not isinstance(entry.get("input"), list)
                or not all(isinstance(item, dict) for item in entry["input"])):
            raise Unusable("queue_entry_malformed")
        client_id = entry.get("clientUserMessageId")
        if (not isinstance(client_id, str) or not client_id
                or len(client_id) > 1024 or not client_id.isprintable()):
            raise Unusable("queue_entry_malformed")
        try:
            body = json.dumps(entry["input"], ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise Unusable("queue_input_malformed") from None
        entries.append({"queue_id": entry["id"], "client_user_message_id": client_id,
                        "input_bytes": len(body), "input_sha256": hashlib.sha256(body).hexdigest()})
        page_ids.add(entry["id"])
    return entries, cursor


def observe_role(args, ledger=launcher_ledger):
    """Active, non-loading diagnostics. Pagination and binding guards are not atomic."""
    role = canonical_wake_role(args.role)
    thread = canonical_wake_thread(args.expect_thread)
    if type(args.expect_generation) is not int or args.expect_generation <= 0 or args.expect_provider != "codex":
        raise ValidationError("observation requires a positive generation and provider codex")
    nominated = args.queue_id
    if nominated is not None and (not isinstance(nominated, str) or not re.fullmatch(_UUID, nominated)):
        raise ValidationError("--queue-id must be an exact lowercase UUID")
    expected = {"generation": args.expect_generation, "provider": "codex", "thread": thread}
    deadline = time.monotonic() + _OBSERVE_TIMEOUT
    result = {"schema": 1, "role": role, "expected_binding": expected, "binding": None,
              "started_at": _observed_at(), "runtime": None, "turns": None,
              "queue": {"entries": [], "pages": 0, "complete": False, "scan_status": "unavailable"},
              "original": {"queue_id": nominated, "standing": "unknown", "seen_in_scan": False},
              "binding_guard": {"before": "unavailable", "after": "not_run"}, "problems": [],
              "limits": {"operation_seconds": _OBSERVE_TIMEOUT, "rpc_seconds": _OBSERVE_RPC_TIMEOUT,
                         "pages": _OBSERVE_PAGES, "page_size": _OBSERVE_PAGE_SIZE,
                         "frame_bytes": _MAX_MESSAGE},
              "input_digest_profile": "json-utf8-sort-keys-compact-unescaped-unicode-no-nan-v1",
              "limitations": ["Reads are active diagnostics; no subscription or loading is requested.",
                              "Queue pagination is non-atomic, even when every page is scanned.",
                              "Not seen does not prove pickup, consumption or completion.",
                              "Binding equality after scanning is not an atomic action fence."]}

    def finish(status):
        result.update(status=status, exit_code=_OBSERVE_EXIT_CODES[status],
                      usable=status == "OBSERVED", finished_at=_observed_at())
        if status in ("OBSERVED", "PARTIAL") and nominated is not None:
            result["original"]["standing"] = ("observed" if result["original"]["seen_in_scan"] else
                                                "not_seen" if result["queue"]["complete"] else "unknown")
        return result

    try:
        binding = _observation_binding(ledger, args.repo or os.getcwd(), role, expected, deadline)
    except (Unusable, OSError):
        result["problems"].append("binding_before_unavailable")
        return finish("UNAVAILABLE")
    if binding is None:
        result["binding_guard"]["before"] = "stale"
        return finish("STALE")
    result["binding"] = {"role": role, "state": binding["state"], **expected}
    result["binding_guard"]["before"] = "matched"
    daemon = None
    try:
        daemon = Daemon(binding["endpoint"].removeprefix("unix://"),
                        timeout=min(_OBSERVE_RPC_TIMEOUT, _remaining(deadline)),
                        experimental_api=True, overall_deadline=deadline)
        for key, method, params, validate in (
                ("runtime", "thread/read", {"threadId": thread, "includeTurns": False},
                 lambda reply: _observation_runtime(reply, thread)),
                ("turns", "thread/turns/list", {"threadId": thread, "limit": 1, "sortDirection": "desc"},
                 _observation_turns)):
            try:
                reply = daemon.call(method, params, timeout=min(_OBSERVE_RPC_TIMEOUT, _remaining(deadline)))
                result[key] = validate(reply)
            except (Refused, Unusable, NoAnswer, OSError):
                result["problems"].append(key + "_unavailable")
        cursor, seen_ids, seen_cursors = None, set(), set()
        for _ in range(_OBSERVE_PAGES):
            params = {"threadId": thread, "limit": _OBSERVE_PAGE_SIZE}
            if cursor is not None:
                params["cursor"] = cursor
            try:
                reply = daemon.call("thread/queue/list", params,
                                    timeout=min(_OBSERVE_RPC_TIMEOUT, _remaining(deadline)))
                entries, cursor = _observation_page(reply, seen_ids, seen_cursors)
            except (Refused, Unusable, NoAnswer, OSError):
                result["problems"].append("queue_unavailable")
                break
            queue = result["queue"]
            queue["entries"].extend(entries)
            queue["pages"] += 1
            queue["observed_at"] = _observed_at()
            seen_ids.update(entry["queue_id"] for entry in entries)
            result["original"]["seen_in_scan"] = nominated in seen_ids
            if cursor is None:
                queue["complete"] = True
                break
            seen_cursors.add(cursor)
        queue = result["queue"]
        queue["scan_status"] = ("complete" if queue["complete"] else
                                "partial" if queue["pages"] else "unavailable")
    except (DaemonUnavailable, OSError):
        result["problems"].append("daemon_unavailable")
    finally:
        if daemon is not None:
            daemon.close()
    try:
        after = _observation_binding(ledger, args.repo or os.getcwd(), role, expected, deadline)
    except (Unusable, OSError):
        result["problems"].append("binding_after_unavailable")
        result["binding_guard"]["after"] = "unavailable"
        return finish("UNAVAILABLE")
    if after is None or after != binding:
        result["binding_guard"]["after"] = "stale"
        return finish("STALE")
    result["binding_guard"]["after"] = "matched"
    if result["runtime"] is not None and result["turns"] is not None and result["queue"]["complete"]:
        return finish("OBSERVED")
    if result["runtime"] is not None or result["turns"] is not None or result["queue"]["pages"]:
        return finish("PARTIAL")
    return finish("UNAVAILABLE")


def observe_main(argv=None, *, ledger=launcher_ledger):
    parser = argparse.ArgumentParser(prog="multithread observe", description=(
        "Observe one exact Codex role binding with bounded, active, non-loading native reads. "
        "A complete non-atomic scan is not pickup proof or an action fence."))
    parser.add_argument("role")
    parser.add_argument("--expect-generation", type=int, required=True)
    parser.add_argument("--expect-provider", choices=("codex",), required=True)
    parser.add_argument("--expect-thread", required=True)
    parser.add_argument("--queue-id", help="original native queue UUID to locate; no latest-attempt default")
    parser.add_argument("--repo")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = observe_role(args, ledger=ledger)
    except ValidationError as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    else:
        print(_printable(f"{result['status']}: {result['role']}; queue scan {result['queue']['scan_status']}; "
                         f"original {result['original']['standing']}."))
        print(_printable(f"Expected binding: generation {args.expect_generation}; codex thread "
                         f"{args.expect_thread}; guards before={result['binding_guard']['before']}, "
                         f"after={result['binding_guard']['after']}."))
        if result["runtime"] is not None:
            print(_printable(f"Runtime: {result['runtime']['status']}; "
                             f"wait flags: {','.join(result['runtime']['active_flags']) or 'none'}; "
                             f"observed at {result['runtime']['observed_at']}."))
        if result["turns"] is not None:
            turn = result["turns"]["latest"]
            print(_printable(f"Latest turn: {turn['id']} {turn['status']}." if turn else "Latest turn: none seen."))
        print(f"Queue: {len(result['queue']['entries'])} entries in {result['queue']['pages']} pages.")
        for entry in result["queue"]["entries"]:
            print(_printable(f"Queue UUID {entry['queue_id']}; clientUserMessageId "
                             f"{json.dumps(entry['client_user_message_id'])}; canonical input "
                             f"{entry['input_bytes']} bytes, sha256 {entry['input_sha256']}."))
        if args.queue_id is not None:
            print(f"Nominated original: {args.queue_id}; {result['original']['standing']}.")
        if result["problems"]:
            print("Unavailable: " + ", ".join(result["problems"]) + ".")
        print("Non-atomic diagnostics; this does not prove pickup or authorize another action.")
    return result["exit_code"]


# --- wake ---------------------------------------------------------------------

def fingerprint(path):
    """A task file's sha256 and size, read once: the wake records the bytes it pointed at."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise IsADirectoryError(path)
        digest, size = hashlib.sha256(), 0
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def message_text(agent, ref, ledger, sender=None):
    """The whole wake: who sends it and where the work is, never the work itself."""
    sequence = wake_ref_sequence(ref)
    where = ref if sequence is None else f"ledger sequence {sequence[1]} in {sequence[0] or ledger}"
    context = ""
    if sender is not None:
        label = f"{sender['project']}/{sender['role']}" if sender["project"] else sender["role"]
        context = f" ({label})"
    return f"Multithread wake from {agent}{context}: {where}"


def _sender_context(source, agent, session, role, ledger):
    """Observe one source ledger; a display label never grants coordination authority."""
    if not session:
        return "unavailable", None
    code, reply, problem = ledger(source, "wake-ledger", "show", *([role] if role else []))
    if code != 0:
        return "unavailable", None
    try:
        if not isinstance(reply, dict) or not isinstance(reply.get("bindings"), list):
            raise ValidationError("unusable sender bindings")
        # A missing project field means this source cannot supply the new snapshot.
        project = canonical_wake_project(reply["project"])
        source_ledger = canonical_wake_ref(reply["ledger"])
        if not source_ledger.startswith("/"):
            raise ValidationError("sender ledger must be an absolute path")
        matches = []
        for binding in reply["bindings"]:
            if not isinstance(binding, dict) or binding.get("state") not in ("active", "paused", "unbound"):
                raise ValidationError("unusable sender binding")
            bound_role = canonical_wake_role(binding.get("role"))
            if role is not None and bound_role != role:
                raise ValidationError("sender lookup returned another role")
            if binding["state"] == "unbound":
                continue
            if binding.get("provider") == "codex":
                thread = canonical_wake_thread(binding.get("thread"))
                owns = agent == "codex" and session == thread
            elif binding.get("provider") == "claude":
                owner = canonical_agent(binding.get("bound_agent"))
                owner_session = canonical_agent(binding.get("bound_session"))
                owns = agent == owner and session == owner_session
            else:
                raise ValidationError("unusable sender provider")
            snapshot = canonical_wake_sender({"ledger": source_ledger, "role": bound_role,
                                              "generation": binding.get("generation"), "project": project})
            if owns:
                matches.append(snapshot)
        if len(matches) > 1:
            return "ambiguous", None
        return ("observed", matches[0]) if matches else ("unbound", None)
    except (KeyError, TypeError, ValidationError):
        return "unavailable", None


def _native(value):
    """A native identifier the ledger can hold, or None."""
    try:
        return canonical_wake_message_id(value)
    except ValidationError:
        return None


def _next_id(message_id):
    """The id that sends the same reference again: .2, .3 and on, within the 128-character bound."""
    stem, dot, number = message_id.rpartition(".")
    stem, count = (stem, int(number) + 1) if dot and stem and number.isdigit() else (message_id, 2)
    suffix = f".{count}"
    if len(suffix) >= 128:  # The counter would leave no leading character: count again from this id.
        stem, suffix = message_id, ".2"
    return stem[:128 - len(suffix)] + suffix


def wake_status(args, ledger=launcher_ledger):
    """Read durable attempts without reading a referenced file or contacting its recipient."""
    repo = Path(args.repo or os.getcwd()).absolute()
    try:
        role = canonical_wake_role(args.role)
        ref = canonical_wake_ref(args.ref) if args.ref is not None else None
    except ValidationError as exc:
        return _outcome("NOT SENT", f"Status wasn't read: {exc}.", "Correct that and run this again.")
    code, history, problem = ledger(repo, "wake-ledger", "history", role,
                                    *(["--ref", ref] if ref is not None else []))
    if code != 0:
        return _outcome("NOT SENT", f"The ledger at {repo} couldn't report wake status: {problem}.",
                        _ledger_next(code, repo, "read status again"), role=role, ref=ref)
    return _outcome("STATUS", f"Read recorded wake attempts for {role}. Nothing was sent; recipient reachability "
                    "and current turn state were not probed.",
                    "Inspect each attempt's outcome and consumption_state; transport acceptance is not consumption.",
                    role=role, ref=ref, history=history, ledger=history.get("ledger", str(repo)),
                    consumption_state=(history["attempts"][0]["consumption_state"] if history.get("attempts") else "unknown"))


def wake(args, ledger=launcher_ledger):
    cwd = os.getcwd()
    repo = Path(args.repo or cwd).absolute()
    source = Path(getattr(args, "sender_repo", None) or cwd).absolute()
    requested = "steer" if args.steer else "queue"
    try:
        sender_role = getattr(args, "sender_role", None)
        if sender_role is not None:
            sender_role = canonical_wake_role(sender_role)
        agent, session = _actor(args, session_required=not args.dry_run or sender_role is not None)
        role = canonical_wake_role(args.role)
        ref = canonical_wake_ref(args.ref)
        if args.message_id is not None:
            canonical_wake_message_id(args.message_id)
        fields = ("generation", "provider", "thread", "bound_agent", "bound_session")
        expected_binding = canonical_wake_expectation({
            field: getattr(args, "expect_" + field, None)
            for field in fields if getattr(args, "expect_" + field, None) is not None
        } or None)
    except ValidationError as exc:
        return _outcome("NOT SENT", f"The wake wasn't sent: {exc}.", "Correct that and run this again.")
    content = []
    if wake_ref_sequence(ref) is None:
        try:
            sha256, size = fingerprint(ref)
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return _outcome("NOT SENT", f"--ref names {ref}, which isn't a file here, so the recipient couldn't "
                            "open it. Nothing was sent.", "Pass the task file's exact absolute path, or a ledger "
                            "sequence number, then run this again.")
        except OSError as exc:
            return _outcome("NOT SENT", f"--ref names {ref}, which couldn't be read ({_detail(exc)}). Nothing was "
                            "sent.", "Make the file readable, or pass another task file, then run this again.")
        content = ["--ref-sha256", sha256, "--ref-size", str(size)]

    sender_state, sender = _sender_context(source, agent, session, sender_role, ledger)
    if sender_role is not None and sender_state != "observed":
        return _outcome("NOT SENT", f"The source ledger at {source} did not confirm {sender_role} for this "
                        f"sender session ({sender_state}). Nothing was recorded or sent.",
                        "Read the source binding and reconcile --sender-role, --sender-repo and the exact "
                        "sender identity before sending again.", sender_state=sender_state)
    step = ["plan", role] if args.dry_run else ["begin", role, "--agent", agent, "--session", session]
    sender_arguments = ["--sender-json", json.dumps(sender)] if sender is not None and not args.dry_run else []
    expectation = [argument for field, value in (expected_binding or {}).items()
                   for argument in ("--expect-" + field.replace("_", "-"), str(value))]
    code, decision, problem = ledger(repo, "wake-ledger", *step, "--ref", ref, "--requested", requested, *content,
                                     *(["--id", args.message_id] if args.message_id else []), *expectation, *sender_arguments)
    if code != 0:
        next_step = ("Read the role's binding again and reconcile the expected recipient deliberately; "
                     "do not retry automatically."
                     if code == 73 and expected_binding is not None
                     else _ledger_next(code, repo, "run this again"))
        return _outcome("NOT SENT", f"The ledger at {repo} couldn't {'plan' if args.dry_run else 'record'} "
                        f"this wake: {problem}. Nothing was sent.", next_step)
    status, binding, message_id = decision["status"], decision["binding"], decision["message_id"]
    base = {"role": role, "ref": ref, "requested": requested, "ledger": decision["ledger"],
            "message_id": message_id,
            "sender_state": sender_state, **({"sender": sender} if sender is not None else {}),
            "recipient_state": {"reachability": "unknown", "turn_state": "unknown", "source": "unobserved"},
            **({"ref_sha256": content[1], "ref_size": int(content[3])} if content else {})}
    if status == "unbound":
        return _outcome("NOT SENT", f"No conversation is bound to {role} in the ledger at {decision['ledger']}. "
                        "Nothing was sent.", f"Bind one with `multithread bind {role} --thread <codex conversation "
                        "id>`, or pass --repo for the ledger that holds this binding.", **base)
    base.update(provider=binding["provider"], thread=binding["thread"], generation=binding["generation"])
    if status == "paused":
        return _outcome("NOT SENT", f"Wakes to {role} are paused (binding {binding['generation']}, paused at "
                        f"ledger seq {binding['paused_seq']}). Nothing was sent.",
                        f"Resume with `multithread resume {role}`, then send again; message id {message_id} "
                        "is still unused.", **base)
    if status == "already_sent":
        prior = decision["prior"]
        # A later source binding cannot relabel an immutable original attempt.
        base.pop("sender", None)
        base["sender_state"] = "observed" if prior.get("sender") is not None else "unavailable"
        if prior.get("sender") is not None:
            base["sender"] = prior["sender"]
        again = _next_id(message_id)
        unchanged = (f", and {ref} hasn't changed since" if content and prior.get("ref_sha256") == content[1]
                     else "")
        if prior["outcome"] in ("queued", "steered", "delivered"):
            return _outcome("ALREADY SENT", f"Message {message_id} went out at {prior['at']} ({prior['outcome']}, "
                            f"ledger seq {prior['seq']}){unchanged}; nothing was sent again.",
                            f"To send it again on purpose, add --id {again}; changed content gets its own id.",
                            prior=prior, **base)
        state = "is uncertain" if prior["outcome"] == "uncertain" else "never recorded its outcome"
        return _outcome("ALREADY SENT", f"An earlier attempt with message id {message_id} (ledger seq "
                        f"{prior['seq']}, {prior['at']}) {state}, so it may have arrived. Nothing was sent again.",
                        f"Check the recipient's conversation first. If it didn't arrive, send it with --id {again}.",
                        prior=prior, **base)

    text = message_text(agent, ref, decision["ledger"], sender)
    base.update(text=text)
    endpoint = binding["endpoint"]
    path = endpoint.removeprefix("unix://")
    attempt = decision.get("attempt_seq")
    base.update(attempt_seq=attempt)

    def conclude(result, outcome, reason, transport=None, native_id=None, detail=None):
        result.update(outcome=outcome, reason=reason, transport=transport, native_id=native_id)
        if detail:
            result["detail"] = detail
        if attempt is None:
            return result
        extra = [*(["--transport", transport] if transport else []),
                 *(["--native-id", native_id] if native_id else []),
                 *(["--detail", detail] if detail else [])]
        code, receipt, problem = ledger(repo, "wake-ledger", "conclude", str(attempt), "--agent", agent,
                                        "--session", session, "--outcome", outcome, "--reason", reason, *extra)
        if code == 0:
            result["concluded_seq"] = receipt["event"]["seq"]
        else:
            result["warning"] = (f"The ledger didn't record this outcome ({problem}). Attempt {attempt} stays open, "
                                 f"so message id {message_id} reports ALREADY SENT until someone checks it.")
        return result

    if binding["provider"] == "claude":
        return _wake_inbox(args, role, binding, text, message_id, base, conclude)
    try:
        daemon = Daemon(path)
    except DaemonUnavailable as exc:
        return conclude(_outcome("NOT SENT", f"Couldn't reach the Codex daemon at {path} ({exc}). Nothing was "
                                 "sent.", "Check `codex app-server daemon version`; retrying with the same message "
                                 "id is safe.", **base), "not_sent", "daemon_unreachable")
    declined = None  # (reason, sentence, Codex's message) once Codex refused the steer before accepting it
    unsure = ("Don't resend blindly: check the recipient's conversation or ask them. If it didn't arrive, send it "
              f"with --id {_next_id(message_id)}.")
    retry = "Check `codex app-server daemon version`; retrying with the same message id is safe."
    try:
        try:
            listing = daemon.call("thread/turns/list", {"threadId": binding["thread"], "limit": 1,
                                                        "sortDirection": "desc"})
            turns = listing.get("data") if isinstance(listing, dict) else None
            if not isinstance(turns, list) or not all(isinstance(turn, dict) for turn in turns):
                raise Unusable("its turn list was not a list of turns")
        except Refused as exc:
            if exc.code == _INVALID_REQUEST and exc.raw.startswith(("thread not found", "invalid thread id")):
                return conclude(_outcome("NOT SENT", f"The daemon doesn't know conversation {binding['thread']} "
                                         f"({exc}). Nothing was sent.", f"Check the binding with `multithread bind "
                                         f"{role}`: the conversation may be archived, or the id is wrong. If it "
                                         "moved, have the exact holder refresh its binding, or make an explicitly "
                                         "authorized handover with --replace, --expected-generation, --reason and "
                                         "--approval-ref. Then run this wake again.", **base), "not_sent", "conversation_unknown",
                                detail=str(exc))
            return conclude(_outcome("NOT SENT", f"The daemon refused to list conversation {binding['thread']}'s "
                                     f"turns ({exc}). Nothing was sent.", retry, **base), "not_sent",
                            "daemon_refused", detail=str(exc))
        except Unusable as exc:
            return conclude(_outcome("NOT SENT", f"The Codex daemon at {path} answered the turn list unreadably "
                                     f"({exc}). Nothing was sent.", retry, **base), "not_sent", "daemon_unreadable",
                            detail=str(exc))
        except NoAnswer as exc:
            return conclude(_outcome("NOT SENT", f"The Codex daemon at {path} stopped answering ({exc}). Nothing "
                                     "was sent.", retry, **base), "not_sent", "daemon_unreachable")
        latest = turns[0] if turns else None
        turn_id = latest.get("id") if latest else None
        live = isinstance(turn_id, str) and latest.get("status") == "inProgress"
        observed_status = latest.get("status") if latest else None
        base["recipient_state"] = {"reachability": "reachable", "turn_state":
                                   observed_status if observed_status in ("inProgress", "completed", "interrupted", "failed")
                                   else "unknown" if latest else "no_turns",
                                   "source": "codex_turn_list"}
        if args.dry_run:
            plan = f"steer into running turn {turn_id}" if args.steer and live else "queue"
            seen = (f"latest turn {turn_id} is {latest.get('status')}" if latest
                    else "the conversation has no turns yet")
            return _outcome("DRY RUN", f"Would {plan} for {role} (conversation {binding['thread']}; {seen}). "
                            "Nothing was sent.", "Run again without --dry-run to send.", **base)
        if args.steer and live:
            try:
                receipt = daemon.call("turn/steer", {
                    "threadId": binding["thread"], "expectedTurnId": turn_id, "clientUserMessageId": message_id,
                    "input": [{"type": "text", "text": text}]}, timeout=_STEER_TIMEOUT)
            except Refused as exc:
                declined = next(((reason, sentence.format(exc.message), exc.message)
                                 for pattern, reason, sentence in _STEER_DECLINED
                                 if exc.code == _INVALID_REQUEST and pattern.fullmatch(exc.raw)), None)
                if declined is None:
                    return conclude(_outcome("UNCERTAIN", f"Codex answered the steer with an error that doesn't show "
                                             f"it was refused before acceptance ({exc}). It may or may not have "
                                             "arrived.", unsure, **base), "uncertain", "unrecognized_error", "steer",
                                    detail=str(exc))
            except Unusable as exc:
                return conclude(_outcome("UNCERTAIN", f"The daemon answered the steer unreadably ({exc}). It may or "
                                         "may not have arrived.", unsure, **base), "uncertain", "unusable_reply",
                                "steer", detail=str(exc))
            except NoAnswer as exc:
                return conclude(_outcome("UNCERTAIN", f"The steer was sent, but the daemon didn't answer ({exc}). "
                                         "It may or may not have arrived.", unsure, **base), "uncertain",
                                "no_answer", "steer")
            else:
                answered = receipt.get("turnId") if isinstance(receipt, dict) else None
                if answered != turn_id:
                    what = ("a receipt for turn " + _line(answered, 80) if isinstance(answered, str)
                            else "no turn id" if isinstance(receipt, dict) else "no receipt object")
                    return conclude(_outcome("UNCERTAIN", f"The daemon answered the steer with {what}, not a "
                                             f"receipt for turn {turn_id}. It may or may not have arrived.", unsure,
                                             **base), "uncertain", "unusable_reply", "steer", detail=what)
                return conclude(_outcome("STEERED", f"Codex accepted the steer for running turn {turn_id}. "
                                         "Recipient consumption is unobserved.", _WAIT, **base),
                                "steered", "live_turn", "steer", _native(turn_id))
        # History alone does not show whether Codex has a queue consumer loaded.
        # This read cannot load/resume a thread or override an intentional wait.
        base["recipient_runtime"] = _observe_runtime(daemon, binding["thread"])
    finally:
        daemon.close()

    reason = "requested" if not args.steer else declined[0] if declined else "no_live_turn"
    why = {"requested": "Queued as asked.",
           "no_live_turn": "No running turn was observed, so it was queued instead of steered."}.get(reason)
    why = why or declined[1]
    first = f" The steer was declined first (Codex: {declined[2]})." if declined else ""
    codex = args.codex or shutil.which("codex")
    if not codex or not os.path.isabs(codex) or not os.access(codex, os.X_OK):
        return conclude(_outcome("NOT SENT", "Codex wasn't found" + (f" at {codex}" if codex else " on PATH")
                                 + ", so nothing was queued." + first, "Pass --codex with Codex's absolute "
                                 "path, or put codex on PATH, then retry with the same message id.", **base),
                        "not_sent", "codex_unavailable", "queue")
    try:
        completed = subprocess.run([codex, "queue", "--remote", endpoint, "--thread", binding["thread"],
                                    "--message", text], stdin=subprocess.DEVNULL, capture_output=True,
                                   timeout=_QUEUE_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return conclude(_outcome("UNCERTAIN", f"codex queue didn't finish within {_QUEUE_TIMEOUT} s. The message "
                                 "may or may not have been queued." + first, unsure, **base), "uncertain",
                        "no_answer", "queue")
    except OSError as exc:  # It never started, so it sent nothing.
        return conclude(_outcome("NOT SENT", f"Couldn't run {codex} ({_detail(exc)}). Nothing was queued."
                                 + first, "Pass --codex with Codex's absolute path, then retry with the same "
                                 "message id.", **base), "not_sent", "codex_unavailable", "queue")
    errors = completed.stderr.decode("utf-8", "replace")
    said = _line(errors.strip() or completed.stdout.decode("utf-8", "replace").strip()
                 or f"exit {completed.returncode}")
    if completed.returncode == 0:
        try:
            output = completed.stdout.decode("utf-8")
        except UnicodeDecodeError:
            output = ""
        receipt = re.search(rf"^Queued message ({_UUID}) for thread {re.escape(binding['thread'])}\.$", output,
                            re.MULTILINE)
        if receipt is None:
            return conclude(_outcome("UNCERTAIN", "codex queue reported success but printed no queue receipt for "
                                     f"conversation {binding['thread']} ({said}). The message may or may not have "
                                     "been queued." + first, unsure, **base), "uncertain", "no_receipt", "queue",
                            detail=said)
        native_id = receipt.group(1)
        readiness, next_step, detail = _queued_readiness(base, native_id)
        return conclude(_outcome("QUEUED", why + " Codex accepted the queue entry; a recipient turn and "
                                 "consumption are unobserved." + readiness, next_step, **base),
                        "queued", reason, "queue", native_id, detail=detail)
    if _refused_before_sending(completed):
        return conclude(_outcome("NOT SENT", f"codex queue refused before sending: {said}. Nothing was queued."
                                 + first, "Check `codex app-server daemon version`, then retry with the same message "
                                 "id.", **base), "not_sent", "queue_refused", "queue", detail=said)
    ended = (f"exited {completed.returncode}" if completed.returncode > 0
             else f"was stopped by signal {-completed.returncode}")
    return conclude(_outcome("UNCERTAIN", f"codex queue {ended} after it may have sent the message ({said}). The "
                             "message may or may not have been queued." + first, unsure, **base), "uncertain",
                    "queue_failed", "queue", detail=f"{ended}: {said}"[:300])


def _refused_before_sending(completed):
    """Only codex queue's whole, cleanly decoded stop before sending, with nothing on stdout, frees the id."""
    if completed.returncode != 1 or completed.stdout.strip():
        return False
    try:
        return _QUEUE_REFUSED_BEFORE_SENDING.fullmatch(completed.stderr.decode("utf-8")) is not None
    except UnicodeDecodeError:
        return False


def _wake_inbox(args, role, binding, text, message_id, base, conclude):
    """Claude Code: one line into the session's inbox. There is no separate steer.

    The binding names a session; its inbox is the one that session last reported from inside itself, while the
    process that reported it still runs, and that process must be the one listening when the line is sent. Anything
    else is a session that isn't running here, never a delivery to whichever session took over its socket."""
    unsteered = " Claude Code has no separate steer, so --steer changed nothing." if args.steer else ""
    rebind = (f"Have the exact holder refresh {role} from its own Claude Code session with "
              "--claude-socket \"$CLAUDE_CODE_MESSAGING_SOCKET\". Moving it to another owner requires an explicitly "
              "authorized handover with --replace, --expected-generation, --reason and --approval-ref. "
              "Then run this wake again.")
    session = binding.get("bound_session")

    def not_running(why):
        base["recipient_state"] = {"reachability": "unavailable", "turn_state": "unknown", "source": "inbox_owner"}
        return conclude(_outcome(
            "NOT RUNNING", f"The Claude Code session {session} bound to {role} isn't running here: {why}. Nothing "
            "was sent.", f"It becomes reachable at its next prompt, when it reports its inbox; a session resumed "
            f"with `claude --resume` keeps {role}. Send again then; message id {message_id} is still unused.", **base),
            "not_sent", "inbox_missing", detail="not running")
    entry = live_inbox(session)
    if entry is None:
        return not_running("no inbox it reported is still owned by its process")
    path = entry["inbox"]
    problem = inbox_problem(path)
    if problem is not None:
        reason, why = problem
        gone = (f"The Claude Code inbox at {path} is missing; the session's state is unknown."
                if reason == "inbox_missing" else f"The Claude Code inbox at {path} failed its checks: {why}.")
        base["recipient_state"] = {"reachability": "unavailable", "turn_state": "unknown", "source": "inbox_path"}
        return conclude(_outcome("NOT SENT", gone + " Nothing was sent.", rebind, **base), "not_sent", reason)
    if args.dry_run:
        return _outcome("DRY RUN", f"Would deliver to the Claude Code inbox at {path} for {role}. Nothing was "
                        "sent." + unsteered, "Run again without --dry-run to send.", **base)
    try:
        deliver(path, text, owner=(entry["pid"], entry["boot_id"], entry["start"]))
    except InboxNotOwner as exc:
        return not_running(f"the inbox it reported, {path}, is now held by another process ({exc})")
    except InboxRefused as exc:
        return conclude(_outcome("NOT SENT", f"The Claude Code inbox at {path} refused the connection ({exc}), so "
                                 "that session may have ended. Nothing was sent.", rebind, **base),
                        "not_sent", "inbox_refused", "inbox")
    except NoAnswer as exc:
        base["recipient_state"] = {"reachability": "reachable", "turn_state": "unknown", "source": "inbox_connection"}
        return conclude(_outcome("UNCERTAIN", f"The connection to the Claude Code inbox at {path} dropped while "
                                 f"sending ({exc}). The message may or may not have arrived.", "Don't resend "
                                 "blindly: check the recipient's session or ask them. If it didn't arrive, send it "
                                 f"with --id {_next_id(message_id)}.", **base), "uncertain", "dropped", "inbox")
    base["recipient_state"] = {"reachability": "reachable", "turn_state": "unknown", "source": "inbox_connection"}
    return conclude(_outcome("DELIVERED TO INBOX", f"The Claude Code inbox at {path} accepted the message. "
                             "Recipient turn state and consumption are unobserved." + unsteered,
                             "Wait for the recipient's acknowledgement. The session may still hold or refuse the "
                             "message under its inbound settings (crossSessionInbound); if nothing arrives, ask "
                             "the person to check that session.", **base), "delivered", "inbox_accepted", "inbox")


def wake_main(argv=None, *, ledger=launcher_ledger):
    parser = argparse.ArgumentParser(prog="multithread wake", description=(
        "Send a compact agent and observed project/role pointer to what a role is bound to. A Codex conversation gets it "
        "through the shared Codex daemon, queued by default; --steer submits it to the observed running turn "
        "instead, and queues when none is running. A Claude Code session gets it in its inbox. Acceptance "
        "does not establish a new turn or consumption. Each attempt is recorded in this ledger; --status "
        "reads its original recipient and explicit acknowledgement. Exit 0 sent, status or dry run, 3 already "
        "sent, 4 not sent, 5 uncertain."))
    parser.add_argument("role", help="the bound role, for example operator")
    parser.add_argument("--ref", help="absolute task-file path, or a ledger sequence number; filters history with --status")
    parser.add_argument("--status", action="store_true", help="read recorded attempts and acknowledgements; use --ref to filter; never read the task file or send")
    parser.add_argument("--steer", action="store_true",
                        help="for news about a Codex recipient's current work: fold into the running turn")
    parser.add_argument("--id", dest="message_id",
                        help="send/dry-run message id; status is filtered with --ref; default: derived from this ledger, the role, its binding and the ref")
    parser.add_argument("--dry-run", action="store_true", help="decide and report; send and record nothing")
    parser.add_argument("--expect-generation", type=int, help="require this binding generation before recording or sending")
    parser.add_argument("--expect-provider", choices=("codex", "claude"), help="expected recipient provider; requires generation and recipient")
    parser.add_argument("--expect-thread", help="expected Codex conversation; requires generation and provider")
    parser.add_argument("--expect-bound-agent", help="expected Claude binding's recorded owner agent; requires generation, provider and owner session")
    parser.add_argument("--expect-bound-session", help="expected Claude binding's recorded owner session")
    parser.add_argument("--agent", help="sender named in the wake; default RELAY_AGENT")
    parser.add_argument("--session", help="sender's session for the ledger record; default RELAY_SESSION")
    parser.add_argument("--sender-repo", help="source checkout for sender-role observation; default: original current directory")
    parser.add_argument("--sender-role", help="select and require this exact sender role when a session holds several")
    parser.add_argument("--codex", help="absolute Codex executable for codex queue; default: PATH")
    parser.add_argument("--repo", help="checkout whose ledger holds the binding; default: current directory")
    parser.add_argument("--json", action="store_true", help="one JSON object with the same outcome")
    args = parser.parse_args(argv)
    if not args.status and args.ref is None:
        parser.error("--ref is required unless --status is used")
    if args.status and (args.steer or args.dry_run or args.message_id is not None
                        or args.sender_repo is not None or args.sender_role is not None
                        or any(getattr(args, "expect_" + field) is not None for field in
                               ("generation", "provider", "thread", "bound_agent", "bound_session"))):
        parser.error("--status cannot be combined with sending, sender-selection or recipient-guard flags; "
                     "--id is for sending/dry-run; use --ref to filter recorded attempts")
    return _emit(wake_status(args, ledger) if args.status else wake(args, ledger), args.json)


# --- signal --wake ---------------------------------------------------------------

# Kinds that enter the recipient's inbox, so a woken session finds them pending.
_DELIVERABLE = ("work.handoff", "work.blocked", "review.requested")


def _receives(binding, agent, session):
    """Whether this binding's recipient is exactly agent/session.

    A Codex binding delivers to its conversation, whoever bound it; a Claude
    Code binding delivers to the session that bound its own inbox.
    """
    if binding.get("state") not in ("active", "paused"):
        return False
    if binding.get("provider") == "codex":
        return agent == "codex" and binding.get("thread") == session
    return binding.get("bound_agent") == agent and binding.get("bound_session") == session


# --- Owner-only hint files beside the ledgers' state: the recipient index and the inbox map ---------
# Both are hints that every use verifies, so a failure costs a lookup, never a wrong action. A file that is
# unreadable, loosely held, oversized or malformed reads as empty and is set aside whole by its next writer.

_PRIVATE_MAX_BYTES = 1 << 20


def _state_dir():
    """Beside the ledgers it describes: RELAY_HOME when one names their state, else the account's."""
    if os.environ.get("RELAY_HOME"):
        return Path(os.path.abspath(os.path.expanduser(os.environ["RELAY_HOME"])))
    # The account's home, as the launcher's registry finds it: an ambient HOME could point into a checkout.
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "share" / "relay"


def _private_read(path, valid):
    """(value, usable): absent is usable and empty; valid(value) returns the normalized value, or None to refuse."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)  # a FIFO never blocks
    except FileNotFoundError:
        return {}, True
    except OSError:
        return {}, False
    try:
        status = os.fstat(fd)
        if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid() or status.st_mode & 0o077
                or status.st_size > _PRIVATE_MAX_BYTES):
            return {}, False
        value = json.loads(os.read(fd, status.st_size + 1))
    except (OSError, ValueError, RecursionError):
        return {}, False
    finally:
        os.close(fd)
    value = valid(value) if isinstance(value, dict) else None
    return ({}, False) if value is None else (value, True)


def _private_update(path, read_file, change, wait=5):
    """Replace the file with change(value) under its lock, waiting at most wait seconds for it; False when it could
    not be written. change may return None to leave the file untouched."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = os.lstat(path.parent)
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid():
            return False  # a symbolic link or someone else's directory
        lock = os.open(path.with_suffix(".lock"),
                       os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    except OSError:
        return False
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.02)
        value, usable = read_file(path)
        changed = change(value)
        if changed is None:
            return True
        if not usable:
            os.replace(path, path.with_name(f"{path.name}.unusable-{time.time_ns()}"))
        fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(changed, stream)  # insertion order is recency: oldest first
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True
    except OSError:
        return False
    finally:
        os.close(lock)


# --- Claude Code inboxes by session -----------------------------------------------------------
# Where each Claude Code session's inbox is now, and whether the process that owns it is still that session.
#
# A role binding names a session, which survives `claude --resume`. Its inbox socket is named after the session's
# process id, so it moves on every restart and, once process ids start over, can belong to another session. Each
# session's Multithread hook therefore reports its inbox together with its process's identity (boot, pid and start
# time), and a wake delivers only to a socket whose process is still exactly that one. The map is a hint like the
# recipient index: a wrong entry can only be refused, never deliver to a session it doesn't name.

INBOXES = None  # tests set an exact path
PROC = Path("/proc")  # tests point this at a synthetic tree
_INBOXES_KEPT = 256  # most recently reporting sessions
_SOCKET_NAME = re.compile(r"([1-9][0-9]{0,9})\.sock")
_INBOX_ANCESTRY = 64  # hook → shell → … → Claude Code
_HOOK_WAIT = 0.5  # the provider hook has seconds in all; a busy map costs one report, never the hook


def _inboxes_path():
    return Path(INBOXES) if INBOXES is not None else _state_dir() / "claude-inboxes.json"


def _valid_inboxes(value):
    entry = lambda item: (isinstance(item, dict) and set(item) == {"inbox", "boot_id", "pid", "start"}
                          and isinstance(item["inbox"], str) and item["inbox"].startswith("/")
                          and isinstance(item["boot_id"], str) and type(item["pid"]) is int
                          and type(item["start"]) is int)
    return value if all(isinstance(key, str) and entry(item) for key, item in value.items()) else None


def _read_inboxes(path):
    return _private_read(path, _valid_inboxes)


def _stat_fields(pid):
    text = (PROC / str(pid) / "stat").read_text()
    return text[text.rindex(")") + 2:].split()  # fields from the third on; the name may hold spaces and ')'


def _process(pid):
    """(boot id, start time in clock ticks) of a live process, or None."""
    try:
        boot = (PROC / "sys/kernel/random/boot_id").read_text().strip()
        return boot, int(_stat_fields(pid)[19])
    except (OSError, ValueError, IndexError):
        return None


def _inbox_owner(inbox):
    match = _SOCKET_NAME.fullmatch(Path(inbox).name) if isinstance(inbox, str) and os.path.isabs(inbox) else None
    return int(match[1]) if match else None


def _ancestors():
    pid, seen = os.getpid(), []
    for _ in range(_INBOX_ANCESTRY):
        try:
            pid = int(_stat_fields(pid)[1])
        except (OSError, ValueError, IndexError):
            break
        if pid <= 1:
            break
        seen.append(pid)
    return seen


def _owns(entry):
    """Whether the process an entry names still runs: same boot, process id and start time."""
    return _process(entry["pid"]) == (entry["boot_id"], entry["start"])


def remember_inbox(session, inbox, claim=False, wait=5):
    """Record that session's inbox is now inbox, as reported from inside it: the process the socket is named after
    must be an ancestor of this one. Only a session's start may claim a socket another live session holds (one
    process can switch sessions with /clear or /resume); a later report or a bind only creates or refreshes, so a
    delayed report from a session that was switched away can't take its socket back. False when nothing was
    recorded; a failure costs only the lookup."""
    pid = _inbox_owner(inbox)
    identity = _process(pid) if pid is not None and pid in _ancestors() else None
    if not isinstance(session, str) or not session or identity is None:
        return False
    entry = {"inbox": inbox, "boot_id": identity[0], "pid": pid, "start": identity[1]}
    if _read_inboxes(_inboxes_path())[0].get(session) == entry:
        return True  # unchanged: a prompt costs one read and no lock
    refused = []

    def change(entries):
        if entries.get(session) == entry:
            return None
        holders = [key for key, item in entries.items() if key != session and item["inbox"] == inbox and _owns(item)]
        if holders and not claim:
            refused.append(holders[0])
            return None
        kept = {key: item for key, item in entries.items() if key != session and item["inbox"] != inbox}
        if len(kept) >= _INBOXES_KEPT:  # make room from sessions whose process has gone before live ones
            dead = [key for key, item in kept.items() if not _owns(item)]
            for key in (dead + list(kept))[:len(kept) - _INBOXES_KEPT + 1]:
                kept.pop(key, None)
        return {**kept, session: entry}
    return _private_update(_inboxes_path(), _read_inboxes, change, wait) and not refused


def live_inbox(session):
    """The entry for the session's reported inbox if the process that reported it still owns it, else None."""
    entry = _read_inboxes(_inboxes_path())[0].get(session) if isinstance(session, str) else None
    return entry if entry is not None and _owns(entry) else None


# Where each recipient holds a role, so a signal recorded in one checkout's ledger can wake it in its own. A hint
# only: every use is verified against that ledger's binding, so the index never decides who is woken.
# It lives beside the ledgers it describes: RELAY_HOME when one names their state, else the account's.
RECIPIENTS = None  # tests set an exact path
_RECIPIENT_PLACES = 8
_RECIPIENTS_KEPT = 256  # most recently bound recipients; sessions come and go


def _recipients_path():
    if RECIPIENTS is not None:
        return Path(RECIPIENTS)
    return _state_dir() / "wake-recipients.json"


def _recipient(provider, agent, session, thread):
    return json.dumps(["codex", thread] if provider == "codex" else ["claude", agent, session])


def _valid_places(value):
    place = lambda item: (isinstance(item, dict) and set(item) == {"checkout", "role"}
                          and isinstance(item["checkout"], str) and item["checkout"].startswith("/")
                          and isinstance(item["role"], str))
    if not all(isinstance(key, str) and isinstance(items, list) and all(place(item) for item in items)
               for key, items in value.items()):
        return None  # set aside whole, never partly kept
    return {key: items[:_RECIPIENT_PLACES] for key, items in value.items()}


def _read_places(path):
    """The index and whether it was usable: absent is usable and empty; unreadable, loosely held, oversized
    or malformed is not."""
    return _private_read(path, _valid_places)


def _places():
    return _read_places(_recipients_path())[0]


def _rewrite_places(change):
    """Replace the index with change(places) under its lock; False when it could not be written, which costs only
    the cross-ledger lookup. An unusable index is set aside, never overwritten."""
    return _private_update(_recipients_path(), _read_places, change)


def _placed(places, recipient, place):
    """places with recipient's newest place first and recipient itself newest, within both bounds."""
    kept = [place, *(item for item in places.pop(recipient, []) if item != place)][:_RECIPIENT_PLACES]
    return {**dict(list(places.items())[-(_RECIPIENTS_KEPT - 1):]), recipient: kept}


def _remember(recipient, checkout, role):
    """Record where a recipient holds a role."""
    return _rewrite_places(lambda places: _placed(places, recipient, {"checkout": checkout, "role": role}))


def rebuild_index(ledger=launcher_ledger, checkouts=None):
    """Add every active or paused binding in every enrolled checkout's ledger to the index, so bindings made before
    it existed are found without each holder binding again. One read per ledger; only the index is written.

    It only fills gaps: bind decides recency, so whatever a bind records during the scan keeps its place. Nothing
    is removed, since the index is a hint verified at every use and a stale place costs one read. Places found now
    follow a recipient's indexed ones, newest binding first; recipients new to the index join at its old end, by
    newest binding. The bounds apply once, at the end."""
    skipped = []
    if checkouts is None:
        try:
            checkouts, unreadable = Registry.for_account().checkouts()
        except EnrollmentError as exc:
            return {"schema": 1, "status": "NOT REBUILT", "exit_code": 1, "ledgers": 0, "places": 0, "dropped": 0,
                    "skipped": [], "gone": [], "happened": f"The account's enrollments could not be read ({exc}); the index "
                                               "is unchanged."}
        skipped += [{"checkout": None, "problem": f"enrollment record {name}: {problem}"}
                    for name, problem in unreadable]
    read, found, gone = 0, [], []
    for checkout in checkouts:
        try:
            os.lstat(checkout)
        except (FileNotFoundError, NotADirectoryError):  # positively absent: a removed checkout's enrollment
            gone.append(str(checkout))
            continue
        except OSError as exc:  # present or not, it can't be read: unavailable is not absent
            skipped.append({"checkout": str(checkout), "problem": f"it could not be inspected ({exc.strerror})"})
            continue
        code, shown, problem = ledger(Path(checkout), "wake-ledger", "show")
        if (code != 0 or not isinstance(shown, dict) or not isinstance(shown.get("bindings"), list)
                or not isinstance(shown.get("ledger"), str)):
            skipped.append({"checkout": str(checkout), "problem": problem or "its answer listed no bindings"})
            continue
        read += 1
        for binding in shown["bindings"]:
            if not isinstance(binding, dict) or binding.get("state") not in ("active", "paused"):
                continue
            if binding.get("provider") == "codex":
                names, recipient = [binding.get("thread")], _recipient("codex", None, None, binding.get("thread"))
            else:
                names = [binding.get("bound_agent"), binding.get("bound_session")]
                recipient = _recipient("claude", *names, None)
            if all(isinstance(name, str) and name for name in names) and isinstance(binding.get("role"), str):
                found.append((str(binding.get("bound_at") or ""), recipient,
                              {"checkout": shown["ledger"], "role": binding["role"]}))
    fresh, newest = {}, {}
    for bound_at, recipient, place in sorted(found, key=lambda item: item[0], reverse=True):
        if place not in fresh.setdefault(recipient, []):
            fresh[recipient].append(place)
        newest.setdefault(recipient, bound_at)
    dropped = 0

    def change(places):
        nonlocal dropped
        merged = {key: fresh[key] for key in sorted(fresh, key=newest.get) if key not in places}
        for key, items in places.items():
            merged[key] = items + [item for item in fresh.get(key, []) if item not in items]
        bounded = {key: items[:_RECIPIENT_PLACES] for key, items in list(merged.items())[-_RECIPIENTS_KEPT:]}
        dropped = sum(map(len, merged.values())) - sum(map(len, bounded.values()))
        return bounded

    written = _rewrite_places(change)
    status = "NOT REBUILT" if not written else "PARTIAL" if skipped else "REBUILT"
    if written:
        happened = f"Read {read} ledger(s) and recorded {len(found)} binding(s) in {_recipients_path()}."
        if dropped:
            happened += (f" {dropped} place(s) fell outside the bounds ({_RECIPIENT_PLACES} per recipient, "
                         f"{_RECIPIENTS_KEPT} recipients).")
        if skipped:
            happened += f" {len(skipped)} could not be read; their bindings were not added."
        if gone:
            happened += f" {len(gone)} enrolled checkout(s) no longer exist."
    else:
        happened = f"Read {read} ledger(s), but {_recipients_path()} could not be written; it is unchanged."
    return {"schema": 1, "status": status, "exit_code": 0 if status == "REBUILT" else 1, "ledgers": read,
            "places": len(found), "dropped": dropped, "skipped": skipped, "gone": gone, "happened": happened}


def wake_index_main(argv=None, *, ledger=launcher_ledger):
    parser = argparse.ArgumentParser(prog="multithread wake-index", description=(
        "The account's index of where each wake recipient holds a role, which lets a signal recorded in one "
        "checkout wake its recipient in another. `bind` keeps it current; `rebuild` reads every enrolled "
        "checkout's bindings once and records them, writing nothing else."))
    parser.add_argument("action", choices=("rebuild",))
    parser.add_argument("--json", action="store_true", help="one JSON object with the same outcome")
    args = parser.parse_args(argv)
    result = rebuild_index(ledger)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(_printable(f"{result['status']}: {result['happened']}"))
        for item in result["skipped"]:
            print(_printable(f"Not read: {item['checkout'] or 'an enrollment'}: {item['problem']}"))
    return result["exit_code"]


def _wake_where_bound(args, ledger, repo, here, seq, recorded, target_agent, target_session, agent, session):
    """Wake the recipient in a checkout where it holds a role, pointing back at the signal here; None when no
    place in the index verifies against that ledger's own binding."""
    provider = "codex" if target_agent == "codex" else "claude"
    for place in _places().get(_recipient(provider, target_agent, target_session, target_session), []):
        checkout = place["checkout"]
        if checkout == here:
            continue
        code, shown, _ = ledger(Path(checkout), "wake-ledger", "show")
        if code != 0 or not isinstance(shown, dict) or not isinstance(shown.get("bindings"), list):
            continue
        bound = sorted((binding for binding in shown["bindings"] if isinstance(binding, dict)
                        and binding.get("state") == "active" and _receives(binding, target_agent, target_session)),
                       key=lambda binding: binding["role"])
        if not bound:
            continue
        binding = bound[0]
        wake_args = argparse.Namespace(repo=checkout, steer=args.steer, agent=agent, session=session,
                                       role=binding["role"], ref=f"ledger:{here}#{seq}", message_id=None,
                                       dry_run=False, sender_repo=str(repo), sender_role=None, codex=args.codex,
                                       **_expectation(binding))
        result = wake(wake_args, ledger)
        result["happened"] = (f"Recorded signal {seq} here; its recipient holds {binding['role']} in {checkout}, "
                              "where the wake was tried. " + result["happened"])
        result["signal"], result["woken_in"] = recorded, checkout
        return result
    return None


def _expectation(binding):
    if binding["provider"] == "codex":
        return {"expect_generation": binding["generation"], "expect_provider": "codex",
                "expect_thread": binding["thread"], "expect_bound_agent": None, "expect_bound_session": None}
    return {"expect_generation": binding["generation"], "expect_provider": "claude", "expect_thread": None,
            "expect_bound_agent": binding["bound_agent"], "expect_bound_session": binding["bound_session"]}


def signal_wake(args, rest, ledger=launcher_ledger):
    """Record one signal, then wake its exact recipient through that recipient's own binding."""
    try:
        agent, session = _actor(args)
        if args.kind not in _DELIVERABLE:
            raise ValidationError(f"--wake needs a kind that reaches an inbox ({', '.join(_DELIVERABLE)}); "
                                  f"{args.kind} does not")
        if args.target is None or args.target_session is None:
            raise ValidationError("--wake needs --target and --target-session: only an exact recipient's own "
                                  "binding is woken")
        target_agent, target_session = canonical_agent(args.target), canonical_agent(args.target_session)
    except ValidationError as exc:
        return _outcome("NOT SENT", f"Nothing was recorded or sent: {exc}.", "Correct that and run this again.")
    repo = Path(args.repo or os.getcwd()).absolute()
    code, reply, problem = ledger(repo, "signal", args.kind, *rest, "--agent", agent, "--session", session,
                                  "--target", target_agent, "--target-session", target_session)
    if code is None:
        return _outcome("UNCERTAIN", f"The ledger at {repo} didn't answer ({problem}); the signal may or may not be "
                        "recorded. Nothing was woken.", "Read the ledger's newest events before sending again; an "
                        "identical signal is recognized as a duplicate.")
    if code != 0:
        return _outcome("NOT SENT", f"The signal wasn't recorded in the ledger at {repo}: {problem}. Nothing was "
                        "sent.", _ledger_next(code, repo, "run this again"))
    event = reply["event"]
    seq = event["seq"]
    recorded = {"seq": seq, "id": event["id"], "duplicate": bool(reply.get("duplicate")), "target": event["target"]}
    later = (f"If the recipient works in this checkout it sees signal {seq} at its next prompt; to wake it now, "
             f"run multithread wake <role> --ref {seq} for a role bound to that session.")
    code, shown, problem = ledger(repo, "wake-ledger", "show")
    if code != 0:
        return _outcome("NOT SENT", f"Recorded signal {seq}, but this ledger's bindings couldn't be read "
                        f"({problem}), so no wake was sent.", later, signal=recorded)
    bindings = sorted((binding for binding in shown.get("bindings", []) if isinstance(binding, dict)
                       and _receives(binding, target_agent, target_session)), key=lambda binding: binding["role"])
    active = [binding for binding in bindings if binding["state"] == "active"]
    if bindings and not active:
        paused = ", ".join(binding["role"] for binding in bindings)
        return _outcome("NOT SENT", f"Recorded signal {seq} for {target_agent} {target_session}, but that "
                        f"session's binding ({paused}) is paused, so no wake was sent.", later, signal=recorded)
    if not active:
        # Its briefs read the ledger of the checkout it works in, which may not be this one: wake it there.
        here = shown.get("ledger") if isinstance(shown.get("ledger"), str) else str(repo)
        crossed = _wake_where_bound(args, ledger, repo, here, seq, recorded, target_agent, target_session,
                                    agent, session)
        if crossed is not None:
            return crossed
        return _outcome("NOT BOUND", f"Recorded signal {seq} for {target_agent} {target_session}, but no role in "
                        "this ledger is bound to that session, so it wasn't woken, and it sees the signal only if "
                        "it reads this ledger.", "If it works in another checkout, wake its role there: multithread "
                        f"wake <role> --repo <its checkout> --ref {shlex.quote(f'ledger:{here}#{seq}')}.",
                        signal=recorded)
    binding = active[0]
    wake_args = argparse.Namespace(repo=str(repo), steer=args.steer, agent=agent, session=session,
                                   role=binding["role"], ref=str(seq), message_id=None, dry_run=False,
                                   sender_repo=None, sender_role=None, codex=args.codex, **_expectation(binding))
    result = wake(wake_args, ledger)
    result["happened"] = f"Recorded signal {seq}. " + result["happened"]
    result["signal"] = recorded
    if len(active) > 1:
        result["also_bound"] = [other["role"] for other in active[1:]]
    return result


def signal_wake_main(argv=None, *, ledger=launcher_ledger):
    parser = argparse.ArgumentParser(prog="multithread signal --wake", allow_abbrev=False, description=(
        "Record one signal, then wake its exact recipient when that session holds a role binding in the same "
        "ledger. The wake's actual outcome is reported, never assumed; an unbound recipient is reported as not "
        "woken. Every other option is the signal's own. Exit 0 woken, 3 already sent, 4 not woken (the signal "
        "may still be recorded: see signal.seq), 5 uncertain."))
    parser.add_argument("kind")
    parser.add_argument("--wake", action="store_true", required=True)
    parser.add_argument("--steer", action="store_true",
                        help="fold into a Codex recipient's running turn; Claude Code has no separate steer")
    parser.add_argument("--agent")
    parser.add_argument("--session")
    parser.add_argument("--target")
    parser.add_argument("--target-session")
    parser.add_argument("--codex", help="absolute Codex executable for codex queue; default: PATH")
    parser.add_argument("--repo", help="checkout whose ledger records the signal and holds the binding")
    parser.add_argument("--json", action="store_true", help="one JSON object with the same outcome")
    args, rest = parser.parse_known_args(argv)
    return _emit(signal_wake(args, rest, ledger), args.json)


# --- bind ------------------------------------------------------------------------

def _coverage(ledger, thread, cwd):
    """Whether the conversation's own hooks reach a ledger; a warning, never a refusal."""
    code, reply, problem = ledger(Path(cwd), "wake-ledger", "observed", thread)
    if code != 0:
        return {"state": "unknown", "message": (
            f"Couldn't confirm that conversation {thread} reaches a ledger: the checkout at {cwd} answered "
            f"\"{problem}\". Until it does, it gets no Multithread brief, so every wake to it must carry its own "
            f"pointer: pass a task-file path as --ref, not a bare sequence. Check with: "
            + _launcher("setup", "--repo", cwd, "--check"))}
    if reply["observed"]:
        return {"state": "reaching", "ledger": reply["ledger"]}
    cause = fix = None
    try:
        evidence = hooks.coverage_evidence(Path(reply["ledger"]))
        entry = dict(evidence["providers"].get("codex", {}), sessions=[{"session": thread, "last_activity": 0}])
        summary = hooks.coverage_report({**evidence, "providers": {"codex": entry}},
                                        {"codex": set()})["providers"]["codex"]
        cause, fix = summary.get("cause"), summary.get("fix")
    except Exception:  # The cause is an explanation; the missing events are the finding.
        pass
    fix = fix or _launcher("setup", "--repo", reply["ledger"], "--check")
    return {"state": "not_reaching", "ledger": reply["ledger"], "cause": cause, "fix": fix, "message": (
        f"Conversation {thread} has left no events in the ledger at {reply['ledger']}"
        + (f": {cause}" if cause else "") + ". It gets no Multithread brief, so every wake to it must carry its "
        f"own pointer: pass a task-file path as --ref, not a bare sequence. Fix: {fix}.")}


class LedgerRefused(Exception):
    """A ledger step refused; its message says why and next_step what helps."""

    def __init__(self, message, next_step):
        super().__init__(message)
        self.next_step = next_step


def _show(ledger, repo, role):
    code, reply, problem = ledger(repo, "wake-ledger", "show", *([role] if role else []))
    if code != 0:
        raise LedgerRefused(f"the ledger at {repo} couldn't show its bindings: {problem}",
                            _ledger_next(code, repo, "run bind again"))
    return reply


def _target(binding):
    if binding["provider"] == "codex":
        return f"Codex conversation {binding['thread']}"
    return f"the Claude Code inbox {binding['endpoint'].removeprefix('unix://')}"


def _describe(binding):
    lines = [f"{binding['role']}: {binding['state']}, bound to {_target(binding)} "
             f"(binding {binding['generation']}, by {binding['bound_by']} at {binding['bound_at']})"]
    if binding["provider"] == "codex":
        lines.append(f"  cwd when bound: {binding['cwd']}; daemon: {binding['endpoint']}")
    if binding.get("role_scope") is not None:
        lines.append(f"  scope: {binding['role_scope']}")
    if binding.get("charter") is not None:
        lines.append(f"  charter: {binding['charter']}")
    last = binding.get("last_attempt")
    if last:
        lines.append(f"  last wake: {last['message_id']} {last['outcome']} at {last['at']} "
                     f"(ledger seq {last['seq']})")
    return lines


def _verify_conversation(path, thread, base):
    """Ask the daemon about the conversation: its (cwd, status), or a NOT BOUND outcome."""
    try:
        daemon = Daemon(path)
    except DaemonUnavailable as exc:
        return _outcome("NOT BOUND", f"Couldn't reach the Codex daemon at {path} ({exc}). Nothing was recorded.",
                        "Check `codex app-server daemon version`, then run bind again.", **base)
    try:
        answer = daemon.call("thread/read", {"threadId": thread, "includeTurns": False})
        found = answer.get("thread") if isinstance(answer, dict) else None
    except Refused as exc:
        return _outcome("NOT BOUND", f"The daemon at {path} doesn't know conversation {thread} ({exc}). "
                        "Nothing was recorded.", "Check the id: it is the conversation's exact UUID, not its "
                        "title. Then run bind again.", **base)
    except Unusable as exc:
        return _outcome("NOT BOUND", f"The Codex daemon at {path} answered unreadably ({exc}). Nothing was "
                        "recorded.", "Check `codex app-server daemon version`, then run bind again.", **base)
    except NoAnswer as exc:
        return _outcome("NOT BOUND", f"The Codex daemon at {path} stopped answering ({exc}). Nothing was "
                        "recorded.", "Check `codex app-server daemon version`, then run bind again.", **base)
    finally:
        daemon.close()
    cwd = found.get("cwd") if isinstance(found, dict) else None
    status = found.get("status") if isinstance(found, dict) else None
    status = status.get("type") if isinstance(status, dict) else None
    if (not isinstance(found, dict) or found.get("id") != thread or not isinstance(cwd, str)
            or not os.path.isabs(cwd) or not cwd.isprintable() or not isinstance(status, str)):
        return _outcome("NOT BOUND", f"The daemon's answer for conversation {thread} didn't identify it with a "
                        "working directory. Nothing was recorded.", "Check `codex app-server daemon version`, "
                        "then run bind again.", **base)
    return cwd, status


def bind(args, ledger=launcher_ledger):
    repo = Path(args.repo or os.getcwd()).absolute()
    inbox = args.claude_socket
    try:
        role = canonical_wake_role(args.role)
        if inbox is None:
            thread = canonical_wake_thread(args.thread)
        elif not os.path.isabs(inbox) or not inbox.isprintable():
            raise ValidationError("--claude-socket must be an absolute path: pass \"$CLAUDE_CODE_MESSAGING_SOCKET\" "
                                  "from the Claude Code session to wake")
        else:
            thread = None
        agent, session = _actor(args)
        shown = _show(ledger, repo, role)
    except (ValidationError, LedgerRefused) as exc:
        return _outcome("NOT BOUND", f"Nothing was recorded: {exc}.",
                        getattr(exc, "next_step", "Correct that and run this again."))
    current = shown["bindings"][0]
    provider = "codex" if inbox is None else "claude"
    path = str(daemon_socket()) if inbox is None else inbox
    endpoint = "unix://" + path
    wanted = {"provider": provider, "thread": thread, "endpoint": endpoint}
    target = _target(wanted)
    base = {"role": role, **wanted, "ledger": shown["ledger"]}
    if current["state"] != "unbound":
        same_holder = (current.get("bound_agent"), current.get("bound_session")) == (agent, session) or (
            current["provider"] == "codex" and agent == "codex" and session == current["thread"])
        same_recipient = current["provider"] == provider and (provider == "claude" or current["thread"] == thread)
        unchanged = current["provider"] == provider and current["thread"] == thread and current["endpoint"] == endpoint
        metadata_unchanged = (args.scope is None or args.scope == current.get("role_scope")) and (
            args.charter is None or args.charter == current.get("charter"))
        if unchanged and metadata_unchanged and args.expected_generation is None:
            indexed = _remember(_recipient(provider, current.get("bound_agent"), current.get("bound_session"),
                                           thread), shown["ledger"], role)
            return _outcome("ALREADY BOUND", f"{role} already wakes {target} (binding {current['generation']}, "
                            f"{current['state']}); nothing was recorded.", "Nothing.",
                            generation=current["generation"], index_recorded=indexed, **base)
        refresh = same_holder and same_recipient
        if not refresh and (not args.replace or args.expected_generation is None or not args.reason or not args.approval_ref):
            return _outcome("NOT BOUND", f"{role} is already bound to {_target(current)} (binding "
                            f"{current['generation']}, since {current['bound_at']}). Nothing was recorded.",
                            f"Have its exact holder {current['bound_by']} refresh its existing recipient, or make "
                            "an explicitly authorized handover with --replace, "
                            f"--expected-generation {current['generation']}, --reason <reason> and "
                            "--approval-ref <immutable approval reference>.", **base)
        if refresh and args.expected_generation is None:
            args.expected_generation = current["generation"]
        if refresh:
            args.replace = True
    if inbox is not None:
        problem = inbox_problem(inbox)
        if problem is not None:
            return _outcome("NOT BOUND", f"{inbox} isn't a usable Claude Code inbox: {problem[1]}. Nothing was "
                            "recorded.", f"Run bind from the Claude Code session to wake, passing its own inbox: "
                            f"multithread bind {role} --claude-socket \"$CLAUDE_CODE_MESSAGING_SOCKET\"", **base)
        record, coverage, extra = ["--provider", "claude"], None, {}
    else:
        verified = _verify_conversation(path, thread, base)
        if isinstance(verified, dict):
            return verified
        cwd, status = verified
        base.update(cwd=cwd, thread_status=status)
        coverage = _coverage(ledger, thread, cwd)
        record = ["--thread", thread, "--cwd", cwd]
        extra = {"warning": coverage["message"]} if "message" in coverage else {}
    options = ["--replace"] if args.replace else []
    for flag, value in (("--scope", args.scope), ("--charter", args.charter), ("--reason", args.reason),
                        ("--expected-generation", args.expected_generation), ("--approval-ref", args.approval_ref)):
        if value is not None:
            options.extend([flag, str(value)])
    code, recorded, problem = ledger(repo, "wake-ledger", "bind", role, "--agent", agent, "--session", session,
                                     "--endpoint", endpoint, *record, *options)
    if code != 0:
        return _outcome("NOT BOUND", f"The ledger at {repo} didn't record the binding: {problem}.",
                        _ledger_next(code, repo, "run bind again"), coverage=coverage, **base)
    binding = recorded["binding"]
    base.update(generation=binding["generation"], binding_state=binding["state"], coverage=coverage)
    # Re-running bind records an older binding here too, so the index fills without a migration.
    base["index_recorded"] = _remember(_recipient(provider, agent, session, thread), shown["ledger"], role)
    if inbox is not None:
        remember_inbox(session, inbox)  # run from inside the session, as the inbox check above requires
    if recorded["duplicate"]:
        return _outcome("ALREADY BOUND", f"{role} already wakes {target} (binding {binding['generation']}); "
                        "nothing was recorded.", "Nothing.", **base, **extra)
    replaced = recorded.get("replaced")
    replaces = (f" It replaces binding {replaced['generation']} ({_target(replaced)}); message ids start over "
                "under this binding, so a file already sent under the old one can be sent again."
                if replaced else "")
    paused = binding["state"] == "paused"
    if paused:
        replaces += " Wakes remain paused."
    next_wake = (f"The role remains paused. Its holder can deliberately resume with `multithread resume {role}` "
                 "using its exact identity, then send a wake." if paused else None)
    if inbox is not None:
        return _outcome("BOUND", f"{role} now wakes the Claude Code session whose inbox is {inbox}, as binding "
                        f"{binding['generation']} in the ledger at {shown['ledger']}." + replaces,
                        next_wake or (f"Send a wake with: multithread wake {role} --ref <task file>. If that session "
                                      "ends or restarts, bind again from the new one."), **base)
    return _outcome("BOUND", f"{role} now wakes Codex conversation {thread} ({status}; cwd {base['cwd']}) as "
                    f"binding {binding['generation']} in the ledger at {shown['ledger']}." + replaces,
                    next_wake or f"Send a wake with: multithread wake {role} --ref <task file>", **base, **extra)


def bind_main(argv=None, *, ledger=launcher_ledger):
    parser = argparse.ArgumentParser(prog="multithread bind", description=(
        "Bind a role in this checkout's ledger to one existing Codex conversation, after the shared Codex "
        "daemon confirms it exists, or to a Claude Code session's inbox, from inside that session. Without a "
        "target, show the current bindings. `multithread unbind`, `pause` and `resume` change a binding; "
        "`multithread wake` uses it."))
    parser.add_argument("role", nargs="?", help="a lowercase role name, for example operator")
    parser.add_argument("--thread", help="a Codex conversation's exact UUID")
    parser.add_argument("--claude-socket", help="a Claude Code session's inbox: its $CLAUDE_CODE_MESSAGING_SOCKET")
    parser.add_argument("--replace", action="store_true", help="request an authorized handover; also requires generation, reason and approval reference")
    parser.add_argument("--scope", help="the role's declared work scope")
    parser.add_argument("--charter", help="the role's declared responsibility")
    parser.add_argument("--reason", help="why this binding is refreshed or handed over")
    parser.add_argument("--expected-generation", type=int, help="the exact current binding generation; required for handover")
    parser.add_argument("--approval-ref", help="immutable handover approval: git:<full OID>, sha256:<digest>, or receipt:<id>")
    parser.add_argument("--agent", help="who records the binding; default RELAY_AGENT")
    parser.add_argument("--session", help="that agent's session; default RELAY_SESSION")
    parser.add_argument("--repo", help="checkout whose ledger holds the binding; default: current directory")
    parser.add_argument("--json", action="store_true", help="one JSON object with the same outcome")
    args = parser.parse_args(argv)
    if args.thread is not None and args.claude_socket is not None:
        parser.error("bind a role to one target: --thread for Codex or --claude-socket for Claude Code")
    targeted = args.thread is not None or args.claude_socket is not None
    if targeted and args.role is None:
        parser.error("a target needs a role")
    if args.replace and not targeted:
        parser.error("--replace needs --thread or --claude-socket")
    if not targeted and any(value is not None for value in (
            args.scope, args.charter, args.reason, args.expected_generation, args.approval_ref)):
        parser.error("binding metadata and handover options need --thread or --claude-socket")
    if args.expected_generation is not None and args.expected_generation < 1:
        parser.error("--expected-generation must be positive")
    if targeted:
        return _emit(bind(args, ledger), args.json)
    repo = Path(args.repo or os.getcwd()).absolute()
    try:
        shown = _show(ledger, repo, canonical_wake_role(args.role) if args.role is not None else None)
    except (ValidationError, LedgerRefused) as exc:
        print(_printable(f"multithread bind: {exc}" + (f". Next: {exc.next_step}" if isinstance(exc, LedgerRefused)
                                                        else "")), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"schema": 1, **shown}, ensure_ascii=False, sort_keys=True))
        return 0
    bound = [item for item in shown["bindings"] if item["state"] != "unbound"]
    for item in bound:
        print("\n".join(_printable(line) for line in _describe(item)))
    if not bound:
        print(_printable(f"{'No roles are' if args.role is None else args.role + ' is not'} bound in the ledger "
                         f"at {shown['ledger']}."))
        print(f"Next: multithread bind {args.role or '<role>'} --thread <codex conversation id>")
    return 0
