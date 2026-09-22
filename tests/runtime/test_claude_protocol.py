"""Claude streaming peer contracts using disposable JSONL executables only."""

import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import claude_peer, native_io, provider

SESSION = "10000000-0000-4000-8000-000000000001"
OTHER_SESSION = "20000000-0000-4000-8000-000000000002"
ANSWER = "Scoped final answer 雪"
LATER = "Second scoped answer"
_SET_PIPE_SIZE = 1031


NATIVE = r'''
import json, os, sys, time
from pathlib import Path
spec = json.loads(Path(SPEC_PATH).read_text())
received = []
Path(RECEIPT_PATH).write_text(json.dumps({'argv': sys.argv, 'cwd': os.getcwd()}))

def resolve(value):
    if isinstance(value, dict):
        return {key: resolve(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item) for item in value]
    if isinstance(value, str) and value.startswith('$uuid:'):
        return received[int(value[6:])].get('uuid')
    if value == '$cwd':
        return os.getcwd()
    return value

def emit(value):
    sys.stdout.write(json.dumps(resolve(value), ensure_ascii=False) + '\n')
    sys.stdout.flush()

for step in spec:
    if 'read' in step:
        for _ in range(step['read']):
            line = sys.stdin.buffer.readline()
            if not line:
                raise SystemExit(0)
            received.append(json.loads(line))
            with open(REQUESTS_PATH, 'a') as stream:
                stream.write(line.decode('utf-8'))
    elif 'emit' in step:
        emit(step['emit'])
    elif 'raw' in step:
        sys.stdout.buffer.write(bytes.fromhex(step['raw']))
        sys.stdout.buffer.flush()
    elif 'sleep' in step:
        time.sleep(step['sleep'])
    elif 'exit' in step:
        raise SystemExit(step['exit'])
raise SystemExit(0)
'''


def init(session=SESSION, cwd="$cwd", **extra):
    frame = {"type": "system", "subtype": "init", "uuid": str(uuid.uuid4()), "session_id": session,
             "cwd": cwd, "model": "fixture-effective-model", "claude_code_version": "2.1.269",
             "permissionMode": "default", "tools": [], "mcp_servers": [], "slash_commands": [],
             "output_style": "default", "skills": [], "plugins": [], "apiKeySource": "none"}
    frame.update(extra)
    return frame


def answered(uuids):
    return {} if uuids is None else {"user_message_uuid": uuids[-1], "user_message_uuids": list(uuids)}


def assistant(text, *, uuids=None, session=SESSION, parent=None, thinking=None):
    content = ([{"type": "thinking", "thinking": thinking}] if thinking else []) + [{"type": "text", "text": text}]
    return {"type": "assistant", "uuid": str(uuid.uuid4()), "session_id": session,
            "parent_tool_use_id": parent, "message": {"id": "msg_" + text[:8], "role": "assistant",
                                                      "content": content, "model": "fixture"},
            **answered(uuids)}


def result(text=ANSWER, *, uuids=("$uuid:0",), subtype="success", is_error=False,
           session=SESSION, errors=None, denials=(), **extra):
    frame = {"type": "result", "subtype": subtype, "uuid": str(uuid.uuid4()), "session_id": session,
             "duration_ms": 12, "duration_api_ms": 8, "is_error": is_error, "num_turns": 1,
             "stop_reason": "end_turn", "terminal_reason": "completed", "total_cost_usd": 0.5,
             "usage": {"input_tokens": 3, "output_tokens": 4}, "modelUsage": {},
             "permission_denials": list(denials), **answered(uuids)}
    if subtype == "success":
        frame["result"] = text
    else:
        frame["errors"] = list(errors or ["fixture native failure"])
    frame.update(extra)
    return frame


def replay(index=1, session=SESSION):
    return {"type": "user", "uuid": "$uuid:" + str(index), "session_id": session,
            "parent_tool_use_id": None, "isReplay": True,
            "message": {"role": "user", "content": "echoed"}}


class Control:
    """The mailbox contract root exposes, without its durable file mailbox."""

    def __init__(self, *requests, batch=8):
        self.queue = list(requests)
        self.batch = batch
        self.targets = []
        self.receipts = {}
        self.dispatched = []
        self.stopped = None
        self.accepting = True

    def set_target(self, session_id, turn_id):
        if not self.accepting:
            raise AssertionError("target advertised after closure")
        self.targets.append((session_id, turn_id))

    def pending(self):
        if not self.accepting:
            return []
        selected, self.queue = self.queue[:self.batch], self.queue[self.batch:]
        self.dispatched.extend(item["request_id"] for item in selected)
        return selected

    def resolve(self, request_id, state, detail):
        if request_id in self.receipts:
            raise AssertionError("an immutable final receipt was rewritten")
        self.receipts[request_id] = state

    def stop_accepting(self, reason):
        self.accepting = False
        self.stopped = reason


def request(text="Between-tool update", session=SESSION, turn=None, identifier=None):
    return {"request_id": identifier or str(uuid.uuid4()), "text": text,
            "session_id": session, "turn_id": turn}


class ClaudeProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-claude-protocol-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "checkout 雪 ;$(touch injected)"
        self.repo.mkdir()
        self.provider = self.base / "claude entry"
        self.specification = self.base / "native-spec.json"
        self.receipt = self.base / "native-receipt.json"
        self.requests = self.base / "requests.jsonl"
        settings = {"SPEC_PATH": str(self.specification), "RECEIPT_PATH": str(self.receipt),
                    "REQUESTS_PATH": str(self.requests)}
        source = "".join(f"{key} = {value!r}\n" for key, value in settings.items()) + NATIVE
        self.provider.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        self.provider.chmod(0o700)
        self.task = "Review 'quoted' 雪.\n$(touch injected)\n"
        self.count = 0
        self.processes = []
        self.addCleanup(self.reap)

    def reap(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdout, process.stdin):
                if stream is not None and not stream.closed:
                    stream.close()

    def run_native(self, steps, *, task=None, control=None, resume=None, session=SESSION,
                   timeout=5, pipe_size=None, feedback=None):
        self.count += 1
        self.specification.write_text(json.dumps(steps))
        self.requests.unlink(missing_ok=True)
        directory = self.base / f"evidence-{self.count}"
        directory.mkdir(mode=0o700)
        errors = (directory / "stderr.txt").open("wb")
        self.addCleanup(errors.close)
        process = subprocess.Popen([str(self.provider)], cwd=str(self.repo), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=errors, start_new_session=True)
        self.processes.append(process)
        if pipe_size is not None:
            fcntl.fcntl(process.stdin.fileno(), _SET_PIPE_SIZE, pipe_size)
        envelope = {"schema": 1, "provider": "claude", "state": "unavailable",
                    "requested_session_id": session, "session_id": None, "result": None,
                    "needs_attention": True, "usage": None}
        body = (task if task is not None else self.task).encode("utf-8")
        claude_peer.run(process, body, str(self.repo), resume, directory, envelope,
                        timeout, control=control, feedback=feedback)
        return envelope, directory, process

    def submitted(self):
        return [json.loads(line) for line in self.requests.read_text().splitlines()]

    def assert_raw(self, envelope, directory):
        raw = (directory / "stdout.json").read_bytes()
        self.assertEqual({"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                          "truncated": envelope["stdout_observation"]["truncated"]},
                         envelope["stdout_observation"])
        self.assertEqual(0o600, (directory / "stdout.json").stat().st_mode & 0o777)
        json.dumps(envelope, ensure_ascii=True, sort_keys=True)
        return raw

    # --- ordinary return -------------------------------------------------

    def test_waiting_callback_runs_during_native_silence_without_resubmitting_task(self):
        feedback = mock.Mock()
        envelope, directory, _ = self.run_native(
            [{"read": 1}, {"sleep": 0.12}, {"emit": init()},
             {"sleep": 0.12}, {"emit": result()}], feedback=feedback)
        self.assertEqual("returned", envelope["state"])
        self.assertEqual(ANSWER, envelope["result"])
        self.assertFalse(envelope["needs_attention"])
        self.assertGreaterEqual(feedback.call_count, 3)
        self.assertTrue(all(call == mock.call() for call in feedback.call_args_list))
        submitted = self.submitted()
        self.assertEqual(1, len(submitted))
        self.assertEqual(self.task, submitted[0]["message"]["content"])
        self.assert_raw(envelope, directory)

    def test_waiting_feedback_follows_actual_initial_pipe_write(self):
        envelope = {"state": "uncertain", "provider": "claude",
                    "requested_session_id": SESSION}
        driver = claude_peer._Driver(mock.Mock(), b"ARTIFICIAL-PRIVATE-TASK", str(self.repo),
                                     None, envelope, 600, None)
        driver.start()
        self.assertEqual("in_progress", envelope["task_delivery"])
        size = driver.marks[0][0]
        feedback = provider._WaitingFeedback(0, 600, envelope, None)
        stderr = io.StringIO()
        with (redirect_stderr(stderr),
              mock.patch.object(provider.time, "monotonic", side_effect=[30, 60, 90])):
            feedback()
            driver.wrote(size - 1)
            feedback()
            driver.wrote(1)
            feedback()
        lines = stderr.getvalue().splitlines()
        self.assertEqual(3, len(lines))
        self.assertTrue(all("writing task to provider stdin" in line for line in lines[:2]))
        self.assertIn("task written to provider stdin; waiting for result", lines[2])
        self.assertNotIn("ARTIFICIAL-PRIVATE", stderr.getvalue())
        self.assertEqual("written", envelope["task_delivery"])

    def test_initial_task_returns_the_exact_final_answer_with_native_identity(self):
        control = Control()
        envelope, directory, _ = self.run_native(
            [{"read": 1}, {"emit": init()}, {"emit": assistant("Working on it", uuids=["$uuid:0"])},
             {"emit": result()}], control=control)
        self.assertEqual("returned", envelope["state"])
        self.assertEqual(ANSWER, envelope["result"])
        self.assertEqual(SESSION, envelope["session_id"])
        self.assertFalse(envelope["needs_attention"])
        self.assertEqual("fixture-effective-model", envelope["native_model"])
        self.assertEqual("2.1.269", envelope["native_version"])
        self.assertEqual({"status": "reported", "version": "2.1.269",
                          "source": "claude_system_init"}, envelope["provider_version"])
        self.assertEqual("default", envelope["native_permission_mode"])
        self.assertEqual("written", envelope["task_delivery"])
        self.assertEqual([(SESSION, None)], control.targets)
        submitted = self.submitted()
        self.assertEqual(1, len(submitted))
        frame = submitted[0]
        self.assertEqual("user", frame["type"])
        self.assertEqual(envelope["initial_message_uuid"], frame["uuid"])
        self.assertEqual(SESSION, frame["session_id"])
        self.assertIsNone(frame["parent_tool_use_id"])
        self.assertEqual({"role": "user", "content": self.task}, frame["message"])
        record = envelope["native_input"][0]
        self.assertEqual({"uuid": frame["uuid"], "kind": "task", "bytes": record["bytes"],
                          "written": True, "echoed": False, "consumption": "consumed",
                          "result_covered": True}, record)
        self.assertEqual(1, len(envelope["native_results"]))
        self.assertEqual("completed", envelope["terminal_reason"])
        self.assertEqual(0.5, envelope["estimated_cost_usd"])
        self.assertNotIn("partial_result", envelope)
        self.assertFalse((self.repo / "injected").exists())
        self.assert_raw(envelope, directory)

    def test_resumed_session_uses_the_exact_supplied_identity(self):
        envelope, _, _ = self.run_native([{"read": 1}, {"emit": init()}, {"emit": result()}],
                                         resume=SESSION)
        self.assertEqual("returned", envelope["state"])
        self.assertEqual(SESSION, envelope["session_id"])
        self.assertEqual(SESSION, self.submitted()[0]["session_id"])

    def test_invalid_optional_measurements_preserve_the_attributed_answer_and_consumption(self):
        metrics = {"num_turns": ("provider_turns", 1),
                   "duration_ms": ("provider_duration_ms", 12),
                   "total_cost_usd": ("estimated_cost_usd", 0.5)}
        for native_field, (field, _) in metrics.items():
            for invalid in (-1, True, "12", "ARTIFICIAL-PRIVATE-MEASUREMENT"):
                with self.subTest(field=field, invalid=invalid):
                    control = Control()
                    envelope, directory, _ = self.run_native(
                        [{"read": 1}, {"emit": init()}, {"emit": result(**{native_field: invalid})}],
                        control=control)
                    self.assertEqual("returned", envelope["state"])
                    self.assertEqual(ANSWER, envelope["result"])
                    self.assertEqual(SESSION, envelope["session_id"])
                    self.assertFalse(envelope["needs_attention"])
                    self.assertIsNone(envelope[field])
                    self.assertEqual([field], envelope["measurement_errors"])
                    self.assertEqual("consumed", envelope["native_input"][0]["consumption"])
                    self.assertEqual([(SESSION, None)], control.targets)
                    self.assertEqual(1, len(self.submitted()))
                    for other, expected in metrics.values():
                        if other != field:
                            self.assertEqual(expected, envelope[other])
                    self.assert_raw(envelope, directory)

    def test_bad_usage_and_model_counters_keep_independent_metrics_and_permission_denials(self):
        private = "ARTIFICIAL-PRIVATE-MODEL-MEASUREMENT"
        envelope, directory, _ = self.run_native(
            [{"read": 1}, {"emit": init()}, {"emit": result(
                usage={"input_tokens": -1, "output_tokens": 4,
                       "server_tool_use": {"web_search_requests": True, "web_fetch_requests": 0},
                       "output_tokens_details": {"thinking_tokens": private}},
                modelUsage={private: {"inputTokens": private, "outputTokens": 7, "costUSD": False}},
                denials=[{"tool_name": "Write", "tool_use_id": "fixture-tool", "tool_input": private}])}])
        self.assertEqual("returned", envelope["state"])
        self.assertEqual(ANSWER, envelope["result"])
        self.assertTrue(envelope["needs_attention"])
        self.assertEqual([{"tool_name": "Write", "tool_use_id": "fixture-tool"}], envelope["permission_denials"])
        self.assertEqual({"input_tokens": None, "output_tokens": 4,
                          "server_tool_use": {"web_search_requests": None, "web_fetch_requests": 0},
                          "output_tokens_details": {"thinking_tokens": None}}, envelope["usage"])
        self.assertEqual({"inputTokens": None, "outputTokens": 7, "costUSD": None},
                         envelope["model_usage"][private])
        self.assertEqual({"usage.input_tokens", "usage.server_tool_use.web_search_requests",
                          "usage.output_tokens_details.thinking_tokens", "model_usage.model.inputTokens",
                          "model_usage.model.costUSD"}, set(envelope["measurement_errors"]))
        self.assertNotIn(private, json.dumps(envelope["measurement_errors"]))
        self.assertEqual(12, envelope["provider_duration_ms"])
        self.assertEqual(0.5, envelope["estimated_cost_usd"])
        self.assertIn(private.encode(), self.assert_raw(envelope, directory))

    def test_absent_or_null_optional_measurements_remain_unknown_without_an_error(self):
        for omitted in (False, True):
            with self.subTest(omitted=omitted):
                final = result()
                for field in ("num_turns", "duration_ms", "total_cost_usd", "usage", "modelUsage"):
                    if omitted:
                        final.pop(field)
                    else:
                        final[field] = None
                envelope, _, _ = self.run_native([{"read": 1}, {"emit": init()}, {"emit": final}])
                self.assertEqual("returned", envelope["state"])
                self.assertEqual(ANSWER, envelope["result"])
                self.assertFalse(envelope["needs_attention"])
                for field in ("provider_turns", "provider_duration_ms", "estimated_cost_usd", "usage", "model_usage"):
                    self.assertIsNone(envelope[field])
                self.assertFalse(envelope.get("measurement_errors"))

    def test_optional_container_failures_have_bounded_paths_without_model_names(self):
        private = "ARTIFICIAL-PRIVATE-MODEL-"
        models = {private + str(index): {"inputTokens": "invalid", "outputTokens": 7}
                  for index in range(50)}
        models[private + "malformed"] = "invalid container"
        envelope, _, _ = self.run_native([
            {"read": 1}, {"emit": init()}, {"emit": result(usage="invalid container", modelUsage=models)}])
        self.assertEqual("returned", envelope["state"])
        self.assertEqual(ANSWER, envelope["result"])
        self.assertFalse(envelope["needs_attention"])
        self.assertIsNone(envelope["usage"])
        self.assertEqual({"usage", "model_usage.model", "model_usage.model.inputTokens"},
                         set(envelope["measurement_errors"]))
        self.assertEqual(3, len(envelope["measurement_errors"]))
        self.assertLessEqual(len(envelope["measurement_errors"]), 32)
        self.assertNotIn(private, json.dumps(envelope["measurement_errors"]))

    def test_related_usage_and_later_cumulative_totals_keep_distinct_stable_scopes(self):
        envelope, _, _ = self.run_native([
            {"read": 1}, {"emit": init()},
            {"emit": result(total_cost_usd=0.25, modelUsage={"fixture": {"outputTokens": 4}})},
            {"emit": result(text="Unrelated background notice", uuids=["unrelated-message"],
                             usage={"input_tokens": 900, "output_tokens": 900}, total_cost_usd=0.4,
                             modelUsage={"fixture": {"outputTokens": 10}})}])
        self.assertEqual("returned", envelope["state"])
        self.assertEqual(ANSWER, envelope["result"])
        self.assertFalse(envelope["needs_attention"])
        self.assertEqual({"input_tokens": 3, "output_tokens": 4}, envelope["usage"])
        self.assertEqual({"fixture": {"outputTokens": 10}}, envelope["model_usage"])
        self.assertEqual(0.4, envelope["estimated_cost_usd"])
        self.assertEqual("latest_related_native_result", envelope["usage_scope_id"])
        self.assertEqual("native_query_cumulative", envelope["model_usage_scope_id"])
        self.assertEqual("cumulative_through_latest_native_result", envelope["cost_scope_id"])

    def test_later_valid_related_measurements_clear_current_warnings_but_preserve_history(self):
        envelope, directory, _ = self.run_native([
            {"read": 1}, {"emit": init()},
            {"emit": result(num_turns=-1, duration_ms=True, total_cost_usd="12",
                             usage={"input_tokens": -1, "output_tokens": 4},
                             modelUsage={"fixture": {"outputTokens": True}})},
            {"emit": result(text=LATER, num_turns=2, duration_ms=24, total_cost_usd=0.75,
                             usage={"input_tokens": 6, "output_tokens": 8},
                             modelUsage={"fixture": {"outputTokens": 12}})}])
        self.assertEqual("returned", envelope["state"])
        self.assertEqual(LATER, envelope["result"])
        self.assertFalse(envelope["needs_attention"])
        self.assertEqual(2, envelope["provider_turns"])
        self.assertEqual(24, envelope["provider_duration_ms"])
        self.assertEqual(0.75, envelope["estimated_cost_usd"])
        self.assertEqual({"input_tokens": 6, "output_tokens": 8}, envelope["usage"])
        self.assertEqual({"fixture": {"outputTokens": 12}}, envelope["model_usage"])
        self.assertFalse(envelope.get("measurement_errors"))
        earlier, latest = envelope["native_results"]
        self.assertEqual({"provider_turns", "provider_duration_ms", "estimated_cost_usd",
                          "usage.input_tokens", "model_usage.model.outputTokens"},
                         set(earlier["measurement_errors"]))
        self.assertIsNone(earlier["num_turns"])
        self.assertIsNone(earlier["usage"]["input_tokens"])
        self.assertFalse(latest.get("measurement_errors"))
        self.assertEqual("consumed", envelope["native_input"][0]["consumption"])
        self.assert_raw(envelope, directory)

    def test_unrelated_bad_loop_metrics_do_not_warn_about_retained_related_measurements(self):
        envelope, _, _ = self.run_native([
            {"read": 1}, {"emit": init()}, {"emit": result()},
            {"emit": result(text="Background notice", uuids=["unrelated-message"],
                             num_turns=-1, duration_ms=True,
                             usage={"input_tokens": "12", "output_tokens": -1},
                             total_cost_usd=0.75, modelUsage={"fixture": {"outputTokens": 12}})}])
        self.assertEqual("returned", envelope["state"])
        self.assertEqual(ANSWER, envelope["result"])
        self.assertFalse(envelope["needs_attention"])
        self.assertEqual(1, envelope["provider_turns"])
        self.assertEqual(12, envelope["provider_duration_ms"])
        self.assertEqual({"input_tokens": 3, "output_tokens": 4}, envelope["usage"])
        self.assertEqual(0.75, envelope["estimated_cost_usd"])
        self.assertEqual({"fixture": {"outputTokens": 12}}, envelope["model_usage"])
        self.assertFalse(envelope.get("measurement_errors"))
        background = envelope["native_results"][-1]
        self.assertFalse(background["related"])
        self.assertEqual({"provider_turns", "provider_duration_ms", "usage.input_tokens",
                          "usage.output_tokens"}, set(background["measurement_errors"]))

    def test_repaired_cumulative_metrics_clear_only_their_warnings(self):
        envelope, _, _ = self.run_native([
            {"read": 1}, {"emit": init()},
            {"emit": result(num_turns=-1, duration_ms=True,
                             usage={"input_tokens": "12", "output_tokens": 4})},
            {"emit": result(text="Background notice", uuids=["unrelated-message"],
                             total_cost_usd=-1, modelUsage={"fixture": {"outputTokens": True}})},
            {"emit": result(text="Later background notice", uuids=["another-unrelated-message"],
                             total_cost_usd=0.75, modelUsage={"fixture": {"outputTokens": 12}})}])
        self.assertEqual("returned", envelope["state"])
        self.assertEqual(ANSWER, envelope["result"])
        self.assertFalse(envelope["needs_attention"])
        self.assertIsNone(envelope["provider_turns"])
        self.assertIsNone(envelope["provider_duration_ms"])
        self.assertEqual({"input_tokens": None, "output_tokens": 4}, envelope["usage"])
        self.assertEqual(0.75, envelope["estimated_cost_usd"])
        self.assertEqual({"fixture": {"outputTokens": 12}}, envelope["model_usage"])
        self.assertEqual({"provider_turns", "provider_duration_ms", "usage.input_tokens"},
                         set(envelope["measurement_errors"]))
        related, faulty, repaired = envelope["native_results"]
        self.assertEqual(set(envelope["measurement_errors"]), set(related["measurement_errors"]))
        self.assertEqual({"estimated_cost_usd", "model_usage.model.outputTokens"},
                         set(faulty["measurement_errors"]))
        self.assertFalse(repaired.get("measurement_errors"))

    def test_resume_initialization_after_background_notice_keeps_the_original_task(self):
        control = Control()
        envelope, _, _ = self.run_native([{'read': 1}, {'emit': init()},
            {'emit': result(text='', uuids=None, origin={'kind': 'task-notification'})},
            {'emit': init()}, {'emit': result()}], resume=SESSION, control=control)
        self.assertEqual('returned', envelope['state'])
        self.assertEqual(ANSWER, envelope['result'])
        self.assertFalse(envelope['needs_attention'])
        self.assertEqual(2, envelope['native_initialization_count'])
        self.assertEqual([(SESSION, None)], control.targets)
        self.assertEqual(1, len(self.submitted()))
        self.assertEqual('task-notification', envelope['native_results'][0]['origin_kind'])
        self.assertEqual('consumed', envelope['native_input'][0]['consumption'])

    def test_repeated_initialization_still_rejects_another_session_or_checkout(self):
        for changed in (init(session=OTHER_SESSION), init(cwd='/wrong-checkout')):
            with self.subTest(changed=changed):
                control = Control()
                envelope, _, _ = self.run_native([{'read': 1}, {'emit': init()},
                    {'emit': changed}, {'emit': result()}], control=control)
                self.assertEqual('uncertain', envelope['state'])
                self.assertIsNone(envelope['result'])
                self.assertTrue(envelope['needs_attention'])
                self.assertEqual([(SESSION, None)], control.targets)

    def test_optional_initialization_version_does_not_change_the_native_outcome(self):
        cases = [(None, "not_reported", None), ("0.0.0", "reported", "0.0.0"),
                 ("1.2.3-alpha.0", "reported", "1.2.3-alpha.0"),
                 ("1.2.3-beta.4", "reported", "1.2.3-beta.4"),
                 ("1.2.3-rc.5", "reported", "1.2.3-rc.5")]
        for value in (True, 123, {}, [], "", "01.2.3", "1.2.3-rc.01", "1.2.3-rc.1000000",
                      "1000000.2.3", "１.2.3", "1.2.3 (Claude Code)", "1.2.3+ARTIFICIAL-CANARY",
                      "1.2.3\nARTIFICIAL-CANARY", "ARTIFICIAL-CANARY" * 100):
            cases.append((value, "unrecognized", None))
        for value, status, version in cases:
            with self.subTest(value=value):
                envelope, _, _ = self.run_native([{"read": 1},
                    {"emit": init(claude_code_version=value)}, {"emit": result()}])
                self.assertEqual("returned", envelope["state"])
                self.assertEqual(ANSWER, envelope["result"])
                self.assertFalse(envelope["needs_attention"])
                self.assertEqual({"status": status, "version": version,
                                  "source": "claude_system_init"}, envelope["provider_version"])

    def test_repeated_initialization_replaces_version_including_missing_or_unrecognized(self):
        missing = init()
        missing.pop("claude_code_version")
        cases = [(missing, "not_reported", None),
                 (init(claude_code_version="ARTIFICIAL-CANARY"), "unrecognized", None),
                 (init(claude_code_version="3.4.5"), "reported", "3.4.5")]
        for latest, status, version in cases:
            with self.subTest(status=status):
                envelope, _, _ = self.run_native([{"read": 1}, {"emit": init()},
                    {"emit": latest}, {"emit": result()}])
                self.assertEqual("returned", envelope["state"])
                self.assertEqual(ANSWER, envelope["result"])
                self.assertFalse(envelope["needs_attention"])
                self.assertEqual(2, envelope["native_initialization_count"])
                self.assertEqual({"status": status, "version": version,
                                  "source": "claude_system_init"}, envelope["provider_version"])

    def test_subagent_initialization_cannot_replace_the_main_provider_version(self):
        envelope, _, _ = self.run_native([{"read": 1}, {"emit": init()},
            {"emit": init(claude_code_version="9.8.7", parent_tool_use_id="fixture-tool")},
            {"emit": result()}])
        self.assertEqual("returned", envelope["state"])
        self.assertFalse(envelope["needs_attention"])
        self.assertEqual(1, envelope["native_initialization_count"])
        self.assertEqual({"status": "reported", "version": "2.1.269",
                          "source": "claude_system_init"}, envelope["provider_version"])

    # --- identity and attribution ---------------------------------------

    def test_session_mismatch_returns_no_answer_and_advertises_no_target(self):
        control = Control()
        envelope, directory, _ = self.run_native(
            [{"read": 1}, {"emit": init(session=OTHER_SESSION)},
             {"emit": result(session=OTHER_SESSION)}], control=control)
        self.assertEqual("uncertain", envelope["state"])
        self.assertIsNone(envelope["result"])
        self.assertTrue(envelope["needs_attention"])
        self.assertEqual(OTHER_SESSION, envelope["observed_session_id"])
        self.assertIsNone(envelope["session_id"])
        self.assertNotIn("provider_version", envelope)
        self.assertEqual([], control.targets)
        self.assertIn(b"result", self.assert_raw(envelope, directory))

    def test_working_directory_mismatch_keeps_the_session_unexposed(self):
        control = Control()
        envelope, _, _ = self.run_native(
            [{"read": 1}, {"emit": init(cwd="/somewhere/else")}, {"emit": result()}], control=control)
        self.assertEqual("uncertain", envelope["state"])
        self.assertIsNone(envelope["result"])
        self.assertNotIn("provider_version", envelope)
        self.assertEqual([], control.targets)

    def test_subagent_frames_never_supply_main_text_result_or_consumption(self):
        control = Control()
        envelope, _, _ = self.run_native(
            [{"read": 1}, {"emit": init()},
             {"emit": assistant("Subagent reasoning", uuids=["$uuid:0"], parent="toolu_1")},
             {"emit": result(text="Subagent answer", parent_tool_use_id="toolu_1")},
             {"emit": assistant("Main progress", thinking="private reasoning")},
             {"emit": result()}], control=control)
        self.assertEqual("returned", envelope["state"])
        self.assertEqual(ANSWER, envelope["result"])
        self.assertEqual(2, envelope["native_subagent_frames"])
        self.assertEqual(1, len(envelope["native_results"]))

    def test_intermediate_text_is_partial_evidence_and_never_the_final_answer(self):
        envelope, _, _ = self.run_native(
            [{"read": 1}, {"emit": init()}, {"emit": assistant("Intermediate note")},
             {"emit": result(subtype="error_during_execution", is_error=True, text=None)}])
        self.assertEqual("provider_error", envelope["state"])
        self.assertIsNone(envelope["result"])
        self.assertIn("Intermediate note", envelope["partial_result"])
        self.assertEqual(["fixture native failure"], envelope["provider_errors"])

    # --- session input ---------------------------------------------------

    def test_update_merged_into_the_first_result_is_consumed_once(self):
        entry = request()
        control = Control(entry)
        envelope, _, _ = self.run_native(
            [{"read": 1}, {"emit": init()}, {"read": 1}, {"emit": replay()},
             {"emit": assistant("Picked it up", uuids=["$uuid:0", "$uuid:1"])},
             {"emit": result(uuids=["$uuid:0", "$uuid:1"])}], control=control)
        self.assertEqual("returned", envelope["state"])
        self.assertEqual(ANSWER, envelope["result"])
        self.assertFalse(envelope["needs_attention"])
        self.assertEqual({entry["request_id"]: "consumed"}, control.receipts)
        submitted = self.submitted()
        self.assertEqual(2, len(submitted))
        self.assertEqual(entry["request_id"], submitted[1]["uuid"])
        self.assertEqual({"role": "user", "content": entry["text"]}, submitted[1]["message"])
        self.assertIsNone(submitted[1]["parent_tool_use_id"])
        update = envelope["native_input"][1]
        self.assertEqual("session_input", update["kind"])
        self.assertTrue(update["echoed"])
        self.assertEqual("consumed", update["consumption"])
        self.assertIsNotNone(control.stopped)

    def test_update_answered_by_a_later_result_keeps_the_last_useful_answer(self):
        entry = request()
        control = Control(entry)
        envelope, _, _ = self.run_native(
            [{"read": 1}, {"emit": init()}, {"read": 1}, {"emit": result()},
             {"emit": result(text=LATER, uuids=["$uuid:1"])}], control=control)
        self.assertEqual("returned", envelope["state"])
        self.assertEqual(LATER, envelope["result"])
        self.assertEqual({entry["request_id"]: "consumed"}, control.receipts)
        self.assertEqual(2, len(envelope["native_results"]))
        self.assertEqual([[envelope["initial_message_uuid"]], [entry["request_id"]]],
                         [item["answered"] for item in envelope["native_results"]])

    def test_echoed_update_without_consumption_stays_uncertain(self):
        entry = request()
        control = Control(entry)
        envelope, _, _ = self.run_native(
            [{"read": 1}, {"emit": init()}, {"read": 1}, {"emit": replay()},
             {"emit": result(uuids=["$uuid:0"])}, {"sleep": 0.2}], control=control, timeout=1)
        self.assertEqual("uncertain", envelope["state"])
        self.assertIsNone(envelope["result"])
        self.assertEqual(ANSWER, envelope["partial_result"])
        update = envelope["native_input"][1]
        self.assertTrue(update["echoed"])
        self.assertEqual("unobserved", update["consumption"])
        self.assertFalse(update["result_covered"])
        self.assertEqual({entry["request_id"]: "uncertain"}, control.receipts)

    def test_missing_consumption_fields_leave_a_queued_update_unresolved(self):
        entry = request()
        control = Control(entry)
        envelope, _, _ = self.run_native(
            [{"read": 1}, {"emit": init()}, {"read": 1}, {"emit": result(uuids=None)}],
            control=control)
        self.assertEqual("uncertain", envelope["state"])
        self.assertIsNone(envelope["result"])
        self.assertTrue(envelope["needs_attention"])
        self.assertIn("unknown", envelope["message"])
        self.assertEqual(["unobserved", "unobserved"],
                         [item["consumption"] for item in envelope["native_input"]])
        self.assertEqual({entry["request_id"]: "uncertain"}, control.receipts)

    def test_unrelated_result_cannot_overwrite_or_demote_the_requested_answer(self):
        for extra in (result(text='Unrelated background answer', uuids=['unrelated-message']),
                      result(uuids=None, subtype='error_during_execution', is_error=True)):
            with self.subTest(extra=extra['subtype']):
                envelope, _, _ = self.run_native([{'read':1}, {'emit':init()}, {'emit':result()}, {'emit':extra}])
                self.assertEqual('returned', envelope['state'])
                self.assertEqual(ANSWER, envelope['result'])
                self.assertFalse(envelope['provider_is_error'])
                self.assertEqual('success', envelope['provider_subtype'])

    def test_unattributed_result_before_task_answer_does_not_close_input(self):
        envelope, _, _ = self.run_native([{'read':1}, {'emit':init()},
            {'emit':result(text='Background notice', uuids=None)}, {'sleep':.05}, {'emit':result()}])
        self.assertEqual('returned', envelope['state'])
        self.assertEqual(ANSWER, envelope['result'])
        self.assertEqual([False, True], [row['related'] for row in envelope['native_results']])

    def test_native_deferral_keeps_useful_answer_but_requires_attention(self):
        envelope, _, _ = self.run_native([{'read':1}, {'emit':init()},
            {'emit':result(terminal_reason='tool_deferred', stop_reason='tool_deferred')}])
        self.assertEqual('returned', envelope['state'])
        self.assertEqual(ANSWER, envelope['result'])
        self.assertTrue(envelope['needs_attention'])

    def test_later_failure_preserves_the_earlier_answer_and_both_outcomes(self):
        entry = request()
        control = Control(entry)
        envelope, _, _ = self.run_native(
            [{"read": 1}, {"emit": init()}, {"read": 1}, {"emit": result()},
             {"emit": result(subtype="error_max_turns", is_error=True, uuids=["$uuid:1"],
                             errors=["follow-up exhausted its turns"])}], control=control)
        self.assertEqual("provider_error", envelope["state"])
        self.assertIsNone(envelope["result"])
        self.assertEqual(ANSWER, envelope["partial_result"])
        self.assertTrue(envelope["needs_attention"])
        self.assertEqual("error_max_turns", envelope["provider_subtype"])
        self.assertEqual(["success", "error_max_turns"],
                         [item["subtype"] for item in envelope["native_results"]])
        self.assertEqual({entry["request_id"]: "consumed"}, control.receipts)

    def test_input_for_another_target_shape_is_rejected_without_submission(self):
        wrong_session = request(session=OTHER_SESSION)
        wrong_turn = request(turn="30000000-0000-4000-8000-000000000003")
        blank = request(text="   ")
        control = Control(wrong_session, wrong_turn, blank)
        envelope, _, _ = self.run_native(
            [{"read": 1}, {"emit": init()}, {"sleep": 0.3}, {"emit": result()}], control=control)
        self.assertEqual("returned", envelope["state"])
        self.assertEqual({wrong_session["request_id"]: "rejected", wrong_turn["request_id"]: "rejected",
                          blank["request_id"]: "rejected"}, control.receipts)
        self.assertEqual(1, len(self.submitted()))

    def test_repeated_request_identity_submits_no_duplicate_input(self):
        entry = request()
        control = Control(entry, dict(entry), batch=1)
        envelope, _, _ = self.run_native(
            [{"read": 1}, {"emit": init()}, {"read": 1}, {"sleep": 0.5}], control=control, timeout=2)
        self.assertEqual("uncertain", envelope["state"])
        self.assertIn("no duplicate input was submitted", envelope["message"])
        self.assertEqual([entry["request_id"]],
                         [item["uuid"] for item in envelope["native_input"]
                          if item["kind"] == "session_input"])
        # The first input may be in the pipe when duplicate rejection closes it;
        # native consumption before cleanup is not part of this invariant.
        self.assertLessEqual(len([frame for frame in self.submitted()
                                  if frame["uuid"] == entry["request_id"]]), 1)

    def test_input_is_never_accepted_before_a_validated_session(self):
        entry = request()
        control = Control(entry)
        envelope, _, _ = self.run_native(
            [{"read": 1}, {"emit": init(session=OTHER_SESSION)}], control=control)
        self.assertEqual("uncertain", envelope["state"])
        self.assertEqual([], control.dispatched)
        self.assertEqual(1, len(self.submitted()))

    # --- delivery, callbacks and malformed output ------------------------

    def test_unwritten_task_at_terminal_eof_is_not_successful_delivery(self):
        envelope, directory, _ = self.run_native(
            [{"emit": init()}, {"emit": result(uuids=None)}, {"sleep": 0.2}],
            task="x" * 40000, pipe_size=4096, timeout=3)
        self.assertEqual("uncertain", envelope["state"])
        self.assertIsNone(envelope["result"])
        self.assertEqual("uncertain", envelope["task_delivery"])
        self.assertFalse(envelope["native_input"][0]["written"])
        self.assertEqual(ANSWER, envelope["native_results"][0]["result_excerpt"])
        self.assertFalse(envelope["native_results"][0]["related"])
        self.assert_raw(envelope, directory)

    def test_unsupported_control_request_is_not_answered_by_an_invented_protocol(self):
        control = Control()
        envelope, directory, _ = self.run_native(
            [{"read": 1}, {"emit": init()},
             {"emit": {"type": "control_request", "request_id": "req_1",
                       "request": {"subtype": "can_use_tool", "tool_name": "Bash"}}},
             {"sleep": 0.3}], control=control, timeout=2)
        self.assertEqual("uncertain", envelope["state"])
        self.assertEqual(["can_use_tool"], envelope["unsupported_native_requests"])
        self.assertIsNotNone(control.stopped)
        self.assertEqual(1, len(self.submitted()))
        self.assert_raw(envelope, directory)

    def test_permission_denials_are_summarized_without_tool_input(self):
        envelope, _, _ = self.run_native(
            [{"read": 1}, {"emit": init()},
             {"emit": {"type": "system", "subtype": "permission_denied", "session_id": SESSION,
                       "uuid": str(uuid.uuid4()), "tool_name": "Bash", "tool_use_id": "toolu_9",
                       "message": "denied"}},
             {"emit": result(denials=[{"tool_name": "Write", "tool_use_id": "toolu_8",
                                       "tool_input": {"secret": "task context"}}])}])
        self.assertEqual("returned", envelope["state"])
        self.assertTrue(envelope["needs_attention"])
        self.assertEqual([{"tool_name": "Bash", "tool_use_id": "toolu_9"},
                          {"tool_name": "Write", "tool_use_id": "toolu_8"}], envelope["permission_denials"])

    def test_malformed_duplicate_deep_oversized_and_non_utf8_output_is_bounded(self):
        cases = {
            "not an object": [{"raw": b"[1,2,3]\n".hex()}],
            "duplicate member": [{"raw": b'{"type":"result","type":"assistant"}\n'.hex()}],
            "non-finite": [{"raw": b'{"type":"result","is_error":NaN}\n'.hex()}],
            "non-utf8": [{"raw": (b'{"type":"' + bytes([0xff, 0xfe]) + b'"}\n').hex()}],
            "deep": [{"raw": (b"[" * 4000 + b"]" * 4000 + b"\n").hex()}],
            "incomplete": [{"raw": b'{"type":"result"'.hex()}],
            "unknown result shape": [{"emit": {"type": "result", "subtype": "success",
                                               "uuid": str(uuid.uuid4()), "session_id": SESSION,
                                               "is_error": False, "num_turns": "many"}}],
            "invalid answered list": [{"emit": result(uuids=None, user_message_uuid="a",
                                                      user_message_uuids=["b"])}],
        }
        for name, changes in (
            ("invalid error flag", {"is_error": "false"}),
            ("invalid error list", {"errors": "ARTIFICIAL-PRIVATE-ERROR"}),
            ("invalid error item", {"errors": [7]}),
            ("invalid denial list", {"permission_denials": "ARTIFICIAL-PRIVATE-DENIAL"}),
            ("invalid denial item", {"permission_denials": [7]}),
            ("invalid final identity", {"uuid": None}),
            ("missing final answer", {"result": None}),
        ):
            cases[name] = [{"emit": {**result(num_turns="invalid optional measurement"), **changes}}]
        for name, steps in cases.items():
            with self.subTest(case=name):
                envelope, directory, _ = self.run_native(
                    [{"read": 1}, {"emit": init()}, *steps, {"sleep": 0.2}], timeout=2)
                self.assertEqual("uncertain", envelope["state"])
                self.assertIsNone(envelope["result"])
                self.assertTrue(envelope["needs_attention"])
                self.assertNotIn("provider_subtype", envelope)
                self.assert_raw(envelope, directory)

    def test_oversized_output_keeps_only_the_bounded_prefix(self):
        # Short lines reach the raw cap without one huge parser allocation.
        chunk = {"raw": ((b"x" * 4095 + b"\n") * 32).hex()}
        with mock.patch.object(native_io, "MAX_OUTPUT", 64 * 1024):
            envelope, directory, _ = self.run_native(
                [{"read": 1}, {"emit": init()}, chunk, chunk, {"sleep": 0.2}], timeout=3)
        self.assertEqual("uncertain", envelope["state"])
        self.assertTrue(envelope["stdout_observation"]["truncated"])
        self.assertEqual(64 * 1024 + 1, len(self.assert_raw(envelope, directory)))

    def test_cancellation_retains_raw_output_and_partial_work(self):
        control = Control()
        original = native_io.Observation.read
        seen = []

        def interrupt_after_text(observation):
            if observation.driver.texts and not seen:
                seen.append(True)
                raise KeyboardInterrupt
            return original(observation)

        with mock.patch.object(native_io.Observation, "read", interrupt_after_text):
            with self.assertRaises(KeyboardInterrupt):
                self.run_native([{"read": 1}, {"emit": init()},
                                 {"emit": assistant("Partial work before cancellation")},
                                 {"sleep": 5}], control=control, timeout=5)
        self.assertTrue(seen)

    def test_output_ending_without_a_result_never_claims_a_returned_turn(self):
        envelope, directory, _ = self.run_native(
            [{"read": 1}, {"emit": init()}, {"emit": assistant("Cut short")}, {"exit": 1}])
        self.assertEqual("uncertain", envelope["state"])
        self.assertIsNone(envelope["result"])
        self.assertEqual("Cut short", envelope["partial_result"])
        self.assertIn("without a validated main session result", envelope["message"])
        self.assert_raw(envelope, directory)


if __name__ == "__main__":
    unittest.main()
