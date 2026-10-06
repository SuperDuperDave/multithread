"""Bounded role diagnostics against disposable ledgers and synthetic sockets."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_wake import WakeCase, THREAD, OTHER, LIVE_TURN, QUEUED_ID
from relay_runtime import cli as runtime_cli, wake

NEWER = "e0000000-0000-7000-8000-00000000000b"


def entry(queue_id=QUEUED_ID, client_id="synthetic-wake-original"):
    return {"id": queue_id, "clientUserMessageId": client_id,
            "input": [{"type": "text", "text": "PRIVATE SYNTHETIC INPUT\nλ"}]}


def page(entries=(), cursor=None):
    return {"data": list(entries), "nextCursor": cursor}


class ObserveTests(WakeCase):
    def setUp(self):
        super().setUp()
        self.generation = self.bound()
        self.daemon.requests.clear()
        self.ledger_calls.clear()
        self.pages = [page()]
        self.queue_calls = 0
        self.original_answer = self.daemon.answer
        patcher = mock.patch.object(self.daemon, "answer", side_effect=self.answer)
        patcher.start()
        self.addCleanup(patcher.stop)

    def answer(self, message):
        if message["method"] == "thread/queue/list":
            index = self.queue_calls
            self.queue_calls += 1
            response = self.pages[min(index, len(self.pages) - 1)]
            if response in (None, "traffic", "trickle", "close"):
                return response
            return {"id": message["id"], "result": response}
        return self.original_answer(message)

    def ledger(self, repo, *arguments, timeout=None):
        if timeout is not None:
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, wake._OBSERVE_TIMEOUT)
        return super().ledger(repo, *arguments)

    def observe(self, *extra, ledger=None):
        self.queue_calls = 0
        out = io.StringIO()
        with redirect_stdout(out):
            code = wake.observe_main(["operator", "--repo", str(self.repo), "--expect-generation",
                                      str(self.generation), "--expect-provider", "codex",
                                      "--expect-thread", THREAD, "--json", *extra], ledger=ledger or self.ledger)
        result = json.loads(out.getvalue())
        self.assertEqual(code, result["exit_code"])
        self.assertNotIn("PRIVATE SYNTHETIC INPUT", out.getvalue())
        self.assertNotIn("native_wake_hints", out.getvalue())
        return result

    def test_complete_empty_read_methods_and_no_ledger_mutation(self):
        before = self.events()
        result = self.observe("--queue-id", QUEUED_ID)
        self.assertEqual("OBSERVED", result["status"])
        self.assertTrue(result["usable"])
        self.assertTrue(result["queue"]["complete"])
        self.assertEqual([], result["queue"]["entries"])
        self.assertEqual("not_seen", result["original"]["standing"])
        self.assertEqual({"id": LIVE_TURN, "status": "completed"}, result["turns"]["latest"])
        self.assertEqual(["initialize", "thread/read", "thread/turns/list", "thread/queue/list"],
                         self.daemon.methods())
        init = self.daemon.requests[0]
        self.assertEqual({"experimentalApi": True}, init["params"]["capabilities"])
        self.assertEqual(False, self.daemon.requests[2]["params"]["includeTurns"])
        self.assertEqual(before, self.events())
        self.assertEqual([], self.codex_calls())
        self.assertEqual([("wake-ledger", "show", "operator")] * 2,
                         [arguments for _, arguments in self.ledger_calls])

    def test_human_output_exposes_metadata_without_raw_input(self):
        self.pages = [page([entry()])]
        code, output = self.run_helper(wake.observe_main, "operator", "--expect-generation",
                                       str(self.generation), "--expect-provider", "codex",
                                       "--expect-thread", THREAD, "--queue-id", QUEUED_ID)
        self.assertEqual(0, code)
        self.assertIn(QUEUED_ID, output)
        self.assertIn("clientUserMessageId", output)
        self.assertIn(LIVE_TURN, output)
        self.assertIn("canonical input", output)
        self.assertNotIn("PRIVATE SYNTHETIC INPUT", output)

    def test_older_nominated_original_is_independent_of_latest_attempt(self):
        self.pages = [page([entry(NEWER, "latest")], "second"), page([entry()])]
        result = self.observe("--queue-id", QUEUED_ID)
        self.assertEqual("observed", result["original"]["standing"])
        self.assertEqual(2, result["queue"]["pages"])
        self.assertEqual("second", [r for r in self.daemon.requests
                                    if r["method"] == "thread/queue/list"][1]["params"]["cursor"])
        old = result["queue"]["entries"][1]
        body = json.dumps(entry()["input"], ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode()
        self.assertEqual(QUEUED_ID, old["queue_id"])
        self.assertEqual("synthetic-wake-original", old["client_user_message_id"])
        self.assertEqual(len(body), old["input_bytes"])
        self.assertEqual(hashlib.sha256(body).hexdigest(), old["input_sha256"])
        self.assertIsNone(self.observe()["original"]["queue_id"])
        self.assertEqual("unknown", self.observe()["original"]["standing"])

    def test_guard_fails_before_contact(self):
        for changes in ({"generation": self.generation + 1}, {"provider": "claude"},
                        {"thread": OTHER}, {"state": "paused"}, {"state": "unbound"}):
            with self.subTest(changes=changes):
                def changed(repo, *args, timeout=None):
                    code, reply, problem = self.ledger(repo, *args, timeout=timeout)
                    reply["bindings"][0].update(changes)
                    return code, reply, problem
                result = self.observe(ledger=changed)
                self.assertEqual("STALE", result["status"])
                self.assertEqual([], self.daemon.methods())
        for reply in (None, {}, {"bindings": []}, {"bindings": [{"role": "operator", "state": "active"}]}):
            with self.subTest(reply=reply):
                result = self.observe(ledger=lambda *a, **k: (0, reply, ""))
                self.assertEqual("UNAVAILABLE", result["status"])
                self.assertEqual([], self.daemon.methods())

    def test_guard_fails_after_contact_and_retains_partial_evidence(self):
        self.pages = [page([entry()])]
        for changes in ({"generation": self.generation + 1}, {"thread": OTHER},
                        {"state": "paused"}, {"provider": "claude"},
                        {"endpoint": "unix:///synthetic/changed.sock"}):
            calls = []
            def changed(repo, *args, timeout=None):
                code, reply, problem = self.ledger(repo, *args, timeout=timeout)
                calls.append(1)
                if len(calls) == 2:
                    reply["bindings"][0].update(changes)
                return code, reply, problem
            with self.subTest(changes=changes):
                result = self.observe("--queue-id", QUEUED_ID, ledger=changed)
                self.assertEqual("STALE", result["status"])
                self.assertFalse(result["usable"])
                self.assertEqual("unknown", result["original"]["standing"])
                self.assertTrue(result["original"]["seen_in_scan"])
                self.assertEqual(1, len(result["queue"]["entries"]))
        calls = []
        def unavailable(repo, *args, timeout=None):
            calls.append(1)
            return self.ledger(repo, *args, timeout=timeout) if len(calls) == 1 else (74, None, "private error")
        result = self.observe("--queue-id", QUEUED_ID, ledger=unavailable)
        self.assertEqual("UNAVAILABLE", result["status"])
        self.assertIsNotNone(result["runtime"])
        self.assertNotIn("private error", json.dumps(result))

    def test_latest_attempt_changes_do_not_move_target(self):
        calls = []
        def changing_attempt(repo, *args, timeout=None):
            code, reply, problem = self.ledger(repo, *args, timeout=timeout)
            calls.append(1)
            reply["bindings"][0]["last_attempt"] = {"native_id": NEWER if len(calls) == 2 else QUEUED_ID}
            return code, reply, problem
        self.assertEqual("OBSERVED", self.observe(ledger=changing_attempt)["status"])

    def test_bounded_scan_is_partial_and_unknown_without_match(self):
        self.pages = [page([], "one"), page([], "two"), page([], "three"), page([entry()])]
        result = self.observe("--queue-id", QUEUED_ID)
        self.assertEqual("PARTIAL", result["status"])
        self.assertEqual(3, self.queue_calls)
        self.assertFalse(result["queue"]["complete"])
        self.assertEqual("partial", result["queue"]["scan_status"])
        self.assertEqual("unknown", result["original"]["standing"])
        self.pages[0] = page([entry()], "one")
        self.assertEqual("observed", self.observe("--queue-id", QUEUED_ID)["original"]["standing"])

    def test_complete_scan_accepts_exact_page_and_total_entry_bounds(self):
        entries = [entry(f"f0000000-0000-7000-8000-{index:012x}") for index in range(60)]
        self.pages = [page(entries[:20], "one"), page(entries[20:40], "two"), page(entries[40:])]
        result = self.observe("--queue-id", entries[-1]["id"])
        self.assertEqual("OBSERVED", result["status"])
        self.assertEqual(60, len(result["queue"]["entries"]))
        self.assertEqual("observed", result["original"]["standing"])
        self.assertEqual("json-utf8-sort-keys-compact-unescaped-unicode-no-nan-v1",
                         result["input_digest_profile"])

    def test_missing_or_malformed_queue_never_becomes_complete_empty(self):
        faults = [None, {}, {"data": []}, {"nextCursor": None}, page(cursor=""), page(cursor=7),
                  page(cursor="x" * 1025), page(cursor="bad\n"), page([entry()] * 21),
                  page([{}]), page([{**entry(), "id": OTHER + "x"}]),
                  page([{**entry(), "input": None}]), page([{**entry(), "input": ["raw"]}]),
                  page([{**entry(), "input": [{"value": float("nan")}]}]),
                  page([{**entry(), "clientUserMessageId": 3}]),
                  page([{**entry(), "clientUserMessageId": None}]),
                  page([{key: value for key, value in entry().items() if key != "clientUserMessageId"}])]
        with mock.patch.object(wake, "_OBSERVE_RPC_TIMEOUT", .05):
            for fault in faults:
                with self.subTest(fault=fault):
                    self.pages = [fault]
                    result = self.observe("--queue-id", QUEUED_ID)
                    self.assertEqual("PARTIAL", result["status"])
                    self.assertFalse(result["queue"]["complete"])
                    self.assertEqual("unavailable", result["queue"]["scan_status"])
                    self.assertEqual("unknown", result["original"]["standing"])

    def test_cursor_duplicate_and_later_page_fault_retain_valid_page(self):
        for later in (page([], "first"), page([entry()]), {}, page([entry(NEWER), entry(NEWER)])):
            with self.subTest(later=later):
                self.pages = [page([entry()], "first"), later]
                result = self.observe("--queue-id", QUEUED_ID)
                self.assertEqual("PARTIAL", result["status"])
                self.assertEqual(1, result["queue"]["pages"])
                self.assertEqual(1, len(result["queue"]["entries"]))
                self.assertFalse(result["queue"]["complete"])
                self.assertEqual("observed", result["original"]["standing"])

    def test_not_loaded_waits_and_interrupted_are_observations(self):
        thread = self.daemon.threads[THREAD]
        thread["turns"][0]["status"] = "interrupted"
        for status, flags in (("notLoaded", []), ("active", ["waitingOnApproval"]),
                              ("active", ["waitingOnUserInput"]), ("systemError", [])):
            thread.update(status=status, active_flags=flags)
            result = self.observe()
            self.assertEqual("OBSERVED", result["status"])
            self.assertEqual(status, result["runtime"]["status"])
            self.assertEqual(flags, result["runtime"]["active_flags"])
            self.assertEqual("interrupted", result["turns"]["latest"]["status"])
            self.assertNotIn("eligible", json.dumps(result))

    def test_strict_runtime_and_latest_turn_schemas(self):
        thread = self.daemon.threads[THREAD]
        for malformed in ({"thread": {"id": OTHER, "status": {"type": "idle"}}},
                          {"thread": {"id": THREAD, "status": {"type": "active"}}},
                          {"thread": {"id": THREAD, "status": {"type": "active", "activeFlags": ["unknown"]}}},
                          {"thread": {"id": THREAD, "status": {"type": "newStatus"}}}):
            with self.subTest(runtime=malformed):
                thread["read_reply"] = {"result": malformed}
                self.assertIsNone(self.observe()["runtime"])
        del thread["read_reply"]
        for malformed in ({}, {"data": [{}]}, {"data": [thread["turns"][0]] * 2},
                          {"data": [{"id": LIVE_TURN, "status": "unknown"}]},
                          {"data": [{"id": "", "status": "completed"}]},
                          {"data": [{"id": "bad\n", "status": "completed"}]},
                          {"data": [{"id": "x" * 1025, "status": "completed"}]}):
            with self.subTest(turns=malformed):
                thread["list_reply"] = {"result": malformed}
                result = self.observe()
                self.assertEqual("PARTIAL", result["status"])
                self.assertIsNone(result["turns"])
        thread["list_reply"] = {"result": {"data": []}}
        self.assertIsNone(self.observe()["turns"]["latest"])

    def test_numeric_legacy_turn_id_is_observed_as_its_actual_string(self):
        self.daemon.threads[THREAD]["turns"][0].update(id="7", status="interrupted")
        result = self.observe()
        self.assertEqual("OBSERVED", result["status"])
        self.assertEqual({"id": "7", "status": "interrupted"}, result["turns"]["latest"])

    def test_native_unavailable_and_partial_are_distinct(self):
        self.daemon.read_mode = "close"
        result = self.observe()
        self.assertEqual("UNAVAILABLE", result["status"])
        self.assertEqual("matched", result["binding_guard"]["after"])
        self.daemon.read_mode = "accept"
        self.pages = ["close"]
        self.assertEqual("PARTIAL", self.observe()["status"])

    def test_total_deadline_covers_traffic_fragments_and_final_guard(self):
        with mock.patch.object(wake, "_OBSERVE_TIMEOUT", .12):
            for traffic in ("traffic", "trickle"):
                with self.subTest(traffic=traffic):
                    self.daemon.read_mode = traffic
                    start = time.monotonic()
                    result = self.observe()
                    self.assertLess(time.monotonic() - start, .35)
                    self.assertEqual("UNAVAILABLE", result["status"])
                    self.assertEqual("unavailable", result["binding_guard"]["after"])
        self.daemon.read_mode = "accept"
        calls = []
        def slow_guard(repo, *args, timeout=None):
            calls.append(timeout)
            if len(calls) == 2:
                time.sleep(timeout)
                return None, None, "bounded timeout"
            return self.ledger(repo, *args, timeout=timeout)
        with mock.patch.object(wake, "_OBSERVE_TIMEOUT", .15):
            start = time.monotonic()
            self.assertEqual("UNAVAILABLE", self.observe(ledger=slow_guard)["status"])
            self.assertLess(time.monotonic() - start, .35)
            self.assertEqual(2, len(calls))
            self.assertLess(calls[1], calls[0])

    def test_fragment_aggregate_and_oversized_frame_bounds(self):
        normal_send = self.daemon.send
        for oversized in (False, True):
            def hostile_send(connection, value, fragmented=False):
                if isinstance(value, dict) and isinstance(value.get("result"), dict) and "data" in value["result"]:
                    if value["result"]["data"] and "input" in value["result"]["data"][0]:
                        if oversized:
                            connection.sendall(bytes([0x81, 127]) + struct.pack(">Q", wake._MAX_MESSAGE + 1))
                        else:
                            for i in range(18):
                                connection.sendall(bytes([1 if i == 0 else 0, 126])
                                                   + struct.pack(">H", 65535) + b"x" * 65535)
                        return
                normal_send(connection, value, fragmented=fragmented)
            self.pages = [page([entry()])]
            with self.subTest(oversized=oversized), mock.patch.object(self.daemon, "send", side_effect=hostile_send):
                result = self.observe()
                self.assertEqual("PARTIAL", result["status"])
                self.assertEqual("unavailable", result["queue"]["scan_status"])

    def test_cli_rejects_incomplete_or_nonpositive_expectation(self):
        for argv in (["operator"], ["operator", "--expect-generation", "0", "--expect-provider", "codex",
                                   "--expect-thread", THREAD],
                     ["operator", "--expect-generation", "1", "--expect-provider", "claude",
                      "--expect-thread", THREAD]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                wake.observe_main(argv, ledger=self.ledger)
            self.assertEqual(2, exc.exception.code)
        self.assertEqual([], self.daemon.methods())


class ObserveDispatchTests(unittest.TestCase):
    def test_installed_dispatcher_forwards_global_flags_without_admission(self):
        arguments = ["operator", "--expect-generation", "7", "--expect-provider", "codex",
                     "--expect-thread", THREAD, "--queue-id", QUEUED_ID]
        registry = mock.Mock()
        registry.enroll.side_effect = AssertionError("observer dispatcher must not enroll")
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(wake, "observe_main", return_value=3) as observer, \
             mock.patch.object(wake, "launcher_ledger", side_effect=AssertionError("dispatcher ledger contact")), \
             mock.patch.object(runtime_cli, "Admission", side_effect=AssertionError("dispatcher admission")) as admission, \
             mock.patch.object(runtime_cli.Registry, "for_account", side_effect=AssertionError("registry contact")), \
             mock.patch.object(runtime_cli.RelayStore, "open_readonly", side_effect=AssertionError("ledger open")), \
             mock.patch.object(runtime_cli.RelayStore, "open", side_effect=AssertionError("ledger open")):
            code = runtime_cli.main(["--json", "--repo", "/srv/synthetic-checkout", "observe", *arguments],
                                    registry=registry)
        self.assertEqual(3, code)
        observer.assert_called_once_with([*arguments, "--repo", "/srv/synthetic-checkout", "--json"])
        admission.assert_not_called()
        self.assertEqual([], registry.mock_calls)

    def test_installed_dispatcher_refuses_state_overrides_before_observer(self):
        arguments = ["observe", "operator", "--expect-generation", "7", "--expect-provider", "codex",
                     "--expect-thread", THREAD]
        for prefix, environment in ((["--home", "/srv/synthetic-state"], {}),
                                    ([], {"RELAY_HOME": "/srv/synthetic-state"})):
            registry = mock.Mock()
            registry.enroll.side_effect = AssertionError("state override must not enroll")
            err = io.StringIO()
            with self.subTest(prefix=prefix, environment=environment), \
                 mock.patch.dict(os.environ, environment, clear=True), redirect_stderr(err), \
                 mock.patch.object(wake, "observe_main", side_effect=AssertionError("override reached observer")) as observer, \
                 mock.patch.object(runtime_cli, "Admission", side_effect=AssertionError("override admission")), \
                 mock.patch.object(runtime_cli.Registry, "for_account", side_effect=AssertionError("registry contact")), \
                 mock.patch.object(wake, "launcher_ledger", side_effect=AssertionError("override ledger contact")):
                code = runtime_cli.main([*prefix, *arguments], registry=registry)
            self.assertNotEqual(0, code)
            self.assertIn("refuses state-directory overrides", err.getvalue())
            observer.assert_not_called()
            self.assertEqual([], registry.mock_calls)


class HandshakeBoundsTests(unittest.TestCase):
    def test_handshake_trickle_cannot_extend_total_deadline(self):
        with tempfile.TemporaryDirectory(prefix="observe-handshake-", dir="/tmp") as temporary:
            path = Path(temporary) / "daemon.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(1)
            self.addCleanup(server.close)
            def trickle():
                connection, _ = server.accept()
                with connection:
                    connection.recv(4096)
                    try:
                        for _ in range(100):
                            connection.sendall(b"H")
                            time.sleep(.01)
                    except OSError:
                        pass
            worker = threading.Thread(target=trickle, daemon=True)
            worker.start()
            start = time.monotonic()
            with self.assertRaises(wake.DaemonUnavailable):
                wake.Daemon(path, timeout=.3, experimental_api=True, overall_deadline=start + .08)
            self.assertLess(time.monotonic() - start, .2)
            worker.join(.2)

    def test_launcher_ledger_preserves_default_and_accepts_remaining_bound(self):
        completed = mock.Mock(returncode=0, stdout="{}")
        with mock.patch.object(wake.subprocess, "run", return_value=completed) as run:
            wake.launcher_ledger("/synthetic", "wake-ledger", "show", "role", timeout=.125)
            self.assertEqual(.125, run.call_args.kwargs["timeout"])
            wake.launcher_ledger("/synthetic", "wake-ledger", "show", "role")
            self.assertEqual(wake._LEDGER_TIMEOUT, run.call_args.kwargs["timeout"])


if __name__ == "__main__":
    unittest.main()
