"""Bind and wake against a fake Codex daemon and a fake codex executable.

The daemon speaks WebSocket over a Unix socket in a temporary directory, the
way Codex's app-server control socket does; `codex` is a script that records
its arguments. The ledger is a real disposable one. No real daemon, provider,
conversation or account configuration is touched.
"""

import base64
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_core import cli as core_cli
from relay_core.store import RelayStore
from relay_core.protocol import session_target
from relay_runtime import cli as runtime_cli, hooks, wake

THREAD = "a0000000-0000-7000-8000-000000000001"
OTHER = "b0000000-0000-7000-8000-000000000002"
LIVE_TURN = "c0000000-0000-7000-8000-000000000003"
QUEUED_ID = "d0000000-0000-7000-8000-00000000000a"
LAUNCHER = Path("/opt/fixture/bin/multithread")
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
FAKE_CODEX = """#!/usr/bin/python3 -I
import json, os, signal, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls.jsonl"), "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
mode = open(os.path.join(here, "mode")).read().strip()
REFUSALS = {  # Codex prints these when it stops before anything can be queued.
    "refuse": "Error: failed to connect to remote app server: synthetic refusal",
    "no_home": "Error: failed to find Codex home: synthetic missing home",
    "unsupported": "Error: the remote app server does not support thread/queue/add; update or restart the remote "
                   "app server: failed to queue session message: synthetic method not found",
}
if mode in REFUSALS:
    print(REFUSALS[mode], file=sys.stderr)
    raise SystemExit(1)
if mode == "hang":
    time.sleep(30)
# Every mode below has already handed the message to the daemon.
if mode == "invalid_output":
    sys.stdout.buffer.write(b"Queued message \\xff\\xfe\\n")
    raise SystemExit(0)
if mode == "no_receipt":
    raise SystemExit(0)
if mode == "other_thread":
    print("Queued message %s for thread %s." % (QUEUED, ELSEWHERE))
    raise SystemExit(0)
if mode == "lost_connection":
    print("Error: failed to queue session message: connection closed before response", file=sys.stderr)
    raise SystemExit(1)
if mode == "post_request_error":
    print("Error: failed to queue session message: server error -32603: synthetic failure after acceptance; "
          "diagnostic mentions failed to connect to remote app server", file=sys.stderr)
    raise SystemExit(1)
if mode == "garbled_refusal":
    sys.stderr.buffer.write(b"Error: failed to connect to remote app server: synthetic \\xff\\xfe\\n")
    raise SystemExit(1)
if mode == "receipt_and_refusal":
    print("Queued message %s for thread %s." % (QUEUED, sys.argv[sys.argv.index("--thread") + 1]))
    print(REFUSALS["refuse"], file=sys.stderr)
    raise SystemExit(1)
if mode == "logged_refusal":
    print("synthetic log line", file=sys.stderr)
    print(REFUSALS["refuse"], file=sys.stderr)
    raise SystemExit(1)
if mode == "refusal_exit_2":
    print(REFUSALS["refuse"], file=sys.stderr)
    raise SystemExit(2)
if mode == "garbled_error":
    sys.stderr.buffer.write(b"Error: \\xff\\xfe connection reset\\n")
    raise SystemExit(1)
if mode == "killed":
    os.kill(os.getpid(), signal.SIGKILL)
print("Queued message %s for thread %s." % (QUEUED, sys.argv[sys.argv.index("--thread") + 1]))
""".replace("QUEUED", repr(QUEUED_ID)).replace("ELSEWHERE", repr(OTHER))


def _error(code, message):
    return lambda turn: {"error": {"code": code, "message": message.format(turn=turn)}}


# Codex 0.159.2's own refusals before acceptance (turn_steer_inner), then replies that prove nothing.
STEER_REPLIES = {
    "accept": lambda turn: {"result": {"turnId": turn}},
    "no_active_turn": _error(-32600, "no active turn to steer"),
    "turn_mismatch": _error(-32600, "expected active turn id `{turn}` but found `e0000000-0000-7000-8000-0000000000ff`"),
    "review_turn": _error(-32600, "cannot steer a review turn"),
    "null_result": lambda turn: {"result": None},
    "empty_receipt": lambda turn: {"result": {}},
    "other_turn": lambda turn: {"result": {"turnId": "e0000000-0000-7000-8000-0000000000ff"}},
    "neither": lambda turn: {},
    "both": lambda turn: {"result": {"turnId": turn}, "error": {"code": -32600, "message": "no active turn to steer"}},
    "codeless_error": lambda turn: {"error": {"message": "no active turn to steer"}},
    "internal_error": _error(-32603, "failed to steer turn: synthetic internal fault"),
    "draining": _error(-32600, "Server is draining; retry after reconnecting"),
    "wrong_code": _error(-32603, "no active turn to steer"),
    "extended_refusal": _error(-32600, "no active turn to steer; and the input was stored"),
}


class FakeDaemon:
    """A WebSocket JSON-RPC server with the four methods wake and bind use."""

    def __init__(self, path):
        self.path = path
        self.threads = {}  # id -> {"cwd", "status", "turns"}
        self.steer = "accept"  # a key of STEER_REPLIES, or hang | close
        self.requests = []
        self.connections = 0
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(8)
        self.server.settimeout(0.05)
        self.stopped = threading.Event()
        self.worker = threading.Thread(target=self.serve, daemon=True)
        self.worker.start()

    def close(self):
        self.stopped.set()
        self.worker.join(5)
        self.server.close()

    def serve(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            threading.Thread(target=self.handle, args=(connection,), daemon=True).start()

    def handle(self, connection):
        with connection:
            connection.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                data += chunk
            head, self.pending = data.split(b"\r\n\r\n", 1)
            key = next(line.split(b":", 1)[1].strip() for line in head.split(b"\r\n")
                       if line.lower().startswith(b"sec-websocket-key:"))
            accept = base64.b64encode(hashlib.sha1(key + GUID).digest())
            connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                               b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            buffer = [self.pending]
            try:
                while True:
                    opcode, payload = self.read(connection, buffer)
                    if opcode == 8:
                        return
                    if opcode != 1:
                        continue
                    message = json.loads(payload)
                    self.requests.append(message)
                    if "id" not in message:
                        continue
                    reply = self.answer(message)
                    if reply == "close":
                        return
                    if reply is None:
                        continue  # Hang: never answer, keep reading until the client leaves.
                    # Traffic before the answer: a ping and a notification.
                    connection.sendall(bytes([0x89, 4]) + b"ping")
                    self.send(connection, {"method": "thread/status/changed", "params": {}})
                    self.send(connection, reply, fragmented=message["method"] == "thread/turns/list")
            except (OSError, ConnectionError):
                return

    def answer(self, message):
        method, params = message["method"], message.get("params", {})
        if method == "initialize":
            return {"id": message["id"], "result": {"userAgent": "fixture"}}
        thread = self.threads.get(params.get("threadId"))
        if method in ("thread/read", "thread/turns/list", "turn/steer") and thread is None:
            return {"id": message["id"], "error": {"code": -32600, "message": "thread not found"}}
        if method == "thread/read":
            return {"id": message["id"], "result": {"thread": {
                "id": thread.get("answers_as", params["threadId"]), "cwd": thread["cwd"],
                "status": {"type": thread["status"]}}}}
        if method == "thread/turns/list":
            if "list_reply" in thread:
                return {"id": message["id"], **thread["list_reply"]}
            return {"id": message["id"], "result": {"data": thread["turns"][:1], "nextCursor": None}}
        if method == "turn/steer":
            if self.steer in ("hang", "close"):
                return None if self.steer == "hang" else "close"
            return {"id": message["id"], **STEER_REPLIES[self.steer](params["expectedTurnId"])}
        return {"id": message["id"], "error": {"code": -32601, "message": "unknown method"}}

    @staticmethod
    def send(connection, value, fragmented=False):
        body = json.dumps(value).encode()
        parts = [body[:5], body[5:]] if fragmented else [body]
        for index, part in enumerate(parts):
            final = 0x80 if index == len(parts) - 1 else 0
            opcode = 1 if index == 0 else 0
            size = len(part)
            header = bytes([final | opcode]) + (bytes([size]) if size < 126 else bytes([126]) + struct.pack(">H", size))
            connection.sendall(header + part)

    @staticmethod
    def read(connection, buffer):
        def take(size):
            while len(buffer[0]) < size:
                chunk = connection.recv(65536)
                if not chunk:
                    raise ConnectionError("client left")
                buffer[0] += chunk
            taken, buffer[0] = buffer[0][:size], buffer[0][size:]
            return taken
        first, second = take(2)
        if not second & 0x80:
            raise AssertionError("a client frame must be masked")
        size = second & 0x7F
        if size == 126:
            size = struct.unpack(">H", take(2))[0]
        elif size == 127:
            size = struct.unpack(">Q", take(8))[0]
        mask = take(4)
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(take(size)))
        return first & 0x0F, payload

    def methods(self):
        return [message["method"] for message in self.requests if "id" in message]


class WakeCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-wake-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "controller"
        self.repo.mkdir()
        for command in (["init", "-q"], ["-c", "user.name=Fixture", "-c", "user.email=fixture@invalid",
                                         "commit", "-q", "--allow-empty", "-m", "seed"]):
            subprocess.run(["git", "-C", str(self.repo), *command], check=True, capture_output=True)
        self.home = self.base / "state"
        patcher = mock.patch("relay_core.store._expected_workspace_binding",
                             return_value=(self.repo / ".git").resolve())
        patcher.start()
        self.addCleanup(patcher.stop)
        # AF_UNIX paths are short; keep the socket's directory near the root when TMPDIR is deep.
        short = tempfile.mkdtemp(prefix="w", dir="/tmp" if len(tempfile.gettempdir()) > 40 else None)
        self.addCleanup(shutil.rmtree, short, True)
        self.codex_home = Path(short)
        (self.codex_home / "app-server-control").mkdir()
        self.socket = self.codex_home / "app-server-control" / "app-server-control.sock"
        self.claude_home = self.base / "claude-home"
        self.claude_home.mkdir()
        for patcher in (mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home),
                                                     "CLAUDE_CONFIG_DIR": str(self.claude_home)}),
                        mock.patch.object(wake, "account_launcher", return_value=LAUNCHER),
                        mock.patch.object(hooks, "account_launcher", return_value=LAUNCHER)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.daemon = FakeDaemon(self.socket)
        self.addCleanup(self.daemon.close)
        self.daemon.threads[THREAD] = {"cwd": str(self.repo), "status": "idle",
                                       "turns": [{"id": LIVE_TURN, "status": "completed", "items": []}]}
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.codex = self.bin / "codex"
        self.codex.write_text(FAKE_CODEX)
        self.codex.chmod(0o755)
        self.codex_mode("accept")
        self.task = self.base / "codex-session-31.md"
        self.task.write_text("synthetic task\n")
        self.ledger_calls = []
        self.failing = set()

    # --- Fixtures ---------------------------------------------------------------

    def codex_mode(self, mode):
        (self.bin / "mode").write_text(mode)

    def codex_calls(self):
        log = self.bin / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def ledger(self, repo, *arguments):
        self.ledger_calls.append((str(repo), arguments))
        if arguments[:2] and arguments[1] in self.failing:
            return 74, None, "synthetic ledger failure"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = core_cli.main(["--repo", str(repo), "--home", str(self.home), "--json", *arguments])
        if code != 0:
            return code, None, err.getvalue().strip().splitlines()[-1].removeprefix("multithread: ")
        return 0, json.loads(out.getvalue()), ""

    def control(self, action, role="operator"):
        out = io.StringIO()
        with redirect_stdout(out):
            code = core_cli.main(["--repo", str(self.repo), "--home", str(self.home), action, role,
                                  "--agent", "claude", "--session", "binder"])
        return code, out.getvalue()

    def store(self):
        return RelayStore.open(repo=self.repo, state_home=self.home)

    def events(self, kind=None):
        with self.store() as store:
            rows = store.events(limit=500)
        return [row for row in rows if kind is None or row["kind"] == kind]

    def run_helper(self, main, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--repo", str(self.repo), *argv], ledger=self.ledger)
        return code, out.getvalue()

    def bind(self, *extra, thread=THREAD):
        return self.run_helper(wake.bind_main, "operator", "--thread", thread, "--agent", "claude",
                               "--session", "binder", *extra)

    def wake(self, *extra, ref=None):
        code, out = self.run_helper(wake.wake_main, "operator", "--ref", str(ref or self.task), "--agent",
                                    "claude", "--session", "sender", "--codex", str(self.codex), "--json", *extra)
        result = json.loads(out)
        self.assertEqual(code, result["exit_code"])
        return result

    def bound(self):
        code, out = self.bind()
        self.assertEqual(0, code, out)
        return self.events("wake.bound")[-1]["seq"]

    def handover(self, thread=OTHER, *extra):
        generation = self.events("wake.bound")[-1]["seq"]
        return self.bind("--replace", "--expected-generation", str(generation), "--reason", "approved fixture move",
                         "--approval-ref", "receipt:fixture-handover", *extra, thread=thread)

    def conclusion(self):
        return self.events("wake.concluded")[-1]["meta"]

    def live(self, status="inProgress"):
        self.daemon.threads[THREAD]["turns"] = [{"id": LIVE_TURN, "status": status, "items": []}]


class WakeOutcomeTests(WakeCase):
    def test_status_reads_original_attempt_after_ref_vanishes_without_native_contact(self):
        generation = self.bound()
        sent = self.wake()
        self.task.unlink()
        before = len(self.events())
        daemon_calls = list(self.daemon.methods())
        queue_calls = self.codex_calls()
        with mock.patch.object(wake, "fingerprint", side_effect=AssertionError("status must not open the ref")):
            code, out = self.run_helper(wake.wake_main, "operator", "--status", "--ref", str(self.task), "--json")
        result = json.loads(out)
        self.assertEqual((0, "STATUS"), (code, result["status"]))
        attempt = result["history"]["attempts"][0]
        self.assertEqual((generation, f"codex:{THREAD}", sent["message_id"], "queued", "unknown"),
                         tuple(attempt[key] for key in ("generation", "recipient", "message_id", "outcome", "consumption_state")))
        self.assertEqual({"reachability": "unknown", "turn_state": "unknown", "source": "unobserved"},
                         result["recipient_state"])
        self.assertEqual(before, len(self.events()))
        self.assertEqual(daemon_calls, self.daemon.methods())
        self.assertEqual(queue_calls, self.codex_calls())
        self.assertEqual(("wake-ledger", "history", "operator", "--ref", str(self.task)), self.ledger_calls[-1][1])

    def test_status_keeps_old_recipient_and_explicit_ack_after_handover(self):
        generation = self.bound()
        with self.store() as store:
            signal = store.emit({"kind": "work.handoff", "agent": "claude", "session": "sender",
                                 "target": session_target("codex", THREAD), "summary": "synthetic scoped handoff",
                                 "artifact": "receipt:fixture-handoff"})
        ref = str(signal["event"]["seq"])
        sent = self.wake(ref=ref)
        with self.store() as store:
            ack = store.acknowledge(int(ref), agent="codex", session=THREAD)
        self.daemon.threads[OTHER] = dict(self.daemon.threads[THREAD])
        self.assertEqual(0, self.handover()[0])
        before = len(self.events())
        code, out = self.run_helper(wake.wake_main, "operator", "--status", "--ref", ref, "--json")
        result = json.loads(out)
        self.assertEqual(0, code)
        attempt = result["history"]["attempts"][0]
        self.assertEqual((generation, f"codex:{THREAD}", sent["message_id"], "acknowledged", ack["event"]["seq"]),
                         tuple(attempt[key] for key in ("generation", "recipient", "message_id", "consumption_state", "acknowledgement_seq")))
        self.assertEqual(OTHER, result["history"]["binding"]["thread"])
        self.assertEqual("acknowledged", result["consumption_state"])
        self.assertEqual(before, len(self.events()))

    def test_status_without_ref_or_actor_reads_empty_history_and_prints_no_false_delivery(self):
        code, out = self.run_helper(wake.wake_main, "operator", "--status", "--json")
        result = json.loads(out)
        self.assertEqual((0, "STATUS", []), (code, result["status"], result["history"]["attempts"]))
        self.assertEqual(0, self.daemon.connections)
        self.assertEqual([], self.codex_calls())
        self.assertEqual([], self.events())
        code, out = self.run_helper(wake.wake_main, "operator", "--status")
        self.assertEqual(0, code)
        self.assertIn("STATUS: Read recorded wake attempts", out)
        self.assertIn("Nothing was sent", out)

    def test_status_refuses_send_options_and_send_still_requires_ref(self):
        for extra in (("--status", "--steer"), ("--status", "--dry-run"), ("--status", "--id", "another"), ()):
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                self.run_helper(wake.wake_main, "operator", *extra)
            self.assertEqual(2, raised.exception.code)
        self.assertEqual([], self.ledger_calls)
        self.assertEqual(0, self.daemon.connections)

    def test_queue_by_default_records_the_attempt_and_the_native_id(self):
        generation = self.bound()
        result = self.wake()
        self.assertEqual(("QUEUED", 0), (result["status"], result["exit_code"]))
        self.assertEqual("Queued as asked. Codex accepted the queue entry; a recipient turn and consumption are "
                         "unobserved.", result["happened"])
        self.assertEqual("unknown", result["consumption_state"])
        self.assertEqual({"reachability": "reachable", "turn_state": "completed", "source": "codex_turn_list"},
                         result["recipient_state"])
        self.assertEqual("Nothing. Wait for the recipient's acknowledgement.", result["next"])
        text = f"Multithread wake from claude: {self.task}"
        self.assertEqual([["queue", "--remote", f"unix://{self.socket}", "--thread", THREAD, "--message", text]],
                         self.codex_calls())
        self.assertNotIn("turn/steer", self.daemon.methods())
        attempt = self.events("wake.attempted")[-1]
        body = self.task.read_bytes()
        self.assertEqual({"role": "operator", "generation": generation, "provider": "codex", "thread": THREAD,
                          "message_id": result["message_id"], "ref": str(self.task),
                          "ref_sha256": hashlib.sha256(body).hexdigest(), "ref_size": len(body),
                          "requested": "queue"}, attempt["meta"])
        self.assertEqual(("claude", "sender", "operator"), (attempt["agent"], attempt["session"], attempt["target"]))
        self.assertEqual({"role": "operator", "generation": generation, "attempt_seq": attempt["seq"],
                          "message_id": result["message_id"], "outcome": "queued", "reason": "requested",
                          "transport": "queue", "native_id": QUEUED_ID}, self.conclusion())
        self.assertEqual(f"wake-concluded:{attempt['seq']}", self.events("wake.concluded")[-1]["id"])
        self.assertRegex(result["message_id"], rf"^wake-operator-{generation}-[0-9a-f]{{12}}$")

    def test_steer_folds_into_the_live_turn_with_the_message_id(self):
        self.bound()
        self.live()
        result = self.wake("--steer")
        self.assertEqual(("STEERED", 0), (result["status"], result["exit_code"]))
        self.assertEqual(f"Codex accepted the steer for running turn {LIVE_TURN}. Recipient consumption is "
                         "unobserved.", result["happened"])
        self.assertEqual("unknown", result["consumption_state"])
        self.assertEqual("inProgress", result["recipient_state"]["turn_state"])
        steer = [m for m in self.daemon.requests if m.get("method") == "turn/steer"]
        self.assertEqual([{"threadId": THREAD, "expectedTurnId": LIVE_TURN, "clientUserMessageId":
                           result["message_id"], "input": [{"type": "text", "text": result["text"]}]}],
                         [m["params"] for m in steer])
        self.assertEqual([], self.codex_calls())
        self.assertEqual(("steered", "live_turn", "steer", LIVE_TURN),
                         tuple(self.conclusion()[key] for key in ("outcome", "reason", "transport", "native_id")))

    def test_steer_queues_when_no_turn_is_running(self):
        self.bound()
        for turns in ([{"id": LIVE_TURN, "status": "completed", "items": []}], []):
            self.daemon.threads[THREAD]["turns"] = turns
            result = self.wake("--steer", "--id", f"no-live-{len(turns)}")
            self.assertEqual("QUEUED", result["status"])
            self.assertTrue(result["happened"].startswith("No running turn was observed, so it was queued instead of steered."))
            self.assertEqual(("queued", "no_live_turn", "queue"),
                             tuple(self.conclusion()[key] for key in ("outcome", "reason", "transport")))
        self.assertNotIn("turn/steer", self.daemon.methods())
        self.assertEqual(2, len(self.codex_calls()))

    def test_a_verified_steer_refusal_queues_once_with_the_same_id_and_says_so(self):
        self.bound()
        self.live()
        cases = {
            "no_active_turn": ("turn_ended", "The turn ended while sending (Codex: no active turn to steer), so it "
                                             "was queued instead."),
            "turn_mismatch": ("turn_changed", f"Another turn was running when the steer arrived (Codex: expected "
                                              f"active turn id `{LIVE_TURN}` but found "
                                              "`e0000000-0000-7000-8000-0000000000ff`), so it was queued instead."),
            "review_turn": ("turn_not_steerable", "The running turn can't take a steer (Codex: cannot steer a review "
                                                  "turn), so it was queued instead."),
        }
        for number, (behavior, (reason, why)) in enumerate(cases.items(), 1):
            with self.subTest(behavior=behavior):
                self.daemon.steer = behavior
                result = self.wake("--steer", "--id", f"declined-{behavior}")
                self.assertEqual("QUEUED", result["status"])
                self.assertEqual(why + " Codex accepted the queue entry; a recipient turn and consumption are "
                                 "unobserved.", result["happened"])
                self.assertEqual(number, self.daemon.methods().count("turn/steer"))
                self.assertEqual(number, len(self.codex_calls()))
                attempt = self.events("wake.attempted")[-1]
                self.assertEqual(("steer", f"declined-{behavior}"),
                                 (attempt["meta"]["requested"], attempt["meta"]["message_id"]))
                self.assertEqual({"outcome": "queued", "reason": reason, "transport": "queue",
                                  "message_id": f"declined-{behavior}"},
                                 {key: self.conclusion()[key] for key in ("outcome", "reason", "transport",
                                                                          "message_id")})

    def test_only_a_verified_refusal_lets_a_steer_fall_back_to_the_queue(self):
        self.bound()
        self.live()
        cases = {
            "null_result": ("unusable_reply", "The daemon answered the steer with no receipt object, not a receipt "
                                              f"for turn {LIVE_TURN}."),
            "empty_receipt": ("unusable_reply", f"The daemon answered the steer with no turn id, not a receipt for "
                                                f"turn {LIVE_TURN}."),
            "other_turn": ("unusable_reply", "The daemon answered the steer with a receipt for turn "
                                             f"e0000000-0000-7000-8000-0000000000ff, not a receipt for turn "
                                             f"{LIVE_TURN}."),
            "neither": ("unusable_reply", "The daemon answered the steer unreadably (its reply held neither a result "
                                          "nor an error)."),
            "both": ("unusable_reply", "The daemon answered the steer unreadably (its reply held both a result and "
                                       "an error)."),
            "codeless_error": ("unusable_reply", "The daemon answered the steer unreadably (its reply held a "
                                                 "malformed error)."),
            "internal_error": ("unrecognized_error", "Codex answered the steer with an error that doesn't show it "
                                                     "was refused before acceptance (Codex -32603: failed to steer "
                                                     "turn: synthetic internal fault)."),
            "draining": ("unrecognized_error", "Codex answered the steer with an error that doesn't show it was "
                                               "refused before acceptance (Codex -32600: Server is draining; retry "
                                               "after reconnecting)."),
            "wrong_code": ("unrecognized_error", "Codex answered the steer with an error that doesn't show it was "
                                                 "refused before acceptance (Codex -32603: no active turn to steer)."),
            "extended_refusal": ("unrecognized_error", "Codex answered the steer with an error that doesn't show it "
                                                       "was refused before acceptance (Codex -32600: no active turn to "
                                                       "steer; and the input was stored)."),
        }
        for behavior, (reason, happened) in cases.items():
            with self.subTest(behavior=behavior):
                self.daemon.steer = behavior
                result = self.wake("--steer", "--id", f"unproven-{behavior}")
                self.assertEqual(("UNCERTAIN", 5), (result["status"], result["exit_code"]))
                self.assertEqual(happened + " It may or may not have arrived.", result["happened"])
                self.assertEqual(f"Don't resend blindly: check the recipient's conversation or ask them. If it "
                                 f"didn't arrive, send it with --id unproven-{behavior}.2.", result["next"])
                conclusion = self.conclusion()
                self.assertEqual(("uncertain", reason, "steer"),
                                 (conclusion["outcome"], conclusion["reason"], conclusion["transport"]))
                self.assertNotIn("native_id", conclusion, "no receipt, no invented turn id")
                self.assertTrue(conclusion["detail"])
                again = self.wake("--steer", "--id", f"unproven-{behavior}")
                self.assertEqual("ALREADY SENT", again["status"], "an unknown outcome keeps its id blocked")
        self.assertEqual([], self.codex_calls(), "nothing unproven falls back to the queue")
        self.assertEqual(len(cases), self.daemon.methods().count("turn/steer"))

    def test_rejected_steer_then_refused_queue_is_not_sent(self):
        self.bound()
        self.live()
        self.daemon.steer = "no_active_turn"
        self.codex_mode("refuse")
        result = self.wake("--steer")
        self.assertEqual(("NOT SENT", 4), (result["status"], result["exit_code"]))
        self.assertEqual("codex queue refused before sending: Error: failed to connect to remote app server: synthetic "
                         "refusal. Nothing was queued. The steer was declined first (Codex: no active turn to steer).",
                         result["happened"])
        self.assertEqual(("not_sent", "queue_refused", "queue"),
                         tuple(self.conclusion()[key] for key in ("outcome", "reason", "transport")))

    def test_a_steer_without_an_answer_is_uncertain_and_never_queued(self):
        self.bound()
        self.live()
        for behavior in ("hang", "close"):
            self.daemon.steer = behavior
            with mock.patch.object(wake, "_STEER_TIMEOUT", 0.5):
                result = self.wake("--steer", "--id", f"unanswered-{behavior}")
            self.assertEqual(("UNCERTAIN", 5), (result["status"], result["exit_code"]))
            self.assertTrue(result["happened"].startswith("The steer was sent, but the daemon didn't answer ("))
            self.assertTrue(result["happened"].endswith("). It may or may not have arrived."))
            self.assertEqual(f"Don't resend blindly: check the recipient's conversation or ask them. If it didn't "
                             f"arrive, send it with --id unanswered-{behavior}.2.", result["next"])
            self.assertEqual(("uncertain", "no_answer", "steer"),
                             tuple(self.conclusion()[key] for key in ("outcome", "reason", "transport")))
        self.assertEqual([], self.codex_calls())

    def test_a_queue_that_does_not_finish_is_uncertain(self):
        self.bound()
        self.codex_mode("hang")
        with mock.patch.object(wake, "_QUEUE_TIMEOUT", 1):
            result = self.wake()
        self.assertEqual(("UNCERTAIN", 5), (result["status"], result["exit_code"]))
        self.assertEqual("codex queue didn't finish within 1 s. The message may or may not have been queued.",
                         result["happened"])
        self.assertEqual(("uncertain", "no_answer", "queue"),
                         tuple(self.conclusion()[key] for key in ("outcome", "reason", "transport")))

    def test_a_queue_failure_after_it_started_is_uncertain_and_keeps_the_id(self):
        self.bound()
        cases = {
            "invalid_output": ("no_receipt", f"codex queue reported success but printed no queue receipt for "
                                             f"conversation {THREAD} (Queued message \ufffd\ufffd)."),
            "no_receipt": ("no_receipt", f"codex queue reported success but printed no queue receipt for "
                                         f"conversation {THREAD} (exit 0)."),
            "other_thread": ("no_receipt", f"codex queue reported success but printed no queue receipt for "
                                           f"conversation {THREAD} (Queued message {QUEUED_ID} for thread "
                                           f"{OTHER}.)."),
            "lost_connection": ("queue_failed", "codex queue exited 1 after it may have sent the message (Error: "
                                                "failed to queue session message: connection closed before "
                                                "response)."),
            "garbled_error": ("queue_failed", "codex queue exited 1 after it may have sent the message (Error: "
                                              "\ufffd\ufffd connection reset)."),
            "post_request_error": ("queue_failed", "codex queue exited 1 after it may have sent the message (Error: "
                                                   "failed to queue session message: server error -32603: synthetic "
                                                   "failure after acceptance; diagnostic mentions failed to connect "
                                                   "to remote app server)."),
            "garbled_refusal": ("queue_failed", "codex queue exited 1 after it may have sent the message (Error: "
                                                "failed to connect to remote app server: synthetic "
                                                "\ufffd\ufffd)."),
            "receipt_and_refusal": ("queue_failed", "codex queue exited 1 after it may have sent the message (Error: "
                                                    "failed to connect to remote app server: synthetic refusal)."),
            "logged_refusal": ("queue_failed", "codex queue exited 1 after it may have sent the message (synthetic "
                                               "log line Error: failed to connect to remote app server: synthetic "
                                               "refusal)."),
            "refusal_exit_2": ("queue_failed", "codex queue exited 2 after it may have sent the message (Error: "
                                               "failed to connect to remote app server: synthetic refusal)."),
            "killed": ("queue_failed", "codex queue was stopped by signal 9 after it may have sent the message "
                                       "(exit -9)."),
        }
        for number, (mode, (reason, happened)) in enumerate(cases.items(), 1):
            with self.subTest(mode=mode):
                self.codex_mode(mode)
                result = self.wake("--id", f"after-start-{mode}")
                self.assertEqual(("UNCERTAIN", 5), (result["status"], result["exit_code"]))
                self.assertEqual(happened + " The message may or may not have been queued.", result["happened"])
                self.assertEqual(("uncertain", reason, "queue"),
                                 tuple(self.conclusion()[key] for key in ("outcome", "reason", "transport")))
                self.assertTrue(self.conclusion()["detail"])
                self.codex_mode("accept")
                self.assertEqual("ALREADY SENT", self.wake("--id", f"after-start-{mode}")["status"])
                self.assertEqual(number, len(self.codex_calls()), "a second send of the id never runs")

    def test_a_queue_that_never_started_is_not_sent_and_keeps_its_id_free(self):
        self.bound()
        broken = self.bin / "not-a-program"
        broken.write_bytes(b"\x00\x01 not an executable format")
        broken.chmod(0o755)
        result = self.wake("--codex", str(broken))
        self.assertEqual(("NOT SENT", 4), (result["status"], result["exit_code"]))
        self.assertTrue(result["happened"].startswith(f"Couldn't run {broken} ("))
        self.assertEqual(("not_sent", "codex_unavailable", "queue"),
                         tuple(self.conclusion()[key] for key in ("outcome", "reason", "transport")))
        self.assertEqual("QUEUED", self.wake()["status"])

    def test_a_refused_queue_is_not_sent_and_the_same_id_can_retry(self):
        self.bound()
        refusals = {
            "refuse": "Error: failed to connect to remote app server: synthetic refusal",
            "no_home": "Error: failed to find Codex home: synthetic missing home",
            "unsupported": "Error: the remote app server does not support thread/queue/add; update or restart the "
                           "remote app server: failed to queue session message: synthetic method not found",
        }
        for mode, said in refusals.items():
            with self.subTest(mode=mode):
                self.codex_mode(mode)
                refused = self.wake("--id", f"before-{mode}")
                self.assertEqual(("NOT SENT", 4), (refused["status"], refused["exit_code"]))
                self.assertEqual(f"codex queue refused before sending: {said}. Nothing was queued.",
                                 refused["happened"])
                self.assertEqual("Check `codex app-server daemon version`, then retry with the same message id.",
                                 refused["next"])
                self.assertEqual(("not_sent", "queue_refused", "queue"),
                                 tuple(self.conclusion()[key] for key in ("outcome", "reason", "transport")))
                self.codex_mode("accept")
                retried = self.wake("--id", f"before-{mode}")
                self.assertEqual(("QUEUED", refused["message_id"]), (retried["status"], retried["message_id"]))
        self.assertEqual(["not_sent", "queued"] * 3, [row["meta"]["outcome"] for row in self.events("wake.concluded")])

    def test_an_unreachable_daemon_sends_nothing_and_retry_is_safe(self):
        self.bound()
        self.daemon.close()
        self.socket.unlink()
        result = self.wake()
        self.assertEqual(("NOT SENT", 4), (result["status"], result["exit_code"]))
        self.assertTrue(result["happened"].startswith(f"Couldn't reach the Codex daemon at {self.socket} ("))
        self.assertTrue(result["happened"].endswith("). Nothing was sent."))
        self.assertEqual("Check `codex app-server daemon version`; retrying with the same message id is safe.",
                         result["next"])
        self.assertEqual([], self.codex_calls())
        conclusion = self.conclusion()
        self.assertEqual(("not_sent", "daemon_unreachable"), (conclusion["outcome"], conclusion["reason"]))
        self.assertNotIn("transport", conclusion)

    def test_an_unknown_conversation_sends_nothing(self):
        self.bound()
        del self.daemon.threads[THREAD]
        result = self.wake()
        self.assertEqual(("NOT SENT", 4), (result["status"], result["exit_code"]))
        self.assertEqual(f"The daemon doesn't know conversation {THREAD} (Codex -32600: thread not found). Nothing "
                         "was sent.", result["happened"])
        self.assertEqual("Check the binding with `multithread bind operator`: the conversation may be archived, "
                         "or the id is wrong. If it moved, have the exact holder refresh its binding, or make an "
                         "explicitly authorized handover with --replace, --expected-generation, --reason and "
                         "--approval-ref. Then run this wake again.", result["next"])
        self.assertEqual([], self.codex_calls())
        self.assertEqual("conversation_unknown", self.conclusion()["reason"])
        self.daemon.threads[OTHER] = {"cwd": str(self.repo), "status": "idle", "turns": []}
        self.assertEqual(0, self.handover()[0])
        moved = self.wake()
        self.assertEqual("QUEUED", moved["status"], "following the next step delivers it")
        self.assertNotEqual(result["message_id"], moved["message_id"])

    def test_a_turn_list_that_is_refused_or_unreadable_sends_nothing(self):
        self.bound()
        retry = "Check `codex app-server daemon version`; retrying with the same message id is safe."
        cases = (
            ({"error": {"code": -32603, "message": "synthetic list fault"}}, "daemon_refused",
             f"The daemon refused to list conversation {THREAD}'s turns (Codex -32603: synthetic list fault). Nothing "
             "was sent."),
            ({"result": {"data": "not a list"}}, "daemon_unreadable",
             f"The Codex daemon at {self.socket} answered the turn list unreadably (its turn list was not a list of "
             "turns). Nothing was sent."),
            ({"result": None}, "daemon_unreadable",
             f"The Codex daemon at {self.socket} answered the turn list unreadably (its turn list was not a list of "
             "turns). Nothing was sent."),
        )
        for reply, reason, happened in cases:
            with self.subTest(reason=reason, reply=reply):
                self.daemon.threads[THREAD]["list_reply"] = reply
                result = self.wake()
                self.assertEqual(("NOT SENT", happened, retry), (result["status"], result["happened"], result["next"]))
                self.assertEqual(("not_sent", reason), (self.conclusion()["outcome"], self.conclusion()["reason"]))
        self.assertEqual([], self.codex_calls())
        del self.daemon.threads[THREAD]["list_reply"]
        self.assertEqual("QUEUED", self.wake()["status"], "nothing was sent, so the id is still free")

    def test_dry_run_decides_without_sending_or_recording(self):
        self.bound()
        self.live()
        before = len(self.events())
        steer = self.wake("--steer", "--dry-run")
        queue = self.wake("--dry-run")
        self.assertEqual(("DRY RUN", 0), (steer["status"], steer["exit_code"]))
        self.assertEqual(f"Would steer into running turn {LIVE_TURN} for operator (conversation {THREAD}; "
                         f"latest turn {LIVE_TURN} is inProgress). Nothing was sent.", steer["happened"])
        self.assertEqual("Run again without --dry-run to send.", steer["next"])
        self.assertTrue(queue["happened"].startswith("Would queue for operator"))
        self.assertEqual(before, len(self.events()))
        self.assertEqual([], self.codex_calls())
        self.assertNotIn("turn/steer", self.daemon.methods())

    def test_a_dry_run_needs_no_session(self):
        self.bound()
        code, out = self.run_helper(wake.wake_main, "operator", "--ref", str(self.task), "--agent", "claude",
                                    "--codex", str(self.codex), "--dry-run")
        self.assertEqual(0, code, out)
        self.assertTrue(out.startswith("DRY RUN: Would queue for operator"))


class WakeLedgerTests(WakeCase):
    def test_the_same_reference_is_already_sent_until_a_new_id_is_chosen(self):
        self.bound()
        first = self.wake()
        connections = self.daemon.connections
        again = self.wake()
        self.assertEqual(("ALREADY SENT", 3), (again["status"], again["exit_code"]))
        attempt = self.events("wake.attempted")[0]
        self.assertEqual(f"Message {first['message_id']} went out at {attempt['recorded_at']} (queued, ledger seq "
                         f"{attempt['seq']}), and {self.task} hasn't changed since; nothing was sent again.",
                         again["happened"])
        self.assertEqual(f"To send it again on purpose, add --id {first['message_id']}.2; changed content gets its "
                         "own id.", again["next"])
        self.assertEqual(connections, self.daemon.connections, "a duplicate never reaches the daemon")
        self.assertEqual(1, len(self.codex_calls()))
        resent = self.wake("--id", first["message_id"] + ".2")
        self.assertEqual("QUEUED", resent["status"])
        self.assertEqual(f"{first['message_id']}.3", wake._next_id(resent["message_id"]))

    def test_a_changed_file_is_a_new_message_and_an_unchanged_one_is_not(self):
        self.bound()
        answers = self.base / "claude-to-codex-3.md"
        answers.write_text("Q1: yes\n")
        first = self.wake(ref=answers)
        self.assertEqual("QUEUED", first["status"])
        self.assertEqual("ALREADY SENT", self.wake(ref=answers)["status"])
        with answers.open("a") as stream:
            stream.write("Q5: go\n")
        second = self.wake(ref=answers)
        self.assertEqual("QUEUED", second["status"])
        self.assertNotEqual(first["message_id"], second["message_id"])
        body = answers.read_bytes()
        self.assertEqual((hashlib.sha256(body).hexdigest(), len(body)), (second["ref_sha256"], second["ref_size"]))
        recorded = [(row["meta"]["ref_sha256"], row["meta"]["ref_size"]) for row in self.events("wake.attempted")]
        self.assertEqual([(hashlib.sha256(b"Q1: yes\n").hexdigest(), 8), (hashlib.sha256(body).hexdigest(), 15)],
                         recorded)
        self.assertEqual(["queue", "queue"], [call[0] for call in self.codex_calls()])
        reused = self.wake("--id", first["message_id"], ref=answers)
        self.assertEqual("ALREADY SENT", reused["status"])
        self.assertNotIn("hasn't changed", reused["happened"], "the file did change since that message")
        # A ledger sequence is immutable already: its identity carries no content.
        generation = self.events("wake.bound")[-1]["seq"]
        sequence = self.wake(ref=str(generation))
        self.assertNotIn("ref_sha256", self.events("wake.attempted")[-1]["meta"])
        self.assertEqual("ALREADY SENT", self.wake(ref=str(generation))["status"])
        self.assertEqual(f"Message {sequence['message_id']} went out at {self.events('wake.attempted')[-1]['recorded_at']} "
                         f"(queued, ledger seq {self.events('wake.attempted')[-1]['seq']}); nothing was sent again.",
                         self.wake(ref=str(generation))["happened"])

    def test_an_unreadable_task_file_sends_nothing(self):
        self.bound()
        locked = self.base / "locked.md"
        locked.write_text("x")
        locked.chmod(0)
        if os.access(locked, os.R_OK):  # root reads anything; the refusal is then not observable
            locked.chmod(0o600)
            return
        result = self.wake(ref=locked)
        self.assertEqual(("NOT SENT", 4), (result["status"], result["exit_code"]))
        self.assertTrue(result["happened"].startswith(f"--ref names {locked}, which couldn't be read ("))
        self.assertEqual("Make the file readable, or pass another task file, then run this again.", result["next"])
        self.assertEqual([], self.events("wake.attempted"))

    def test_a_rebind_starts_message_ids_over(self):
        self.bound()
        self.assertEqual("QUEUED", self.wake()["status"])
        self.assertEqual("ALREADY SENT", self.wake()["status"])
        self.daemon.threads[OTHER] = dict(self.daemon.threads[THREAD])
        self.assertEqual(0, self.handover()[0])
        again = self.wake()
        self.assertEqual("QUEUED", again["status"], "the same unchanged file is a new message under a new binding")
        self.assertEqual(2, len(self.codex_calls()))

    def test_a_resend_id_stays_within_the_bound(self):
        longest = "w" * 128
        self.assertEqual("w" * 126 + ".2", wake._next_id(longest))
        self.assertEqual("w" * 125 + ".10", wake._next_id("w" * 126 + ".9"))
        self.assertEqual("x." + "9" * 124 + ".2", wake._next_id("x." + "9" * 126), "the counter can't fit, so "
                         "it counts again from the id")
        self.assertEqual("wake-operator-3-abc.3", wake._next_id("wake-operator-3-abc.2"))
        for message_id in (longest, "w" * 126 + ".9", "w" * 124 + ".999", "x." + "9" * 126, "x." + "9" * 125,
                           "x.2", "7"):
            suggested = wake._next_id(message_id)
            self.assertLessEqual(len(suggested), 128)
            self.assertNotEqual(message_id, suggested)
            self.assertEqual(suggested, wake.canonical_wake_message_id(suggested))

    def test_an_uncertain_or_open_attempt_blocks_its_id(self):
        self.bound()
        self.live()
        self.daemon.steer = "hang"
        with mock.patch.object(wake, "_STEER_TIMEOUT", 0.5):
            uncertain = self.wake("--steer")
        again = self.wake("--steer")
        self.assertEqual("ALREADY SENT", again["status"])
        self.assertIn(f"An earlier attempt with message id {uncertain['message_id']} (ledger seq", again["happened"])
        self.assertTrue(again["happened"].endswith(") is uncertain, so it may have arrived. Nothing was sent again."))
        self.assertEqual(f"Check the recipient's conversation first. If it didn't arrive, send it with --id "
                         f"{uncertain['message_id']}.2.", again["next"])
        # A crash between recording the attempt and its outcome leaves it open.
        content = {"ref_sha256": "0" * 64, "ref_size": 1}
        with self.store() as store:
            store.wake_begin("operator", ref="/srv/other.md", requested="queue", agent="claude", session="crashed",
                             **content)
            open_id = store.wake_plan("operator", ref="/srv/other.md", requested="queue", **content)["message_id"]
        blocked = self.wake("--id", open_id)
        self.assertEqual("ALREADY SENT", blocked["status"])
        self.assertIn("never recorded its outcome, so it may have arrived", blocked["happened"])

    def test_paused_and_unbound_roles_send_nothing_and_name_the_fix(self):
        self.bound()
        self.assertEqual(0, self.control("pause")[0])
        before = (len(self.events()), self.daemon.connections)
        paused = self.wake()
        self.assertEqual(("NOT SENT", 4), (paused["status"], paused["exit_code"]))
        pause = self.events("wake.paused")[-1]
        self.assertEqual(f"Wakes to operator are paused (binding {pause['meta']['generation']}, paused at ledger seq "
                         f"{pause['seq']}). Nothing was sent.", paused["happened"])
        self.assertEqual(f"Resume with `multithread resume operator`, then send again; message id "
                         f"{paused['message_id']} is still unused.", paused["next"])
        self.assertEqual(before, (len(self.events()), self.daemon.connections))
        self.control("resume")
        resumed = self.wake()
        self.assertEqual(("QUEUED", paused["message_id"]), (resumed["status"], resumed["message_id"]))
        self.control("unbind")
        unbound = self.wake("--id", "after-unbind")
        self.assertEqual(("NOT SENT", 4), (unbound["status"], unbound["exit_code"]))
        self.assertEqual(f"No conversation is bound to operator in the ledger at {self.repo}. Nothing was sent.",
                         unbound["happened"])
        self.assertEqual("Bind one with `multithread bind operator --thread <codex conversation id>`, or pass "
                         "--repo for the ledger that holds this binding.", unbound["next"])
        self.assertEqual(1, len(self.codex_calls()))

    def test_a_sequence_reference_names_its_ledger_and_must_exist(self):
        generation = self.bound()
        result = self.wake(ref=str(generation))
        self.assertEqual(f"Multithread wake from claude: ledger sequence {generation} in {self.repo}",
                         result["text"])
        missing = self.wake(ref="999")
        self.assertEqual(("NOT SENT", 4), (missing["status"], missing["exit_code"]))
        self.assertIn("ledger sequence 999 doesn't exist here", missing["happened"])
        self.assertEqual("Correct that and run this again.", missing["next"])

    def test_bad_input_refuses_before_the_ledger_or_the_daemon(self):
        self.bound()
        cases = [
            (["--ref", "relative/task.md"], "ref must be an absolute task-file path or a ledger sequence number"),
            (["--ref", str(self.base / "missing.md")], "which isn't a file here"),
            (["--ref", str(self.task), "--id", "has space"], "message id must be"),
        ]
        calls = len(self.ledger_calls)
        for argv, expected in cases:
            code, out = self.run_helper(wake.wake_main, "operator", *argv, "--agent", "claude", "--session", "s",
                                        "--codex", str(self.codex))
            self.assertEqual(4, code, out)
            self.assertTrue(out.startswith("NOT SENT: "), out)
            self.assertIn(expected, out)
        code, out = self.run_helper(wake.wake_main, "operator", "--ref", str(self.task), "--codex", str(self.codex))
        self.assertEqual(4, code)
        self.assertIn("pass --agent and --session (or set RELAY_AGENT and RELAY_SESSION)", out)
        self.assertEqual(calls, len(self.ledger_calls))
        self.assertEqual(1, self.daemon.connections, "only bind reached the daemon")

    def test_codex_is_needed_only_to_queue(self):
        self.bound()
        self.live()
        missing = str(self.base / "no-codex")
        steered = self.wake("--steer", "--codex", missing)
        self.assertEqual("STEERED", steered["status"])
        self.daemon.steer = "no_active_turn"
        result = self.wake("--steer", "--id", "needs-queue", "--codex", missing)
        self.assertEqual(("NOT SENT", 4), (result["status"], result["exit_code"]))
        self.assertEqual(f"Codex wasn't found at {missing}, so nothing was queued. The steer was declined first "
                         "(Codex: no active turn to steer).", result["happened"])
        self.assertEqual("Pass --codex with Codex's absolute path, or put codex on PATH, then retry with the same "
                         "message id.", result["next"])
        self.assertEqual(("not_sent", "codex_unavailable", "queue"),
                         tuple(self.conclusion()[key] for key in ("outcome", "reason", "transport")))
        self.assertEqual("QUEUED", self.wake("--steer", "--id", "needs-queue")["status"],
                         "a queue that never started leaves its id free")

    def test_a_ledger_that_cannot_record_sends_nothing(self):
        self.bound()
        self.failing = {"begin"}
        result = self.wake()
        self.assertEqual(("NOT SENT", 4), (result["status"], result["exit_code"]))
        self.assertEqual(f"The ledger at {self.repo} couldn't record this wake: synthetic ledger failure. Nothing "
                         "was sent.", result["happened"])
        self.assertEqual(f"Check the ledger with `{LAUNCHER} --repo {self.repo} doctor`, then run this again.",
                         result["next"])
        self.assertEqual([], self.codex_calls())

    def test_text_output_shows_a_control_character_escaped(self):
        folder = self.base / "notes\x1b[2J\u202e"
        folder.mkdir()
        # The reference itself is refused when it carries one; here only the checkout path does.
        for main, argv in ((wake.wake_main, ["operator", "--ref", str(self.task)]),
                           (wake.bind_main, ["operator", "--thread", THREAD])):
            with self.subTest(main=main.__name__):
                code, out = self.run_helper(main, *argv, "--agent", "claude", "--session", "s", "--repo", str(folder))
                self.assertEqual(4, code)
                self.assertNotIn("\x1b", out)
                self.assertNotIn("\u202e", out)
                self.assertIn("ledger at " + str(self.base) + "/notes\\u001b[2J\\u202e", out)

    def test_an_unenrolled_checkout_is_pointed_at_enrollment_not_doctor(self):
        refusal = (f"this checkout is not enrolled: {json.dumps(str(self.repo))}. If this is the repository you "
                   f"want Multithread in, enroll it with: {LAUNCHER} setup --repo {self.repo} --apply")
        self.ledger = lambda repo, *arguments: (78, None, refusal)
        enroll = (f"If this is the checkout you want Multithread in, enroll it with "
                  f"`{LAUNCHER} setup --repo {self.repo} --apply`, then ")
        result = self.wake()
        self.assertEqual(("NOT SENT", 4), (result["status"], result["exit_code"]))
        self.assertEqual(f"The ledger at {self.repo} couldn't record this wake: {refusal}. Nothing was sent.",
                         result["happened"])
        self.assertEqual(enroll + "run this again.", result["next"])
        code, out = self.bind()
        self.assertEqual(4, code)
        self.assertTrue(out.endswith("Next: " + enroll + "run bind again.\n"), out)
        self.assertEqual([], self.codex_calls())
        self.assertEqual(0, self.daemon.connections)

    def test_a_folder_outside_git_names_the_way_back(self):
        folder = self.base / "relay-notes"
        folder.mkdir()
        (folder / "task.md").write_text("x")
        code, out = self.run_helper(wake.wake_main, "operator", "--ref", str(folder / "task.md"), "--agent",
                                    "claude", "--session", "s", "--repo", str(folder))
        self.assertEqual(4, code)
        self.assertEqual([f"NOT SENT: The ledger at {folder} couldn't record this wake: {json.dumps(str(folder))} "
                          "is not a Git checkout: run from an enrolled checkout or pass --repo <checkout>. Nothing "
                          "was sent.", "Next: Run it from an enrolled checkout, or pass --repo <checkout>."],
                         out.splitlines())
        code, out = self.run_helper(wake.bind_main, "operator", "--thread", THREAD, "--agent", "claude",
                                    "--session", "s", "--repo", str(folder))
        self.assertEqual(4, code)
        self.assertTrue(out.startswith(f"NOT BOUND: Nothing was recorded: the ledger at {folder} couldn't show its "
                                       f"bindings: {json.dumps(str(folder))} is not a Git checkout"))
        self.assertTrue(out.endswith("Next: Run it from an enrolled checkout, or pass --repo <checkout>.\n"))
        self.assertEqual(0, self.daemon.connections)

    def test_an_unrecorded_outcome_warns_and_keeps_the_attempt_open(self):
        self.bound()
        self.failing = {"conclude"}
        result = self.wake()
        self.assertEqual("QUEUED", result["status"])
        self.assertEqual(f"The ledger didn't record this outcome (synthetic ledger failure). Attempt "
                         f"{result['attempt_seq']} stays open, so message id {result['message_id']} reports ALREADY "
                         "SENT until someone checks it.", result["warning"])
        self.failing = set()
        self.assertEqual("ALREADY SENT", self.wake()["status"])

    def test_human_output_is_status_message_and_next_step(self):
        self.bound()
        code, out = self.run_helper(wake.wake_main, "operator", "--ref", str(self.task), "--agent", "claude",
                                    "--session", "s", "--codex", str(self.codex))
        self.assertEqual(0, code)
        attempt = self.events("wake.attempted")[-1]["meta"]
        self.assertEqual([
            "QUEUED: Queued as asked. Codex accepted the queue entry; a recipient turn and consumption are unobserved.",
            f"Message {attempt['message_id']}: \"Multithread wake from claude: {self.task}\"",
            "Next: Nothing. Wait for the recipient's acknowledgement.",
        ], out.splitlines())


class FakeInbox:
    """A Claude Code inbox: a private Unix socket that reads complete lines."""

    def __init__(self, path):
        self.path = path
        self.lines = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        os.chmod(path, 0o600)
        self.server.listen(4)
        self.server.settimeout(0.05)
        self.stopped = threading.Event()
        self.worker = threading.Thread(target=self.serve, daemon=True)
        self.worker.start()

    def serve(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            with connection:
                connection.settimeout(5)
                data = b""
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                self.lines.extend(data.splitlines(keepends=True))

    def close(self):
        self.stopped.set()
        self.worker.join(5)
        self.server.close()


class ClaudeTests(WakeCase):
    def setUp(self):
        super().setUp()
        self.inbox_path = self.codex_home / "7739.sock"
        self.inbox = FakeInbox(self.inbox_path)
        self.addCleanup(self.inbox.close)

    def bind_inbox(self, *extra, path=None):
        code, out = self.run_helper(wake.bind_main, "reviewer", "--claude-socket", str(path or self.inbox_path),
                                    "--agent", "claude", "--session", "self", "--json", *extra)
        return code, json.loads(out)

    def wake_inbox(self, *extra):
        code, out = self.run_helper(wake.wake_main, "reviewer", "--ref", str(self.task), "--agent", "codex",
                                    "--session", "sol", "--json", *extra)
        result = json.loads(out)
        self.assertEqual(code, result["exit_code"])
        return result

    def received(self):
        for _ in range(100):
            if self.inbox.lines:
                break
            threading.Event().wait(0.02)
        return [json.loads(line) for line in self.inbox.lines]

    def test_bind_records_the_inbox_without_the_daemon(self):
        code, result = self.bind_inbox()
        self.assertEqual((0, "BOUND"), (code, result["status"]))
        bound = self.events("wake.bound")[-1]
        self.assertEqual({"role": "reviewer", "provider": "claude", "endpoint": f"unix://{self.inbox_path}"},
                         bound["meta"])
        self.assertEqual(f"reviewer now wakes the Claude Code session whose inbox is {self.inbox_path}, as binding "
                         f"{bound['seq']} in the ledger at {self.repo}.", result["happened"])
        self.assertEqual("Send a wake with: multithread wake reviewer --ref <task file>. If that session ends or "
                         "restarts, bind again from the new one.", result["next"])
        self.assertEqual(0, self.daemon.connections)
        self.assertEqual([], self.inbox.lines, "binding sends nothing")
        self.assertEqual("ALREADY BOUND", self.bind_inbox()[1]["status"])

    def test_bind_refuses_anything_but_a_private_socket(self):
        link = self.base / "link.sock"
        link.symlink_to(self.inbox_path)
        plain = self.base / "plain"
        plain.write_text("")
        plain.chmod(0o600)
        for path, why in ((self.base / "gone.sock", "it doesn't exist"), (link, "it is a symbolic link"),
                          (plain, "it isn't a socket")):
            code, result = self.bind_inbox(path=path)
            self.assertEqual((4, "NOT BOUND"), (code, result["status"]))
            self.assertEqual(f"{path} isn't a usable Claude Code inbox: {why}. Nothing was recorded.",
                             result["happened"])
            self.assertEqual('Run bind from the Claude Code session to wake, passing its own inbox: multithread '
                             'bind reviewer --claude-socket "$CLAUDE_CODE_MESSAGING_SOCKET"', result["next"])
        os.chmod(self.inbox_path, 0o660)
        self.assertIn("its mode is 660, not 600", self.bind_inbox()[1]["happened"])
        os.chmod(self.inbox_path, 0o600)
        with mock.patch.object(wake.os, "getuid", return_value=os.getuid() + 1):
            self.assertIn("another user owns it", self.bind_inbox()[1]["happened"])
        code, out = self.run_helper(wake.bind_main, "reviewer", "--claude-socket", "cc-socks/7739.sock",
                                    "--agent", "a", "--session", "s")
        self.assertIn("--claude-socket must be an absolute path", out)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            wake.bind_main(["reviewer", "--thread", THREAD, "--claude-socket", str(self.inbox_path)],
                           ledger=self.ledger)
        self.assertEqual([], self.events("wake.bound"))

    def test_wake_delivers_one_user_line_and_records_it(self):
        self.bind_inbox()
        result = self.wake_inbox()
        self.assertEqual(("DELIVERED TO INBOX", 0), (result["status"], result["exit_code"]))
        self.assertEqual(f"The Claude Code inbox at {self.inbox_path} accepted the message. Recipient turn state "
                         "and consumption are unobserved.", result["happened"])
        self.assertEqual("unknown", result["consumption_state"])
        self.assertEqual({"reachability": "reachable", "turn_state": "unknown", "source": "inbox_connection"},
                         result["recipient_state"])
        self.assertEqual("Wait for the recipient's acknowledgement. The session may still hold or refuse the message "
                         "under its inbound settings (crossSessionInbound); if nothing arrives, ask the person to "
                         "check that session.", result["next"])
        text = f"Multithread wake from codex: {self.task}"
        self.assertEqual([{"type": "user", "message": {"role": "user", "content": text}}], self.received())
        self.assertEqual(1, self.inbox.lines[0].count(b"\n"))
        self.assertTrue(self.inbox.lines[0].endswith(b"}\n"))
        self.inbox.lines[0].decode("ascii")
        attempt = self.events("wake.attempted")[-1]["meta"]
        self.assertEqual(("claude", "queue"), (attempt["provider"], attempt["requested"]))
        self.assertNotIn("thread", attempt)
        self.assertEqual(("delivered", "inbox_accepted", "inbox"),
                         tuple(self.conclusion()[key] for key in ("outcome", "reason", "transport")))
        self.assertEqual([], self.codex_calls())
        self.assertEqual(0, self.daemon.connections)
        again = self.wake_inbox()
        self.assertEqual("ALREADY SENT", again["status"])
        self.assertIn("(delivered, ledger seq", again["happened"])
        self.assertEqual(1, len(self.received()))

    def test_steer_has_no_separate_meaning_for_an_inbox(self):
        self.bind_inbox()
        result = self.wake_inbox("--steer")
        self.assertEqual("DELIVERED TO INBOX", result["status"])
        self.assertTrue(result["happened"].endswith(" Claude Code has no separate steer, so --steer changed nothing."))
        self.assertEqual("steer", self.events("wake.attempted")[-1]["meta"]["requested"])
        dry = self.wake_inbox("--steer", "--dry-run", "--id", "dry")
        self.assertEqual(f"Would deliver to the Claude Code inbox at {self.inbox_path} for reviewer. Nothing was "
                         "sent. Claude Code has no separate steer, so --steer changed nothing.", dry["happened"])
        self.assertEqual(1, len(self.received()))

    def test_an_ended_session_or_a_refused_connection_sends_nothing_and_names_the_rebind(self):
        self.bind_inbox()
        rebind = ('Have the exact holder refresh reviewer from its own Claude Code session with --claude-socket '
                  '"$CLAUDE_CODE_MESSAGING_SOCKET". Moving it to another owner requires an explicitly authorized '
                  'handover with --replace, --expected-generation, --reason and --approval-ref. Then run this wake '
                  'again.')
        self.inbox.close()  # The listener is gone but its socket file remains: a refused connection.
        refused = self.wake_inbox()
        self.assertEqual(("NOT SENT", 4), (refused["status"], refused["exit_code"]))
        self.assertTrue(refused["happened"].startswith(f"The Claude Code inbox at {self.inbox_path} refused the "
                                                       "connection ("))
        self.assertTrue(refused["happened"].endswith("), so that session may have ended. Nothing was sent."))
        self.assertEqual(rebind, refused["next"])
        self.assertEqual(("not_sent", "inbox_refused", "inbox"),
                         tuple(self.conclusion()[key] for key in ("outcome", "reason", "transport")))
        self.inbox_path.unlink()
        gone = self.wake_inbox()
        self.assertEqual(f"The Claude Code inbox at {self.inbox_path} is missing; the session's state is unknown. "
                         "Nothing was sent.", gone["happened"])
        self.assertEqual(rebind, gone["next"])
        self.assertEqual(refused["message_id"], gone["message_id"], "a message that was not sent keeps its id")
        self.assertEqual(("not_sent", "inbox_missing"), (self.conclusion()["outcome"], self.conclusion()["reason"]))
        self.inbox_path.write_text("")
        self.inbox_path.chmod(0o600)
        unsafe = self.wake_inbox()
        self.assertEqual(f"The Claude Code inbox at {self.inbox_path} failed its checks: it isn't a socket. Nothing "
                         "was sent.", unsafe["happened"])
        self.assertEqual(rebind, unsafe["next"])
        self.assertEqual("inbox_unsafe", self.conclusion()["reason"])
        restarted = FakeInbox(self.codex_home / "7740.sock")
        self.addCleanup(restarted.close)
        self.assertEqual(0, self.bind_inbox(path=restarted.path)[0])
        delivered = self.wake_inbox()
        self.assertEqual("DELIVERED TO INBOX", delivered["status"], "following the next step delivers it")
        self.assertNotEqual(gone["message_id"], delivered["message_id"])
        self.inbox = restarted
        self.assertEqual(1, len(self.received()))

    def test_a_connection_dropped_while_sending_is_uncertain(self):
        self.bind_inbox()
        with mock.patch.object(wake.socket.socket, "sendall", side_effect=BrokenPipeError("synthetic drop")):
            result = self.wake_inbox()
        self.assertEqual(("UNCERTAIN", 5), (result["status"], result["exit_code"]))
        self.assertEqual(f"The connection to the Claude Code inbox at {self.inbox_path} dropped while sending "
                         "(synthetic drop). The message may or may not have arrived.", result["happened"])
        self.assertEqual(f"Don't resend blindly: check the recipient's session or ask them. If it didn't arrive, "
                         f"send it with --id {result['message_id']}.2.", result["next"])
        self.assertEqual(("uncertain", "dropped", "inbox"),
                         tuple(self.conclusion()[key] for key in ("outcome", "reason", "transport")))
        self.assertEqual("ALREADY SENT", self.wake_inbox()["status"])

    def test_show_names_the_inbox(self):
        self.bind_inbox()
        code, out = self.run_helper(wake.bind_main, "reviewer")
        bound = self.events("wake.bound")[-1]
        self.assertEqual([f"reviewer: active, bound to the Claude Code inbox {self.inbox_path} (binding "
                          f"{bound['seq']}, by claude:self at {bound['recorded_at']})"], out.splitlines())


class BindTests(WakeCase):
    def test_paused_role_stays_paused_through_refresh_and_authorized_handover(self):
        self.bound()
        self.assertEqual(0, self.control("pause")[0])
        code, out = self.bind("--charter", "Updated paused responsibility", "--json")
        refreshed = json.loads(out)
        self.assertEqual((0, "BOUND", "paused"), (code, refreshed["status"], refreshed["binding_state"]))
        self.assertIn("Wakes remain paused", refreshed["happened"])
        self.assertIn("deliberately resume", refreshed["next"])
        self.daemon.threads[OTHER] = dict(self.daemon.threads[THREAD])
        code, out = self.handover(OTHER, "--json")
        moved = json.loads(out)
        self.assertEqual((0, "paused"), (code, moved["binding_state"]))
        self.assertIn("deliberately resume", moved["next"])
        self.assertEqual(3, len(self.events("wake.paused")))
        daemon_calls = list(self.daemon.methods())
        refused = self.wake()
        self.assertEqual("NOT SENT", refused["status"])
        self.assertIn("paused", refused["happened"])
        self.assertEqual(daemon_calls, self.daemon.methods())
        self.assertEqual([], self.codex_calls())
        self.assertEqual([], self.events("wake.attempted"))
        self.assertEqual(0, self.control("resume")[0])
        self.assertEqual("QUEUED", self.wake()["status"])

    def test_handover_requires_each_authorization_field_before_recipient_probe(self):
        generation = self.bound()
        self.daemon.threads[OTHER] = dict(self.daemon.threads[THREAD])
        fields = (("--expected-generation", str(generation)), ("--reason", "approved fixture move"),
                  ("--approval-ref", "receipt:fixture-handover"))
        for missing in range(len(fields)):
            extra = [part for index, field in enumerate(fields) if index != missing for part in field]
            with self.subTest(missing=fields[missing][0]):
                code, out = self.bind("--replace", *extra, thread=OTHER)
                self.assertEqual(4, code, out)
                self.assertIn("--approval-ref", out)
        self.assertEqual(1, self.daemon.connections)
        self.assertEqual(1, len(self.events("wake.bound")))

    def test_binding_metadata_and_same_holder_refresh_forward_observed_generation(self):
        code, out = self.bind("--scope", "docs:review", "--charter", "Review scoped docs", "--json")
        self.assertEqual(0, code, out)
        first = self.events("wake.bound")[-1]["seq"]
        code, out = self.bind("--charter", "Review docs and receipts", "--reason", "scope clarification", "--json")
        result = json.loads(out)
        self.assertEqual((0, "BOUND"), (code, result["status"]))
        binding_call = self.ledger_calls[-1][1]
        self.assertIn("--replace", binding_call)
        self.assertEqual(str(first), binding_call[binding_call.index("--expected-generation") + 1])
        with self.store() as store:
            binding = store.wake_bindings("operator")["bindings"][0]
        self.assertEqual(("docs:review", "Review docs and receipts"), (binding["role_scope"], binding["charter"]))
        code, out = self.run_helper(wake.bind_main, "operator")
        self.assertEqual(0, code)
        self.assertIn("  scope: docs:review", out)
        self.assertIn("  charter: Review docs and receipts", out)

    def test_actual_codex_recipient_can_refresh_and_stale_generation_refuses(self):
        first = self.bound()
        code, out = self.run_helper(wake.bind_main, "operator", "--thread", THREAD, "--agent", "codex",
                                    "--session", THREAD, "--charter", "Current recipient responsibility", "--json")
        self.assertEqual((0, "BOUND"), (code, json.loads(out)["status"]))
        self.assertEqual(str(first), self.ledger_calls[-1][1][self.ledger_calls[-1][1].index("--expected-generation") + 1])
        before = len(self.events())
        code, out = self.run_helper(wake.bind_main, "operator", "--thread", THREAD, "--agent", "codex",
                                    "--session", THREAD, "--expected-generation", str(first), "--json")
        self.assertEqual((4, "NOT BOUND"), (code, json.loads(out)["status"]))
        self.assertIn("generation", json.loads(out)["happened"])
        self.assertEqual(before, len(self.events()))

    def test_foreign_holder_needs_complete_authorization_even_for_same_recipient_metadata(self):
        self.bound()
        before = len(self.events())
        connections = self.daemon.connections
        code, out = self.run_helper(wake.bind_main, "operator", "--thread", THREAD, "--agent", "claude",
                                    "--session", "another-holder", "--charter", "New responsibility", "--replace", "--json")
        result = json.loads(out)
        self.assertEqual((4, "NOT BOUND"), (code, result["status"]))
        self.assertIn("--approval-ref", result["next"])
        self.assertEqual(before, len(self.events()))
        self.assertEqual(connections, self.daemon.connections)

    def test_bind_checks_the_conversation_and_records_its_generation(self):
        code, out = self.run_helper(wake.bind_main, "operator", "--thread", THREAD, "--agent", "claude",
                                    "--session", "binder", "--json")
        result = json.loads(out)
        self.assertEqual((0, "BOUND"), (code, result["status"]))
        bound = self.events("wake.bound")[-1]
        self.assertEqual(bound["seq"], result["generation"])
        self.assertEqual({"role": "operator", "provider": "codex", "thread": THREAD,
                          "endpoint": f"unix://{self.socket}", "cwd": str(self.repo)}, bound["meta"])
        self.assertEqual(f"operator now wakes Codex conversation {THREAD} (idle; cwd {self.repo}) as binding "
                         f"{bound['seq']} in the ledger at {self.repo}.", result["happened"])
        self.assertEqual("Send a wake with: multithread wake operator --ref <task file>", result["next"])
        read = [m["params"] for m in self.daemon.requests if m.get("method") == "thread/read"]
        self.assertEqual([{"threadId": THREAD, "includeTurns": False}], read)
        self.assertEqual({"initialize", "thread/read"}, set(self.daemon.methods()), "bind only reads")

    def test_bind_warns_when_the_conversation_leaves_no_ledger_events(self):
        code, out = self.run_helper(wake.bind_main, "operator", "--thread", THREAD, "--agent", "claude",
                                    "--session", "binder", "--json")
        result = json.loads(out)
        self.assertEqual((0, "BOUND", "not_reaching"), (code, result["status"], result["coverage"]["state"]))
        self.assertEqual(f"Conversation {THREAD} has left no events in the ledger at {self.repo}: Multithread's "
                         "user-level Codex hooks aren't installed. It gets no Multithread brief, so every wake to "
                         "it must carry its own pointer: pass a task-file path as --ref, not a bare sequence. Fix: "
                         f"{LAUNCHER} hooks install --client codex.", result["warning"])
        # Once the conversation's own hooks have reached the ledger, there is nothing to warn about.
        with self.store() as store:
            store.wake_control("unbind", "operator", agent="claude", session="binder")
            store.emit({"kind": "session.started", "agent": "codex", "session": THREAD, "summary": "Codex started",
                        "meta": {"client": "codex", "branch": "main", "worktree": "primary"}})
        code, out = self.run_helper(wake.bind_main, "operator", "--thread", THREAD, "--agent", "claude",
                                    "--session", "binder", "--json")
        result = json.loads(out)
        self.assertEqual(("BOUND", {"state": "reaching", "ledger": str(self.repo)}),
                         (result["status"], result["coverage"]))
        self.assertNotIn("warning", result)

    def test_bind_warns_when_the_conversation_checkout_has_no_ledger(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.daemon.threads[THREAD]["cwd"] = str(elsewhere)
        code, out = self.run_helper(wake.bind_main, "operator", "--thread", THREAD, "--agent", "claude",
                                    "--session", "binder")
        self.assertEqual(0, code, out)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("BOUND: operator now wakes Codex conversation"))
        self.assertTrue(lines[1].startswith(f"Warning: Couldn't confirm that conversation {THREAD} reaches a "
                                            f"ledger: the checkout at {elsewhere} answered \""))
        self.assertIn("every wake to it must carry its own pointer", lines[1])
        self.assertTrue(lines[1].endswith(f"Check with: {LAUNCHER} setup --repo {elsewhere} --check"))

    def test_bind_refuses_an_unknown_conversation_or_an_unreachable_daemon(self):
        code, out = self.bind(thread=OTHER)
        self.assertEqual(4, code)
        self.assertEqual([f"NOT BOUND: The daemon at {self.socket} doesn't know conversation {OTHER} (Codex -32600: "
                          "thread not found). Nothing was recorded.",
                          "Next: Check the id: it is the conversation's exact UUID, not its title. Then run bind "
                          "again."], out.splitlines())
        self.daemon.close()
        self.socket.unlink()
        code, out = self.bind()
        self.assertEqual(4, code)
        self.assertTrue(out.startswith(f"NOT BOUND: Couldn't reach the Codex daemon at {self.socket} ("))
        self.assertIn("Next: Check `codex app-server daemon version`, then run bind again.", out)
        self.assertEqual([], self.events("wake.bound"))

    def test_bind_refuses_an_answer_about_another_conversation(self):
        self.daemon.threads[THREAD]["answers_as"] = OTHER
        code, out = self.bind()
        self.assertEqual(4, code)
        self.assertEqual([f"NOT BOUND: The daemon's answer for conversation {THREAD} didn't identify it with a "
                          "working directory. Nothing was recorded.",
                          "Next: Check `codex app-server daemon version`, then run bind again."], out.splitlines())
        self.assertEqual([], self.events("wake.bound"))

    def test_bind_refuses_a_recipient_move_until_authorized_handover(self):
        first = self.bound()
        self.daemon.threads[OTHER] = {"cwd": str(self.repo), "status": "active", "turns": []}
        code, out = self.bind(thread=OTHER)
        self.assertEqual(4, code)
        self.assertEqual(1, self.daemon.connections, "a refused move never reaches the daemon")
        bound_at = self.events("wake.bound")[-1]["recorded_at"]
        self.assertEqual([f"NOT BOUND: operator is already bound to Codex conversation {THREAD} (binding {first}, since "
                          f"{bound_at}). Nothing was recorded.",
                          "Next: Have its exact holder claude:binder refresh its existing recipient, or make an "
                          f"explicitly authorized handover with --replace, --expected-generation {first}, "
                          "--reason <reason> and --approval-ref <immutable approval reference>."],
                         out.splitlines())
        old_id = self.wake("--dry-run")["message_id"]
        code, out = self.bind("--replace", thread=OTHER)
        self.assertEqual(4, code, out)
        self.assertNotIn("To move it, run:", out)
        self.assertEqual(2, self.daemon.connections, "bare replacement never reaches the daemon")
        code, out = self.handover()
        self.assertEqual(0, code, out)
        second = self.events("wake.bound")[-1]
        self.assertEqual(first, second["meta"]["replaces"])
        self.assertIn(f"It replaces binding {first} (Codex conversation {THREAD}); message ids start over under "
                      "this binding, so a file already sent under the old one can be sent again.", out)
        self.assertNotEqual(old_id, self.wake("--dry-run")["message_id"], "a new generation gets new ids")
        code, out = self.bind(thread=OTHER)
        self.assertEqual(0, code)
        self.assertTrue(out.startswith(f"ALREADY BOUND: operator already wakes Codex conversation {OTHER}"))

    def test_bind_refuses_malformed_input_before_anything(self):
        for argv, expected in (
                (["operator", "--thread", "Operator Console", "--agent", "a", "--session", "s"],
                 "thread must be the conversation's exact lowercase UUID, not a title"),
                (["Operator", "--thread", THREAD, "--agent", "a", "--session", "s"], "role must be a lowercase name"),
                (["operator", "--thread", THREAD], "pass --agent and --session")):
            code, out = self.run_helper(wake.bind_main, *argv)
            self.assertEqual(4, code)
            self.assertTrue(out.startswith("NOT BOUND: Nothing was recorded: "), out)
            self.assertIn(expected, out)
        self.assertEqual(0, self.daemon.connections)
        self.assertEqual([], self.ledger_calls)

    def test_show_lists_bindings_and_their_last_wake(self):
        code, out = self.run_helper(wake.bind_main)
        self.assertEqual((0, [f"No roles are bound in the ledger at {self.repo}.",
                              "Next: multithread bind <role> --thread <codex conversation id>"]),
                         (code, out.splitlines()))
        generation = self.bound()
        sent = self.wake()
        code, out = self.run_helper(wake.bind_main, "operator")
        bound = self.events("wake.bound")[-1]
        attempt = self.events("wake.attempted")[-1]
        self.assertEqual([
            f"operator: active, bound to Codex conversation {THREAD} (binding {generation}, by claude:binder at "
            f"{bound['recorded_at']})",
            f"  cwd when bound: {self.repo}; daemon: unix://{self.socket}",
            f"  last wake: {sent['message_id']} queued at {attempt['recorded_at']} (ledger seq {attempt['seq']})",
        ], out.splitlines())
        code, out = self.run_helper(wake.bind_main, "reviewer")
        self.assertEqual([f"reviewer is not bound in the ledger at {self.repo}.",
                          "Next: multithread bind reviewer --thread <codex conversation id>"], out.splitlines())


class LauncherLedgerTests(unittest.TestCase):
    """The real ledger path: one installed-launcher run per step, read by exit code and JSON."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-wake-launcher-")
        self.addCleanup(temporary.cleanup)
        self.launcher = Path(temporary.name) / "multithread"
        patcher = mock.patch.object(wake, "account_launcher", return_value=self.launcher)
        patcher.start()
        self.addCleanup(patcher.stop)

    def script(self, body):
        self.launcher.write_text("#!/usr/bin/python3 -I\nimport json, sys, time\n" + body)
        self.launcher.chmod(0o755)

    def test_a_step_passes_its_arguments_and_returns_json(self):
        self.script("print(json.dumps({'argv': sys.argv[1:]}))\n")
        code, result, problem = wake.launcher_ledger(Path("/srv/checkout"), "wake-ledger", "show", "operator")
        self.assertEqual((0, ""), (code, problem))
        self.assertEqual(["--repo", "/srv/checkout", "--json", "wake-ledger", "show", "operator"], result["argv"])

    def test_a_refusal_keeps_its_exit_code_and_last_line(self):
        self.script("print('multithread: first', file=sys.stderr)\n"
                    "print('multithread: operator isn\\'t bound', file=sys.stderr)\nraise SystemExit(73)\n")
        self.assertEqual((73, None, "operator isn't bound"), wake.launcher_ledger(Path("/srv/checkout"), "pause"))

    def test_silence_non_json_and_a_missing_launcher_are_failures(self):
        self.script("time.sleep(30)\n")
        with mock.patch.object(wake, "_LEDGER_TIMEOUT", 1):
            self.assertEqual((None, None, "it didn't answer within 1 s"),
                             wake.launcher_ledger(Path("/srv/checkout"), "show"))
        self.script("print('not json')\n")
        self.assertEqual((1, None, "it returned output that is not JSON"),
                         wake.launcher_ledger(Path("/srv/checkout"), "show"))
        self.launcher.unlink()
        code, result, problem = wake.launcher_ledger(Path("/srv/checkout"), "show")
        self.assertEqual((1, None), (code, result))
        self.assertTrue(problem.startswith(f"{self.launcher} could not run ("))


class GuideTests(unittest.TestCase):
    """What an agent reads before waking from Codex's sandbox, not after the refusal."""

    def test_the_guides_send_a_sandboxed_wake_through_escalation(self):
        rule = ("run `multithread wake` through Codex's approved escalation outside the sandbox, and never work "
                "around the launcher check.")
        for name in ("docs/PEER.md", "skills/multithread/SKILL.md"):
            text = " ".join((ROOT / name).read_text(encoding="utf-8").split())
            with self.subTest(guide=name):
                self.assertIn("unsafe launcher ancestry", text)
                self.assertIn(rule, text)
        self.assertIn("(docs/PEER.md#wake-an-existing-conversation)", (ROOT / "README.md").read_text())

    def test_the_outcome_table_lists_every_wake_status_with_its_exit_code(self):
        section = (ROOT / "docs/PEER.md").read_text(encoding="utf-8").split("## Wake an existing conversation")[1]
        rows = re.findall(r"^\| `([A-Z ]+)` \| (\d) \|", section, re.MULTILINE)
        binding = {"BOUND", "ALREADY BOUND", "NOT BOUND"}
        self.assertEqual({status: code for status, code in wake.EXIT_CODES.items() if status not in binding},
                         {status: int(code) for status, code in rows})


class RoutingTests(unittest.TestCase):
    def test_installed_dispatcher_hands_bind_and_wake_their_arguments(self):
        for command, target in (("bind", "bind_main"), ("wake", "wake_main")):
            for argv in (["operator", "--steer"], ["--steer", "operator"], []):
                with mock.patch(f"relay_runtime.wake.{target}", return_value=5) as helper:
                    code = runtime_cli.main(["--repo", "/srv/checkout", "--json", command, *argv])
                self.assertEqual(5, code)
                helper.assert_called_once_with([*argv, "--repo", "/srv/checkout", "--json"])

    def test_read_only_worker_profile_covers_exactly_the_wake_reads(self):
        parser = runtime_cli._parser()
        for argv, expected in ((["wake-ledger", "show"], True), (["wake-ledger", "observed", THREAD], True),
                               (["wake-ledger", "history", "operator"], True),
                               (["wake-ledger", "plan", "operator", "--ref", "1", "--requested", "queue"], True),
                               (["wake-ledger", "begin", "operator", "--ref", "1", "--requested", "queue"], False),
                               (["wake-ledger", "conclude", "3", "--outcome", "queued", "--reason", "requested"], False),
                               (["pause", "operator"], False)):
            self.assertEqual(expected, runtime_cli._readonly(parser.parse_args(argv)), argv)


if __name__ == "__main__":
    unittest.main()
