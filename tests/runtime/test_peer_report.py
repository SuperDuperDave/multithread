"""Offline support reports read one private receipt and disclose typed facts only."""

import builtins
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import peer_control, provider


CANARY = "ARTIFICIAL-PRIVATE-REPORT-CANARY"
CALL_FIELDS = {
    "provider", "provider_version", "requested_effort", "model_relation",
    "state", "provider_started", "needs_attention", "process_exit_code",
    "elapsed_seconds", "provider_turns", "provider_duration_ms", "estimated_cost_usd",
    "actual_billed_cost", "permission_denial_count", "provider_error_count",
    "session_identity", "stdout_observation", "faults", "unavailable_stage",
    "provider_measurement_scope", "cost_scope",
    "task_submission", "unsupported_native_request_count", "invalid_measurements",
    "task_delivery", "native_input_unwritten_bytes",
    "producer_runtime_identity", "caller_stop_reason",
    "hook_delivery", "provider_tools", "relay_acknowledgement", "workflow_completion",
}
UNCHECKED = ("hook_delivery", "provider_tools", "relay_acknowledgement", "workflow_completion")


class PeerReportTests(unittest.TestCase):
    def test_provider_version_is_attributed_and_selected_in_both_formats(self):
        for client, source in (("codex", "codex_initialize_user_agent"), ("claude", "claude_system_init")):
            with self.subTest(client=client):
                observation = {"status": "reported", "version": "1.2.3", "source": source}
                call = self.reported(provider=client, provider_version={**observation, "private": CANARY},
                                     native_version=CANARY, native_model=CANARY)
                self.assertEqual(observation, call["provider_version"])
                _, output = self.invoke(structured=False)
                self.assertIn("Recorded provider version: 1.2.3 (provider-reported; " + source + ")", output)

    def test_missing_or_unrecognized_version_preserves_call_and_uncertainty(self):
        for client, source in (("codex", "codex_initialize_user_agent"), ("claude", "claude_system_init")):
            call = self.reported(provider=client, native_version="1.2.3")
            self.assertEqual({"status": "not_recorded", "version": None, "source": None}, call["provider_version"])
            for status in ("not_reported", "unrecognized"):
                observation = {"status": status, "version": None, "source": source}
                call = self.reported(provider=client, provider_version=observation)
                self.assertEqual(observation, call["provider_version"])
                self.assertEqual("returned", call["state"])
                self.assertFalse(call["needs_attention"])
                _, output = self.invoke(structured=False)
                self.assertIn("Recorded provider version: unknown (" + status + ")", output)

    def test_invalid_version_provenance_does_not_leak_or_discard_an_outcome(self):
        valid = {"status": "reported", "version": "1.2.3", "source": "claude_system_init"}
        observations = [None, [], CANARY, {}, {**valid, "source": "codex_initialize_user_agent"}]
        for key in valid:
            observations.extend({**valid, key: value} for value in (None, True, [], {}, CANARY))
        observations.extend({**valid, "version": value} for value in (
            "1.2.3+" + CANARY, "1.2.3-" + CANARY, "1.2.3\n", "1.2.3 " + CANARY,
            "01.2.3", "١.2.3", "1" * 200, "1.2.3-alpha.01"))
        observations.extend({**valid, "status": status} for status in ("not_reported", "unrecognized"))
        for observation in observations:
            with self.subTest(observation=observation):
                call = self.reported(provider_version=observation)
                self.assertEqual({"status": "invalid", "version": None, "source": None}, call["provider_version"])
                self.assertEqual("returned", call["state"])
                self.assertFalse(call["needs_attention"])
                _, output = self.invoke(structured=False)
                self.assertIn("Recorded provider version: unknown (invalid)", output)

    def test_human_task_delivery_is_shown_once_and_keeps_consumption_distinct(self):
        self.reported(task_delivery="uncertain", needs_attention=True)
        _, output = self.invoke(structured=False)
        self.assertEqual(1, output.count("pipe delivery:"))
        self.assertIn("Task pipe delivery: uncertain", output)
        self.assertIn("a pipe write does not prove native consumption", output)
        self.assertIn("arbitrary native diagnostics are excluded", output)

    def test_runtime_provenance_is_selected_without_hashes_or_current_installation_reads(self):
        digest = "ab" * 32
        cases = (
            ({}, "not_recorded"),
            ({"producer_runtime": {"status": "recorded", "runtime_manifest_sha256": digest}}, "recorded"),
            ({"producer_runtime": {"status": "unavailable", "runtime_manifest_sha256": None}}, "unavailable"),
            ({"producer_runtime": {"status": "recorded", "runtime_manifest_sha256": CANARY}}, "invalid"),
            ({"producer_runtime": {"status": "recorded", "runtime_manifest_sha256": digest.upper()}}, "invalid"),
            ({"producer_runtime": {"status": "unavailable", "runtime_manifest_sha256": digest}}, "invalid"),
            ({"producer_runtime": {"status": CANARY}}, "invalid"),
            ({"producer_runtime": None}, "invalid"),
        )
        for changes, expected in cases:
            with self.subTest(expected=expected, changes=changes):
                self.write(self.receipt(**changes))
                code, report = self.invoke()
                self.assertEqual(0, code)
                self.assertEqual("returned", report["call"]["state"])
                self.assertEqual(expected, report["call"]["producer_runtime_identity"])
                code, output = self.invoke(structured=False)
                self.assertEqual(0, code)
                self.assertIn("Recorded producer runtime identity: " + expected, output)
                self.assertNotIn(digest, output + json.dumps(report))
                self.assertNotIn(CANARY, output + json.dumps(report))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="multithread-report-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.directory = self.base / CANARY
        self.directory.mkdir(mode=0o700)
        self.result = self.directory / "result.json"

    def receipt(self, **changes):
        value = {"schema": 1, "provider": "claude", "state": "returned",
                 "provider_started": True, "needs_attention": False}
        value.update(changes)
        return value

    def write(self, value):
        self.write_bytes(json.dumps(value, ensure_ascii=True).encode("utf-8"))

    def write_bytes(self, body):
        self.result.write_bytes(body)
        self.result.chmod(0o600)

    def snapshot(self):
        observed = {}
        for path in (self.base, *self.base.rglob("*")):
            info = path.lstat()
            digest = None
            if stat.S_ISREG(info.st_mode):
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except PermissionError:
                    digest = "unreadable"
            observed[str(path.relative_to(self.base))] = (
                info.st_mode, info.st_uid, info.st_size, info.st_mtime_ns, digest,
                os.readlink(path) if stat.S_ISLNK(info.st_mode) else None)
        return observed

    def invoke(self, *, directory=None, structured=True, repo=None, compare_call_dir=None):
        """Guard the public command boundary, independently of report helpers."""
        arguments = ["report", "--call-dir", str(directory or self.directory)]
        if structured:
            arguments.append("--json")
        if repo is not None:
            arguments.extend(("--repo", str(repo)))
        if compare_call_dir is not None:
            arguments.extend(("--compare-call-dir", str(compare_call_dir)))
        before = self.snapshot()
        output, errors = io.StringIO(), io.StringIO()
        native_open, builtin_open, io_open = os.open, builtins.open, io.open
        receipt_descriptors = set()

        def open_descriptor(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND),
                             "Report attempted a write-capable open")
            if not flags & os.O_DIRECTORY:
                self.assertIn(os.fspath(path), ("result.json", "checkpoint.json"),
                              "Report read unrelated evidence")
                self.assertIsInstance(kwargs.get("dir_fd"), int)
                self.assertTrue(flags & os.O_NOFOLLOW, "Receipt open must refuse symlinks")
                self.assertTrue(flags & os.O_NONBLOCK, "Receipt open must not block on a FIFO")
            descriptor = native_open(path, flags, *args, **kwargs)
            if not flags & os.O_DIRECTORY:
                receipt_descriptors.add(descriptor)
            return descriptor

        def open_stream(original, file, mode="r", *args, **kwargs):
            self.assertIs(type(file), int, "Report reopened a pathname outside its directory descriptor")
            self.assertIn(file, receipt_descriptors)
            self.assertFalse(any(flag in mode for flag in ("w", "a", "+", "x")))
            return original(file, mode, *args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            for owner, name in (
                (provider, "prepare"), (provider, "executable"), (provider, "_task"),
                (provider, "_run_peer"), (provider, "_record"), (provider, "_private_file"),
                (provider, "account_launcher"), (peer_control, "control_main"),
                (peer_control, "CallControl"), (subprocess, "Popen"), (subprocess, "run"),
            ):
                stack.enter_context(mock.patch.object(owner, name,
                    side_effect=AssertionError("Report crossed the read-only receipt boundary: " + name)))
            stack.enter_context(mock.patch.object(os, "open", side_effect=open_descriptor))
            stack.enter_context(mock.patch.object(builtins, "open",
                side_effect=lambda *args, **kwargs: open_stream(builtin_open, *args, **kwargs)))
            stack.enter_context(mock.patch.object(io, "open",
                side_effect=lambda *args, **kwargs: open_stream(io_open, *args, **kwargs)))
            code = provider.peer_main(arguments)
        self.assertEqual(before, self.snapshot(), "Report changed retained evidence or directory contents")
        self.assertEqual("", errors.getvalue(), "Report leaked a diagnostic to stderr")
        self.assertNotIn(CANARY, output.getvalue())
        self.assertNotIn(str(self.base), output.getvalue())
        self.assertTrue(output.getvalue())
        return code, json.loads(output.getvalue()) if structured else output.getvalue()

    def reported(self, **changes):
        self.write(self.receipt(**changes))
        code, report = self.invoke()
        self.assertEqual(0, code, report)
        self.assertEqual(1, report["schema"])
        self.assertEqual("peer_report", report["kind"])
        self.assertEqual("reported", report["report_state"])
        self.assertEqual("available", report["receipt_status"])
        self.assertEqual(CALL_FIELDS, set(report["call"]))
        for key in UNCHECKED:
            self.assertEqual("not_checked", report["call"][key])
        return report["call"]

    def unavailable(self, status, **kwargs):
        code, report = self.invoke(**kwargs)
        self.assertEqual(1, code, report)
        self.assertEqual(1, report["schema"])
        self.assertEqual("peer_report", report["kind"])
        self.assertEqual("unavailable", report["report_state"])
        self.assertEqual(status, report["receipt_status"])
        self.assertIsNone(report["call"])
        return report

    def test_minimal_receipt_preserves_unknown_observations(self):
        call = self.reported()
        self.assertEqual("returned", call["state"])
        self.assertTrue(call["provider_started"])
        self.assertFalse(call["needs_attention"])
        for key in ("elapsed_seconds", "process_exit_code", "provider_turns", "provider_duration_ms",
                    "estimated_cost_usd", "permission_denial_count", "provider_error_count",
                    "unsupported_native_request_count", "native_input_unwritten_bytes"):
            self.assertIsNone(call[key], key)
        self.assertEqual("unknown", call["actual_billed_cost"])
        self.assertEqual("unknown", call["provider_measurement_scope"])
        self.assertEqual("unknown", call["cost_scope"])
        self.assertEqual("not_recorded", call["session_identity"])
        self.assertEqual("not_recorded", call["task_submission"])
        self.assertEqual("not_recorded", call["task_delivery"])
        self.assertEqual("not_recorded", call["caller_stop_reason"])
        self.assertEqual([], call["invalid_measurements"])
        self.assertEqual({"status": "not_recorded", "bytes": None, "truncated": None,
                          "scope": "unknown"}, call["stdout_observation"])
        self.assertEqual(dict.fromkeys(("control", "recording", "cleanup"), "not_recorded"), call["faults"])

    def test_caller_stop_reason_is_bounded_and_not_inferred_from_old_receipts(self):
        for reason in ("timeout", "interrupted", "shutdown_timeout", CANARY, None, [], {}):
            with self.subTest(reason=reason):
                self.write(self.receipt(state="uncertain", needs_attention=True,
                                        caller_stop_reason=reason))
                before = self.snapshot()
                code, report = self.invoke()
                expected = reason if reason in ("timeout", "interrupted", "shutdown_timeout") else "unknown"
                self.assertEqual(0, code)
                self.assertEqual(expected, report["call"]["caller_stop_reason"])
                code, output = self.invoke(structured=False)
                self.assertEqual(0, code)
                self.assertIn("Caller stop reason: " + expected, output)
                self.assertIn("not a diagnosis of provider behavior", output)
                self.assertNotIn(CANARY, json.dumps(report) + output)
                self.assertEqual(before, self.snapshot())
        call = self.reported(state="uncertain", needs_attention=True,
                             elapsed_seconds=240.874, process_exit_code=143)
        self.assertEqual("not_recorded", call["caller_stop_reason"])

    def test_report_success_is_independent_of_the_observed_call_state(self):
        for client in ("claude", "codex"):
            for state in ("returned", "uncertain", "provider_error", "unavailable"):
                with self.subTest(provider=client, state=state):
                    call = self.reported(provider=client, state=state, needs_attention=True,
                                         provider_started=state != "unavailable", process_exit_code=None)
                    self.assertEqual(client, call["provider"])
                    self.assertEqual(state, call["state"])
                    self.assertTrue(call["needs_attention"])
                    self.assertEqual(state != "unavailable", call["provider_started"])

    def test_recorded_zeros_are_distinct_from_missing_and_null_metrics(self):
        metrics = ("elapsed_seconds", "process_exit_code", "provider_turns", "provider_duration_ms",
                   "estimated_cost_usd")
        counts = ("permission_denial_count", "provider_error_count", "unsupported_native_request_count")
        call = self.reported(**dict.fromkeys(metrics, 0), permission_denials=[], provider_errors=[],
                             unsupported_native_requests=[])
        for key in (*metrics, *counts):
            self.assertEqual(0, call[key], key)
            self.assertIsNotNone(call[key], key)
        self.assertEqual([], call["invalid_measurements"])
        call = self.reported(**dict.fromkeys(metrics, None), permission_denials=None, provider_errors=None,
                             unsupported_native_requests=None)
        for key in (*metrics, *counts):
            self.assertIsNone(call[key], key)
        self.assertEqual([], call["invalid_measurements"])

    def test_valid_metrics_preserve_estimates_and_process_signals(self):
        call = self.reported(elapsed_seconds=3.25, process_exit_code=-15, provider_turns=2,
                             provider_duration_ms=1500.5, estimated_cost_usd=0.0125,
                             actual_billed_cost=CANARY)
        for key, value in (("elapsed_seconds", 3.25), ("process_exit_code", -15),
                           ("provider_turns", 2), ("provider_duration_ms", 1500.5),
                           ("estimated_cost_usd", 0.0125)):
            self.assertEqual(value, call[key])
        self.assertEqual("unknown", call["actual_billed_cost"])
        self.assertEqual([], call["invalid_measurements"])

    def test_private_native_data_are_absent_from_both_report_formats(self):
        private = {
            "requested_session_id": CANARY + "-session", "session_id": CANARY + "-session",
            "repo": CANARY + "-repo", "evidence_directory": CANARY + "-directory",
            "argv": [CANARY + "-argument"], "task_sha256": CANARY + "-task-digest",
            "follow_up_preparation": {"argv_prefix": [CANARY + "-follow-up-argument"]},
            "result": CANARY + "-answer", "partial_result": CANARY + "-partial-answer",
            "message": CANARY + "-message", "terminal_reason": CANARY + "-reason",
            "provider_subtype": CANARY + "-subtype", "native_session_id": CANARY + "-native-id",
            "turn_id": CANARY + "-turn", "authentication": CANARY + "-authentication",
            "usage": {CANARY + "-model": {CANARY + "-usage-key": 123}},
            "usage_scope": CANARY + "-usage-scope", "cost_scope": CANARY + "-cost-scope",
            "model": CANARY + "-model", "actual_billed_cost": CANARY + "-billing",
            "permission_denials": [{"tool_name": CANARY + "-tool", "tool_use_id": CANARY + "-tool-id"}],
            "provider_errors": [CANARY + "-first-error", CANARY + "-second-error"],
            "unsupported_native_requests": [CANARY + "-native-method"],
            "task_submission": CANARY + "-submission",
            "task_delivery": CANARY + "-delivery",
            "provider_errors_truncated": True,
            "stdout_observation": {"bytes": 23, "truncated": True, "scope": CANARY + "-scope",
                                   "sha256": CANARY + "-output-digest", "raw": CANARY + "-raw"},
            "control_fault": {"state": "unavailable", "detail": CANARY + "-control-detail",
                              "operation": CANARY + "-operation"},
            "evidence_recording": CANARY + "-recording", "server_cleanup": CANARY + "-cleanup",
            "unavailable_stage": CANARY + "-stage", "native_results": [{"result_excerpt": CANARY}],
            "unknown_extension": {CANARY: [CANARY]},
            **dict.fromkeys(UNCHECKED, CANARY + "-claimed-success"),
        }
        call = self.reported(needs_attention=True, **private)
        self.assertEqual(1, call["permission_denial_count"])
        self.assertEqual(2, call["provider_error_count"])
        self.assertEqual(1, call["unsupported_native_request_count"])
        self.assertEqual("unknown", call["task_submission"])
        self.assertEqual("unknown", call["task_delivery"])
        self.assertEqual("unknown", call["unavailable_stage"])
        self.assertEqual("unknown", call["provider_measurement_scope"])
        self.assertEqual("unknown", call["cost_scope"])
        self.assertEqual("unknown", call["stdout_observation"]["scope"])
        self.assertEqual(dict.fromkeys(("control", "recording", "cleanup"), "reported"), call["faults"])
        code, output = self.invoke(structured=False)
        self.assertEqual(0, code)
        self.assertIn("reported", output)
        self.assertIn("returned", output)
        self.assertIn("claude", output)
        self.assertIn("attention", output.lower())

    def test_only_result_receipt_is_read_and_all_other_evidence_is_preserved(self):
        for name in ("request.json", "task.txt", "stdout.json", "stdout.jsonl", "stderr.txt",
                     "provider-config.json", "ledger.sqlite"):
            path = self.directory / name
            path.write_text(CANARY + name, encoding="utf-8")
            path.chmod(0o600)
        control = self.directory / "control"
        control.mkdir(mode=0o700)
        (control / "call.json").write_text(CANARY, encoding="utf-8")
        (control / "call.json").chmod(0o600)
        self.reported(state="uncertain", needs_attention=True)

    def test_latest_result_measurements_and_cumulative_estimates_keep_their_scope(self):
        call = self.reported(
            provider_turns=2, provider_duration_ms=1250, estimated_cost_usd=0.01,
            usage_scope="latest related native result; main loop only, not the whole call",
            cost_scope="cumulative through the latest native result; an estimate, not billing")
        self.assertEqual("latest_related_native_result", call["provider_measurement_scope"])
        self.assertEqual("cumulative_through_latest_native_result", call["cost_scope"])
        self.assertEqual("unknown", call["actual_billed_cost"])

    def test_stable_scope_identifiers_survive_changed_or_missing_native_prose(self):
        for usage in ("native_main_loop", "latest_related_native_result", "native_thread_last_and_total"):
            for prose in ({}, {"usage_scope": CANARY, "cost_scope": CANARY}):
                with self.subTest(usage=usage, prose=prose):
                    call = self.reported(usage_scope_id=usage,
                        cost_scope_id="cumulative_through_latest_native_result", **prose)
                    self.assertEqual(usage, call["provider_measurement_scope"])
                    self.assertEqual("cumulative_through_latest_native_result", call["cost_scope"])
                    code, output = self.invoke(structured=False)
                    self.assertEqual(0, code)
                    self.assertIn("Recorded call: claude / returned", output)

    def test_explicit_unknown_scope_never_falls_back_to_recognized_legacy_prose(self):
        for identifier in (CANARY, None, False, [], {}):
            with self.subTest(identifier=identifier):
                call = self.reported(usage_scope_id=identifier, cost_scope_id=identifier,
                    usage_scope="latest related native result; main loop only, not the whole call",
                    cost_scope="cumulative through the latest native result; an estimate, not billing")
                self.assertEqual("unknown", call["provider_measurement_scope"])
                self.assertEqual("unknown", call["cost_scope"])

    def test_legacy_usage_scope_prose_remains_readable_without_an_identifier(self):
        for prose, scope in (
            ("this native query's main loop; excludes subagents", "native_main_loop"),
            ("latest related native result; main loop only, not the whole call", "latest_related_native_result"),
            ("native thread's last request and running totals; not this call's incremental usage",
             "native_thread_last_and_total"),
        ):
            with self.subTest(scope=scope):
                self.assertEqual(scope, self.reported(usage_scope=prose)["provider_measurement_scope"])

    def test_recorded_measurement_errors_survive_normalized_nulls_without_private_paths(self):
        call = self.reported(provider_turns=None, provider_duration_ms=12, estimated_cost_usd=0.25,
            measurement_errors=["provider_turns", "usage.input_tokens", "usage.output_tokens",
                                "model_usage.model.inputTokens", "model_context_window",
                                CANARY, {CANARY: CANARY}, "usage." + CANARY,
                                "provider_turns", "usage_like_private_field." + CANARY],
            usage={CANARY: CANARY}, model_usage={CANARY: CANARY})
        self.assertEqual("returned", call["state"])
        self.assertFalse(call["needs_attention"])
        self.assertIsNone(call["provider_turns"])
        self.assertEqual(12, call["provider_duration_ms"])
        self.assertEqual(0.25, call["estimated_cost_usd"])
        self.assertEqual({"provider_turns", "usage", "model_usage", "model_context_window"},
                         set(call["invalid_measurements"]))
        self.assertEqual(4, len(call["invalid_measurements"]))
        code, output = self.invoke(structured=False)
        self.assertEqual(0, code)
        self.assertIn("Invalid recorded measurements (values omitted):", output)

    def test_unknown_or_malformed_measurement_error_metadata_does_not_invent_a_task_failure(self):
        for errors in (None, CANARY, {CANARY: CANARY}, [CANARY, None, 12, {}],
                       ["usage_like_private_field.input_tokens"]):
            with self.subTest(errors=errors):
                call = self.reported(measurement_errors=errors)
                self.assertEqual("returned", call["state"])
                self.assertFalse(call["needs_attention"])
                self.assertEqual([], call["invalid_measurements"])

    def test_recorded_task_submission_is_distinct_from_provider_start_or_completion(self):
        for submission in ("not_submitted", "requested", "accepted"):
            with self.subTest(submission=submission):
                call = self.reported(state="uncertain", needs_attention=True,
                                     task_submission=submission)
                self.assertTrue(call["provider_started"])
                self.assertEqual("uncertain", call["state"])
                self.assertEqual(submission, call["task_submission"])
                self.assertEqual("not_checked", call["workflow_completion"])
        for submission in (CANARY, None, False, [], {}):
            with self.subTest(unknown_submission=submission):
                self.assertEqual("unknown", self.reported(task_submission=submission)["task_submission"])

    def test_written_task_and_zero_unwritten_bytes_do_not_imply_a_returned_call(self):
        for client in ("claude", "codex"):
            with self.subTest(provider=client):
                call = self.reported(provider=client, state="uncertain", needs_attention=True,
                                     task_delivery="written", native_input_unwritten_bytes=0)
                self.assertEqual("written", call["task_delivery"])
                self.assertEqual(0, call["native_input_unwritten_bytes"])
                self.assertIsNotNone(call["native_input_unwritten_bytes"])
                self.assertEqual("uncertain", call["state"])
                self.assertTrue(call["needs_attention"])
                self.assertEqual("not_checked", call["workflow_completion"])
                code, output = self.invoke(structured=False)
                self.assertEqual(0, code)
                self.assertIn("Task pipe delivery: written; recorded native-input pending bytes (mode-dependent): 0", output)
                self.assertIn("a pipe write does not prove native consumption", output)

    def test_partial_task_delivery_remains_visible_without_disclosing_unrelated_answers(self):
        for client in ("claude", "codex"):
            with self.subTest(provider=client):
                call = self.reported(provider=client, state="uncertain", needs_attention=True,
                    task_delivery="uncertain", native_input_unwritten_bytes=17,
                    result=CANARY + "-unrelated-answer", partial_result=CANARY + "-partial-answer",
                    native_results=[{"related": False, "result_excerpt": CANARY + "-native-answer"}])
                self.assertEqual("uncertain", call["task_delivery"])
                self.assertEqual(17, call["native_input_unwritten_bytes"])
                self.assertEqual("uncertain", call["state"])
                self.assertTrue(call["needs_attention"])
                self.assertNotIn("result", call)
                self.assertNotIn("native_results", call)
                code, output = self.invoke(structured=False)
                self.assertEqual(0, code)
                self.assertIn("Task pipe delivery: uncertain; recorded native-input pending bytes (mode-dependent): 17", output)

    def test_legacy_delivery_observation_stays_unrecorded_and_null_bytes_stay_unknown(self):
        for observation in ({}, {"native_input_unwritten_bytes": None}):
            with self.subTest(observation=observation):
                call = self.reported(**observation)
                self.assertEqual("not_recorded", call["task_delivery"])
                self.assertIsNone(call["native_input_unwritten_bytes"])
                code, output = self.invoke(structured=False)
                self.assertEqual(0, code)
                self.assertIn("Task pipe delivery: not_recorded; recorded native-input pending bytes (mode-dependent): unknown", output)

    def test_unknown_delivery_values_are_normalized_without_leaking_private_text(self):
        for delivery in (CANARY, None, False, [], {}):
            with self.subTest(delivery=delivery):
                call = self.reported(task_delivery=delivery)
                self.assertEqual("unknown", call["task_delivery"])
                self.assertIsNone(call["native_input_unwritten_bytes"])
                self.assertEqual("returned", call["state"])
                code, output = self.invoke(structured=False)
                self.assertEqual(0, code)
                self.assertIn("Task pipe delivery: unknown; recorded native-input pending bytes (mode-dependent): unknown", output)

    def test_repo_selector_is_accepted_without_enrollment_or_provider_access(self):
        self.write(self.receipt())
        code, ordinary = self.invoke()
        self.assertEqual(0, code)
        code, selected = self.invoke(repo=self.base / (CANARY + "-nonexistent-repository"))
        self.assertEqual(0, code, selected)
        self.assertEqual(ordinary, selected)

    def test_invalid_auxiliary_measurements_preserve_core_outcome_and_other_measurements(self):
        measurements = {"provider_turns": 2, "provider_duration_ms": 1250.5,
                        "estimated_cost_usd": 0.0125}
        for field in measurements:
            invalid = [-1, False, CANARY, {}, 10**400]
            if field == "provider_turns":
                invalid.append(1.5)
            for value in invalid:
                with self.subTest(field=field, value=value):
                    call = self.reported(**{**measurements, field: value})
                    self.assertEqual("returned", call["state"])
                    self.assertTrue(call["provider_started"])
                    self.assertFalse(call["needs_attention"])
                    self.assertIsNone(call[field])
                    self.assertEqual([field], call["invalid_measurements"])
                    for other in measurements.keys() - {field}:
                        self.assertEqual(measurements[other], call[other])
        for state in ("returned", "uncertain", "provider_error", "unavailable"):
            with self.subTest(state=state):
                call = self.reported(state=state, needs_attention=state != "returned",
                    provider_started=state != "unavailable", **dict.fromkeys(measurements, CANARY))
                self.assertEqual(state, call["state"])
                self.assertEqual(state != "returned", call["needs_attention"])
                self.assertEqual(state != "unavailable", call["provider_started"])
                self.assertEqual(set(measurements), set(call["invalid_measurements"]))
                self.assertEqual(3, len(call["invalid_measurements"]))
                for field in measurements:
                    self.assertIsNone(call[field])

    def test_human_report_exposes_recorded_submission_counts_faults_and_invalid_measurements(self):
        self.write(self.receipt(
            state="uncertain", needs_attention=True, task_submission="accepted",
            unavailable_stage="provider_call", requested_session_id=CANARY,
            permission_denials=[{"tool_name": CANARY}, {"tool_name": CANARY}],
            provider_errors=[], unsupported_native_requests=[CANARY],
            control_fault={"state": "unavailable", "operation": CANARY, "detail": CANARY},
            evidence_recording=CANARY, owned_process_cleanup=CANARY,
            provider_turns=CANARY, provider_duration_ms=CANARY, estimated_cost_usd=CANARY))
        code, output = self.invoke(structured=False)
        self.assertEqual(0, code)
        for line in (
            "Recorded call: claude / uncertain",
            "Recorded task submission: accepted",
            "Unavailable stage: provider_call",
            "Retained permission denials: 2",
            "Retained provider errors: 0",
            "Retained unsupported native requests: 1",
            "Recorded control fault: reported",
            "Recorded recording fault: reported",
            "Recorded cleanup fault: reported",
            "Recorded session identity: requested_only (identifiers omitted)",
        ):
            self.assertIn(line, output)
        measurement_line = next(line for line in output.splitlines()
                                if line.startswith("Invalid recorded measurements"))
        self.assertIn("values omitted", measurement_line)
        for field in ("provider_turns", "provider_duration_ms", "estimated_cost_usd"):
            self.assertIn(field, measurement_line)

    def test_human_report_keeps_unknown_counts_and_submission_explicit(self):
        self.write(self.receipt(session_id=CANARY))
        code, output = self.invoke(structured=False)
        self.assertEqual(0, code)
        self.assertIn("Recorded task submission: not_recorded", output)
        self.assertIn("Recorded session identity: verified (identifiers omitted)", output)
        for label in ("permission denials", "provider errors", "unsupported native requests"):
            self.assertIn("Retained " + label + ": unknown", output)
        self.assertNotIn("fault: reported", output)
        self.assertNotIn("Invalid recorded measurements", output)
        self.assertNotIn("Unavailable stage:", output)
        self.assertNotIn("Next:", output)

    def test_session_identity_reports_attribution_without_exposing_identifiers(self):
        cases = (
            ({}, "not_recorded"),
            ({"session_id": None, "requested_session_id": None}, "not_recorded"),
            ({"requested_session_id": CANARY}, "requested_only"),
            ({"session_id": CANARY}, "verified"),
            ({"requested_session_id": CANARY, "session_id": CANARY}, "verified"),
            ({"requested_session_id": CANARY, "observed_session_id": CANARY + "-other"}, "mismatch_observed"),
        )
        for changes, expected in cases:
            with self.subTest(expected=expected, fields=list(changes)):
                self.assertEqual(expected, self.reported(**changes)["session_identity"])

    def test_stdout_zero_and_truncated_prefix_are_recorded_observations(self):
        for count, truncated in ((0, False), (81, False), (81, True)):
            with self.subTest(bytes=count, truncated=truncated):
                call = self.reported(state="uncertain", needs_attention=True,
                    stdout_observation={"bytes": count, "truncated": truncated, "scope": "bounded_read"})
                self.assertEqual({"status": "recorded", "bytes": count, "truncated": truncated,
                                  "scope": "bounded_read"}, call["stdout_observation"])
                self.assertEqual("uncertain", call["state"])

    def test_stream_observation_without_scope_does_not_become_a_bounded_read(self):
        for truncated in (False, True):
            with self.subTest(truncated=truncated):
                call = self.reported(provider="codex", state="uncertain", needs_attention=True,
                    stdout_observation={"bytes": 81, "truncated": truncated, "sha256": CANARY})
                self.assertEqual({"status": "recorded", "bytes": 81, "truncated": truncated,
                                  "scope": "unknown"}, call["stdout_observation"])
                code, output = self.invoke(structured=False)
                self.assertEqual(0, code)
                self.assertIn("scope=unknown", output)
                self.assertNotIn("bounded_read", output)

    def test_stdout_read_error_supersedes_a_count_without_claiming_empty_output(self):
        call = self.reported(state="uncertain", needs_attention=True,
            stdout_observation={"bytes": 0, "truncated": False, "scope": "bounded_read"},
            stdout_observation_error=CANARY)
        self.assertEqual({"status": "unavailable", "bytes": None, "truncated": None,
                          "scope": "unknown"}, call["stdout_observation"])

    def test_each_recorded_cleanup_fault_preserves_returned_with_attention(self):
        for key in ("server_cleanup", "owned_process_cleanup", "stdout_completion"):
            with self.subTest(key=key):
                call = self.reported(needs_attention=True, **{key: CANARY})
                self.assertEqual("returned", call["state"])
                self.assertTrue(call["needs_attention"])
                self.assertEqual("reported", call["faults"]["cleanup"])

    def test_known_unavailable_stage_is_an_enum_and_unknown_text_is_not_copied(self):
        for stage in ("task_read", "relay_configuration", "evidence_setup", "provider_spawn",
                      "provider_call", "result_read"):
            with self.subTest(stage=stage):
                call = self.reported(state="unavailable", provider_started=False, unavailable_stage=stage)
                self.assertEqual(stage, call["unavailable_stage"])

    def test_missing_receipt_and_missing_directory_do_not_infer_a_call_state(self):
        self.unavailable("missing")
        self.unavailable("unavailable", directory=self.base / "missing")
        code, output = self.invoke(structured=False)
        self.assertEqual(1, code)
        self.assertIn("unavailable", output)
        self.assertIn("missing", output)
        self.assertNotIn("provider_error", output)
        self.assertNotIn("active", output.lower())

    def test_missing_terminal_receipt_projects_only_a_nonterminal_checkpoint(self):
        checkpoint = {"schema": 1, "kind": "peer_checkpoint", "provider": "claude",
                      "phase": "spawned", "provider_start_observation": "confirmed",
                      "requested_session_id": CANARY, "outcome": "unknown"}
        path = self.directory / "checkpoint.json"
        path.write_text(json.dumps(checkpoint), encoding="utf-8")
        path.chmod(0o600)
        code, report = self.invoke()
        self.assertEqual(1, code)
        self.assertEqual("incomplete", report["report_state"])
        self.assertEqual("missing", report["receipt_status"])
        self.assertEqual({"provider": "claude", "phase": "spawned",
                          "provider_start_observation": "confirmed", "outcome": "unknown"},
                         report["checkpoint"])
        self.assertNotIn(CANARY, json.dumps(report))

    def test_before_spawn_checkpoint_does_not_claim_provider_start(self):
        checkpoint = {"schema": 1, "kind": "peer_checkpoint", "provider": "claude",
                      "phase": "before_spawn", "provider_start_observation": "unknown",
                      "requested_session_id": CANARY, "outcome": "unknown"}
        path = self.directory / "checkpoint.json"
        path.write_text(json.dumps(checkpoint), encoding="utf-8")
        path.chmod(0o600)
        code, report = self.invoke()
        self.assertEqual(1, code)
        self.assertEqual("unknown", report["checkpoint"]["provider_start_observation"])
        self.assertNotIn(CANARY, json.dumps(report))

    def test_same_session_cumulative_difference_is_qualified_and_read_only(self):
        session = "00000000-0000-4000-8000-000000000001"
        prior = self.base / "prior"
        prior.mkdir(mode=0o700)
        earlier = self.receipt(session_id=session, resumed=False,
                               cost_scope_id="cumulative_through_latest_native_result",
                               estimated_cost_usd=1.25)
        (prior / "result.json").write_text(json.dumps(earlier), encoding="utf-8")
        (prior / "result.json").chmod(0o600)
        self.write(self.receipt(session_id=session, resumed=True,
                                cost_scope_id="cumulative_through_latest_native_result",
                                estimated_cost_usd=1.5))
        code, report = self.invoke(compare_call_dir=prior)
        self.assertEqual(0, code)
        self.assertEqual("estimated", report["cost_comparison"]["status"])
        self.assertEqual(0.25, report["cost_comparison"]["estimated_difference_usd"])
        self.assertIn("intervening", report["cost_comparison"]["scope"])
        self.write(self.receipt(session_id="other-session", resumed=True,
                                cost_scope_id="cumulative_through_latest_native_result",
                                estimated_cost_usd=1.5))
        _, report = self.invoke(compare_call_dir=prior)
        self.assertEqual("unavailable", report["cost_comparison"]["status"])

    def test_unavailable_human_reports_offer_one_private_preserving_next_step(self):
        valid = json.dumps(self.receipt(result=CANARY)).encode("utf-8")
        cases = (
            ("missing", None, 0o600,
             "compare the selected call directory with the original call's retained-evidence location."),
            ("malformed", b'{"' + CANARY.encode("ascii"), 0o600,
             "inspect the original private result.json locally for invalid or incomplete data without rewriting it."),
            ("unavailable", valid, 0o640,
             "verify the selected directory and receipt against the documented access, ownership, privacy and file-type requirements."),
            ("unsupported_schema", json.dumps(self.receipt(schema=2, result=CANARY)).encode("utf-8"), 0o600,
             "select a reviewed reporter that supports the retained receipt's schema, preserving the original receipt."),
            ("too_large", valid + b" " * 512, 0o600,
             "inspect the original private receipt locally in bounded portions without truncating or rewriting it."),
        )
        for status, body, mode, next_step in cases:
            with self.subTest(status=status), mock.patch.object(provider, "_MAX_RESULT", 512):
                self.result.unlink(missing_ok=True)
                if body is not None:
                    self.write_bytes(body)
                    self.result.chmod(mode)
                code, report = self.invoke()
                self.assertEqual(1, code)
                self.assertEqual({"schema": 1, "kind": "peer_report", "report_state": "unavailable",
                                  "receipt_status": status, "call": None}, report)
                code, output = self.invoke(structured=False)
                self.assertEqual(1, code)
                self.assertIn("Multithread peer report: unavailable", output)
                self.assertIn("Result receipt: " + status, output)
                self.assertEqual(["Next: " + next_step],
                                 [line for line in output.splitlines() if line.startswith("Next:")])
                self.assertIn("provider activity, cause and workflow completion are not checked", output)
                self.assertNotIn("Recorded call:", output)
                self.assertNotIn("Needs attention:", output)

    def test_required_fields_and_enums_are_validated_before_reporting(self):
        for key in ("schema", "provider", "state", "provider_started", "needs_attention"):
            with self.subTest(missing=key):
                receipt = self.receipt()
                del receipt[key]
                self.write(receipt)
                self.unavailable("malformed")
        for key, value in (("schema", True), ("schema", "1"), ("schema", None),
                           ("provider", CANARY), ("provider", []), ("state", CANARY),
                           ("state", {}), ("provider_started", 1), ("needs_attention", 0)):
            with self.subTest(key=key, value=value):
                self.write(self.receipt(**{key: value}))
                self.unavailable("malformed")

    def test_unsupported_integer_schema_is_distinct_from_malformed_receipt(self):
        for schema in (0, 2, -1):
            with self.subTest(schema=schema):
                self.write(self.receipt(schema=schema))
                self.unavailable("unsupported_schema")

    def test_invalid_projected_optional_values_refuse_without_rewriting_receipt(self):
        changes = [
            {"process_exit_code": True}, {"process_exit_code": "0"}, {"process_exit_code": 0.5},
            {"elapsed_seconds": -0.1}, {"elapsed_seconds": True}, {"elapsed_seconds": "2"},
            {"native_input_unwritten_bytes": -1}, {"native_input_unwritten_bytes": False},
            {"native_input_unwritten_bytes": True}, {"native_input_unwritten_bytes": 1.5},
            {"native_input_unwritten_bytes": CANARY}, {"native_input_unwritten_bytes": {}},
            {"permission_denials": {}}, {"permission_denials": [CANARY]},
            {"provider_errors": CANARY}, {"provider_errors": [{"error": CANARY}]},
            {"unsupported_native_requests": CANARY}, {"unsupported_native_requests": [1]},
            {"session_id": []}, {"requested_session_id": False}, {"observed_session_id": 1},
            {"stdout_observation": []},
            {"control_fault": CANARY}, {"control_fault": {}},
            {"control_fault": {"state": "returned", "operation": CANARY, "detail": CANARY}},
            {"control_fault": {"state": "unavailable", "operation": None, "detail": CANARY}},
        ]
        for field in ("evidence_recording", "server_cleanup", "owned_process_cleanup",
                      "stdout_completion", "stdout_observation_error"):
            changes.extend({field: value} for value in (None, False, {}, ""))
        for field, value in (("bytes", -1), ("bytes", True), ("bytes", 1.5), ("truncated", "false")):
            observation = {"bytes": 0, "truncated": False, "scope": "bounded_read"}
            observation[field] = value
            changes.append({"stdout_observation": observation})
        for change in changes:
            with self.subTest(change=change):
                self.write(self.receipt(**change))
                self.unavailable("malformed")

    def test_malformed_json_unicode_and_nonobject_receipts_are_unavailable(self):
        for body in (b"", b"{", b"null", b"true", b"[]", b'"text"', b'{"private":"\xff"}'):
            with self.subTest(body=body):
                self.write_bytes(body)
                self.unavailable("malformed")

    def test_duplicate_keys_and_nonfinite_numbers_are_rejected_including_unknown_fields(self):
        prefix = json.dumps(self.receipt())[:-1].encode("ascii")
        for suffix in (b', "schema":1}', b', "private":{"duplicate":1,"duplicate":2}}',
                       b', "elapsed_seconds":NaN}', b', "private":Infinity}',
                       b', "provider_turns":NaN}', b', "provider_duration_ms":Infinity}',
                       b', "estimated_cost_usd":-Infinity}',
                       b', "private":-Infinity}', b', "private":{"value":1e400}}'):
            with self.subTest(suffix=suffix):
                self.write_bytes(prefix + suffix)
                self.unavailable("malformed")

    def test_receipt_read_is_bounded_even_when_json_would_otherwise_be_valid(self):
        body = json.dumps(self.receipt()).encode("ascii")
        self.write_bytes(body + b" " * (16 * 1024 * 1024 + 1 - len(body)))
        self.unavailable("too_large")

    def test_receipt_exactly_at_the_read_bound_is_still_available(self):
        body = json.dumps(self.receipt()).encode("ascii")
        self.write_bytes(body + b" " * (16 * 1024 * 1024 - len(body)))
        code, report = self.invoke()
        self.assertEqual(0, code, report)
        self.assertEqual("available", report["receipt_status"])

    def test_symlinked_receipt_and_symlinked_directory_components_are_refused(self):
        self.write(self.receipt())
        target = self.base / "receipt-target.json"
        self.result.rename(target)
        self.result.symlink_to(target)
        self.unavailable("unavailable")
        self.result.unlink()
        target.rename(self.result)
        alias = self.base / "directory-alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        self.unavailable("unavailable", directory=alias)
        ancestor_alias = self.base / "ancestor-alias"
        ancestor_alias.symlink_to(self.base, target_is_directory=True)
        self.unavailable("unavailable", directory=ancestor_alias / self.directory.name)

    def test_fifo_and_directory_receipts_are_refused_without_blocking(self):
        os.mkfifo(self.result, 0o600)
        self.unavailable("unavailable")
        self.result.unlink()
        self.result.mkdir(mode=0o700)
        self.unavailable("unavailable")

    def test_publicly_readable_or_unreadable_receipt_and_directory_modes_are_refused(self):
        self.write(self.receipt())
        for mode in (0o644, 0o400, 0o000):
            with self.subTest(file_mode=oct(mode)):
                self.result.chmod(mode)
                self.unavailable("unavailable")
        self.result.chmod(0o600)
        self.directory.chmod(0o750)
        try:
            self.unavailable("unavailable")
        finally:
            self.directory.chmod(0o700)

    def test_receipt_owned_by_another_account_is_refused(self):
        self.write(self.receipt())
        original = os.fstat

        def foreign_file(descriptor):
            info = original(descriptor)
            if stat.S_ISREG(info.st_mode):
                fields = list(info)
                fields[4] = info.st_uid + 1
                return os.stat_result(fields)
            return info

        with mock.patch.object(os, "fstat", side_effect=foreign_file):
            self.unavailable("unavailable")

    def test_unreadable_receipt_diagnostic_never_discloses_private_exception_text(self):
        self.write(self.receipt())
        original = os.open

        def denied(path, flags, *args, **kwargs):
            if os.fspath(path) == "result.json":
                raise PermissionError(CANARY)
            return original(path, flags, *args, **kwargs)

        with mock.patch.object(os, "open", side_effect=denied):
            self.unavailable("unavailable")

    def test_report_help_is_available_without_provider_or_receipt_access(self):
        output, errors = io.StringIO(), io.StringIO()
        with (redirect_stdout(output), redirect_stderr(errors),
              mock.patch.object(provider, "prepare", side_effect=AssertionError("provider preparation")),
              mock.patch.object(os, "open", side_effect=AssertionError("file access")),
              self.assertRaises(SystemExit) as stopped):
            provider.peer_main(["report", "--help"])
        self.assertEqual(0, stopped.exception.code)
        self.assertIn("--call-dir", output.getvalue())
        self.assertIn("--json", output.getvalue())
        self.assertEqual("", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
