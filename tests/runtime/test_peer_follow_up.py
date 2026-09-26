"""Follow-up preparation preserves exact invocation without starting another call."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import provider
import test_peer as peer_fixture


class PeerFollowUpTests(unittest.TestCase):
    def setUp(self):
        self.fixture = peer_fixture.PeerTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def preparation(self, *, client="claude", report_entry=None, **changes):
        fixture = self.fixture
        args = SimpleNamespace(client=client, report_entry=report_entry,
                               timeout=937, max_turns=37 if client == "claude" else None,
                               live_input=client == "claude", model=None,
                               effort=None, stream_progress=False)
        plan = {"repo": str(fixture.repo), "argv": [str(fixture.provider)],
                "relay_plan": {"hook_command": shlex.join(
                    [str(fixture.relay), *([] if client == "codex" else ["--repo", str(fixture.repo)]),
                     "provider-hook", "--client", client])}}
        envelope = {"provider": client, "state": "returned", "provider_started": True,
                    "process_exit_code": 0, "needs_attention": False,
                    "session_id": "00000000-0000-4000-8000-000000000001"}
        envelope.update(changes)
        original = deepcopy((vars(args), plan, envelope))
        result = provider._follow_up_preparation(args, plan, envelope)
        self.assertEqual(original, (vars(args), plan, envelope))
        return result

    def test_retains_source_entry_live_input_and_chosen_limits(self):
        entry = [sys.executable, "-I", "-S", "-B", str(ROOT / "examples/call_peer.py")]
        prefix = self.preparation(report_entry=entry)["argv_prefix"]
        fixture = self.fixture
        self.assertEqual([*entry, "claude", "--repo", str(fixture.repo),
                          "--multithread", str(fixture.relay), "--provider", str(fixture.provider),
                          "--resume=00000000-0000-4000-8000-000000000001",
                          "--timeout", "937", "--max-turns", "37", "--live-input",
                          "--dry-run", "--json", "--task-file"], prefix)
        self.assertNotIn(str(fixture.task), prefix)
        self.assertNotIn("--output-dir", prefix)

    def test_clean_result_preserves_provider_symlink_and_records_preparation(self):
        fixture = self.fixture
        entry = fixture.base / "provider alias 'literal' ;$(touch injected)"
        entry.symlink_to(fixture.provider)
        code, result, _ = fixture.invoke("--provider", str(entry), "--timeout", "937", "--max-turns", "37")
        self.assertEqual(0, code, result)
        prefix = result["follow_up_preparation"]["argv_prefix"]
        self.assertEqual([str(fixture.relay), "peer", "claude"], prefix[:3])
        self.assertEqual(str(entry), prefix[prefix.index("--provider") + 1])
        self.assertEqual(str(fixture.repo), prefix[prefix.index("--repo") + 1])
        self.assertEqual(str(fixture.relay), prefix[prefix.index("--multithread") + 1])
        self.assertIn("--resume=" + result["session_id"], prefix)
        self.assertEqual("937", prefix[prefix.index("--timeout") + 1])
        self.assertEqual("37", prefix[prefix.index("--max-turns") + 1])
        self.assertEqual(["--dry-run", "--json", "--task-file"], prefix[-3:])
        self.assertNotIn("--live-input", prefix)
        self.assertFalse((fixture.repo / "injected").exists())
        receipt = json.loads((Path(result["evidence_directory"]) / "result.json").read_text())
        self.assertEqual(result["follow_up_preparation"], receipt["follow_up_preparation"])

    def test_generated_source_preparation_reads_new_task_without_provider_or_evidence_writes(self):
        fixture = self.fixture
        entry = [sys.executable, "-I", "-S", "-B", str(ROOT / "examples/call_peer.py")]
        original_directory = fixture.base / "original-call"
        initial = subprocess.run(
            [*entry, "claude", "--repo", str(fixture.repo), "--multithread", str(fixture.relay),
             "--provider", str(fixture.provider), "--task-file", str(fixture.task),
             "--output-dir", str(original_directory), "--timeout", "937", "--max-turns", "37", "--json"],
            cwd=fixture.repo, env=fixture.environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, initial.returncode, initial.stderr)
        result = json.loads(initial.stdout)
        prefix = result["follow_up_preparation"]["argv_prefix"]
        self.assertEqual(entry, prefix[:len(entry)])
        task = fixture.base / "new task 雪.txt"
        task.write_text("Assess the changed evidence and remaining question.\n", encoding="utf-8")
        destination = fixture.base / "new-call"
        prepared = subprocess.run([*prefix, str(task), "--output-dir", str(destination)],
                                  cwd=fixture.repo, env=fixture.environment,
                                  capture_output=True, text=True, timeout=15)
        self.assertEqual(0, prepared.returncode, prepared.stderr)
        plan = json.loads(prepared.stdout)
        self.assertEqual("call_prepared", plan["state"])
        self.assertFalse(plan["provider_started"])
        self.assertEqual(result["session_id"], plan["requested_session_id"])
        self.assertEqual(937, plan["timeout_seconds"])
        self.assertEqual(hashlib.sha256(task.read_bytes()).hexdigest(), plan["task_sha256"])
        self.assertIn("--resume", plan["argv"])
        self.assertEqual("37", plan["argv"][plan["argv"].index("--max-turns") + 1])
        self.assertNotIn("follow_up_preparation", plan)
        self.assertEqual("call\n", fixture.calls.read_text())
        self.assertFalse(destination.exists())

    def test_bare_task_option_fails_before_task_reads_or_preparation(self):
        prefix = self.preparation()["argv_prefix"]
        with (mock.patch.object(provider, "_task") as task,
              mock.patch.object(provider, "prepare") as prepare,
              redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised):
            provider.peer_main(prefix[2:])
        self.assertEqual(2, raised.exception.code)
        task.assert_not_called()
        prepare.assert_not_called()
        self.assertFalse(self.fixture.calls.exists())

    def test_opaque_codex_identity_remains_one_argument_even_with_leading_dash(self):
        identity = "-opaque native 'thread' 雪"
        prefix = self.preparation(client="codex", session_id=identity)["argv_prefix"]
        self.assertIn("--resume=" + identity, prefix)
        self.assertNotIn("--max-turns", prefix)
        self.assertNotIn("--live-input", prefix)
        stdout = io.StringIO()
        with (mock.patch.object(provider, "_run_peer", return_value=0) as run,
              redirect_stdout(stdout)):
            self.assertEqual(0, provider.peer_main([*prefix[2:], str(self.fixture.task)]))
        self.assertEqual(identity, run.call_args.args[0].resume)
        self.assertTrue(run.call_args.args[0].dry_run)

    def test_ineligible_outcomes_never_offer_preparation(self):
        cases = [{"state": state} for state in
                 ("unavailable", "uncertain", "provider_error", "call_prepared")]
        cases.extend(({"needs_attention": True}, {"needs_attention": None},
                      {"provider_started": False}, {"session_id": None},
                      {"process_exit_code": None}, {"process_exit_code": 1},
                      {"observed_session_id": "unverified"}, {"server_cleanup": "required"},
                      {"control_fault": {}}, {"evidence_recording": "unavailable"}))
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertIsNone(self.preparation(**changes))

    def test_auxiliary_measurement_errors_do_not_gate_valid_follow_up(self):
        self.assertIsNotNone(self.preparation(measurement_errors={"usage": "invalid"},
                                             actual_billed_cost="unknown"))

    def test_provider_failures_and_attention_never_record_preparation(self):
        cases = ({"raw": "invalid native JSON"},
                 {"native": {"session_id": "00000000-0000-4000-8000-000000000099"}},
                 {"native": {"subtype": "error_during_execution", "is_error": True}},
                 {"native": {"permission_denials": [{"tool_name": "artificial-tool"}]}},
                 {"exit": 1})
        for spec in cases:
            with self.subTest(spec=spec):
                self.fixture.configure(**spec)
                _, result, _ = self.fixture.invoke()
                self.assertTrue(result["needs_attention"])
                self.assertNotIn("follow_up_preparation", result)
                receipt = json.loads((Path(result["evidence_directory"]) / "result.json").read_text())
                self.assertNotIn("follow_up_preparation", receipt)

    def test_final_receipt_failure_removes_preparation_from_returned_json(self):
        record = provider._atomic_record

        def fail_final(directory, name, value, **kwargs):
            if name == "result.json":
                self.assertIn("follow_up_preparation", value)
                raise OSError("artificial final receipt failure")
            return record(directory, name, value, **kwargs)

        with mock.patch.object(provider, "_atomic_record", side_effect=fail_final):
            code, result, _ = self.fixture.invoke()
        self.assertEqual(1, code)
        self.assertEqual("returned", result["state"])
        self.assertTrue(result["needs_attention"])
        self.assertNotIn("follow_up_preparation", result)


if __name__ == "__main__":
    unittest.main()
