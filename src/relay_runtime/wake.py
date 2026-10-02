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
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys

from relay_core.protocol import (ValidationError, canonical_agent, canonical_wake_expectation,
                                 canonical_wake_message_id, canonical_wake_ref,
                                 canonical_wake_role, canonical_wake_thread)
from . import account_launcher, hooks
from .enrollment import NotEnrolled

SOCKET = Path("app-server-control") / "app-server-control.sock"
EXIT_CODES = {"STEERED": 0, "QUEUED": 0, "DELIVERED TO INBOX": 0, "DRY RUN": 0, "STATUS": 0,
              "BOUND": 0, "ALREADY BOUND": 0,
              "ALREADY SENT": 3, "NOT SENT": 4, "NOT BOUND": 4, "UNCERTAIN": 5}
_CLIENT_INFO = {"name": "multithread-wake", "title": "Multithread wake", "version": "1"}
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_MESSAGE = 1024 * 1024
_DAEMON_TIMEOUT = 10
_STEER_TIMEOUT = 20
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


class Daemon:
    """One connection to the shared daemon, initialized; requests run one at a time."""

    def __init__(self, path, timeout=_DAEMON_TIMEOUT):
        self.sock = None
        self.buffer = b""
        self.next_id = 0
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(str(path))
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            self.sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                               "Connection: Upgrade\r\nSec-WebSocket-Key: " + key
                               + "\r\nSec-WebSocket-Version: 13\r\n\r\n").encode("ascii"))
            while b"\r\n\r\n" not in self.buffer:
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
            if not isinstance(self.call("initialize", {"clientInfo": _CLIENT_INFO}, timeout=timeout), dict):
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
        try:
            self.sock.settimeout(timeout)
            self._send(json.dumps({"id": pending, "method": method, "params": params}).encode("utf-8"))
            while True:
                message = self._message()
                if "method" in message or message.get("id") != pending:
                    continue  # Notifications and other traffic are not this answer.
                break
        except (OSError, ValueError) as exc:
            raise NoAnswer(_detail(exc)) from None
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

    def _send(self, data, opcode=1):
        mask = os.urandom(4)
        size = len(data)
        header = bytes([0x80 | opcode]) + (
            bytes([0x80 | size]) if size < 126 else bytes([0x80 | 126]) + struct.pack(">H", size)
            if size < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", size))
        self.sock.sendall(header + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(data)))

    def _take(self, size):
        while len(self.buffer) < size:
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
                value = json.loads(body)
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


def deliver(path, text, timeout=_INBOX_TIMEOUT):
    """Write the one user line Claude Code reads; the line is ready before connecting."""
    line = (json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=True)
            + "\n").encode("ascii")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        try:
            connection.connect(str(path))
        except OSError as exc:
            raise InboxRefused(_detail(exc)) from None
        try:
            connection.sendall(line)
        except OSError as exc:
            raise NoAnswer(_detail(exc)) from None
    finally:
        connection.close()


# --- The ledger: one admitted step per installed launcher run ----------------

def launcher_ledger(repo, *arguments):
    """Run one ledger step: (exit code or None when it didn't answer, result, message)."""
    command = [str(account_launcher()), "--repo", str(repo), "--json", *arguments]
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   timeout=_LEDGER_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return None, None, f"it didn't answer within {_LEDGER_TIMEOUT} s"
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


# --- wake ----------------------------------------------------------------------

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


def message_text(agent, ref, ledger):
    """The whole wake: who sends it and where the work is, never the work itself."""
    where = f"ledger sequence {ref} in {ledger}" if ref.isdigit() else ref
    return f"Multithread wake from {agent}: {where}"


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
    repo = Path(args.repo or os.getcwd()).absolute()
    requested = "steer" if args.steer else "queue"
    try:
        agent, session = _actor(args, session_required=not args.dry_run)
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
    if not ref.isdigit():
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

    step = ["plan", role] if args.dry_run else ["begin", role, "--agent", agent, "--session", session]
    expectation = [argument for field, value in (expected_binding or {}).items()
                   for argument in ("--expect-" + field.replace("_", "-"), str(value))]
    code, decision, problem = ledger(repo, "wake-ledger", *step, "--ref", ref, "--requested", requested, *content,
                                     *(["--id", args.message_id] if args.message_id else []), *expectation)
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

    text = message_text(agent, ref, decision["ledger"])
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
        return _wake_inbox(args, role, path, text, message_id, base, conclude)
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
        return conclude(_outcome("QUEUED", why + " Codex accepted the queue entry; a recipient turn and "
                                 "consumption are unobserved.", _WAIT, **base), "queued", reason, "queue", receipt.group(1))
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


def _wake_inbox(args, role, path, text, message_id, base, conclude):
    """Claude Code: one line into the session's inbox. There is no separate steer."""
    unsteered = " Claude Code has no separate steer, so --steer changed nothing." if args.steer else ""
    rebind = (f"Have the exact holder refresh {role} from its own Claude Code session with "
              "--claude-socket \"$CLAUDE_CODE_MESSAGING_SOCKET\". Moving it to another owner requires an explicitly "
              "authorized handover with --replace, --expected-generation, --reason and --approval-ref. "
              "Then run this wake again.")
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
        deliver(path, text)
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
        "Send `Multithread wake from <agent>: <ref>` to what a role is bound to. A Codex conversation gets it "
        "through the shared Codex daemon, queued by default; --steer submits it to the observed running turn "
        "instead, and queues when none is running. A Claude Code session gets it in its inbox. Acceptance "
        "does not establish a new turn or consumption. Each attempt is recorded in this ledger; --status "
        "reads its original recipient and explicit acknowledgement. Exit 0 sent, status or dry run, 3 already "
        "sent, 4 not sent, 5 uncertain."))
    parser.add_argument("role", help="the bound role, for example operator")
    parser.add_argument("--ref", help="absolute task-file path, or a ledger sequence number; optional for --status")
    parser.add_argument("--status", action="store_true", help="read recorded attempts and acknowledgements; never read the task file or send")
    parser.add_argument("--steer", action="store_true",
                        help="for news about a Codex recipient's current work: fold into the running turn")
    parser.add_argument("--id", dest="message_id",
                        help="message id; default: derived from this ledger, the role, its binding and the ref")
    parser.add_argument("--dry-run", action="store_true", help="decide and report; send and record nothing")
    parser.add_argument("--expect-generation", type=int, help="require this binding generation before recording or sending")
    parser.add_argument("--expect-provider", choices=("codex", "claude"), help="expected recipient provider; requires generation and recipient")
    parser.add_argument("--expect-thread", help="expected Codex conversation; requires generation and provider")
    parser.add_argument("--expect-bound-agent", help="expected Claude binding's recorded owner agent; requires generation, provider and owner session")
    parser.add_argument("--expect-bound-session", help="expected Claude binding's recorded owner session")
    parser.add_argument("--agent", help="sender named in the wake; default RELAY_AGENT")
    parser.add_argument("--session", help="sender's session for the ledger record; default RELAY_SESSION")
    parser.add_argument("--codex", help="absolute Codex executable for codex queue; default: PATH")
    parser.add_argument("--repo", help="checkout whose ledger holds the binding; default: current directory")
    parser.add_argument("--json", action="store_true", help="one JSON object with the same outcome")
    args = parser.parse_args(argv)
    if not args.status and args.ref is None:
        parser.error("--ref is required unless --status is used")
    if args.status and (args.steer or args.dry_run or args.message_id is not None
                        or any(getattr(args, "expect_" + field) is not None for field in
                               ("generation", "provider", "thread", "bound_agent", "bound_session"))):
        parser.error("--status cannot be combined with --steer, --dry-run, --id or --expect-* flags")
    return _emit(wake_status(args, ledger) if args.status else wake(args, ledger), args.json)


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
            return _outcome("ALREADY BOUND", f"{role} already wakes {target} (binding {current['generation']}, "
                            f"{current['state']}); nothing was recorded.", "Nothing.",
                            generation=current["generation"], **base)
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
