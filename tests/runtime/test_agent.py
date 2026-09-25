"""Optional Muse adapter routing with isolated account state and synthetic clients."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import agent, cli


class MuseAgentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="multithread-agent-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.private = self.base / "private"
        self.private.mkdir(mode=0o700)
        self.registry = self.private / "agents.json"
        self.client = self.base / "client.py"
        self.calls = self.base / "calls.json"
        self.client.write_text("import json, sys\n"
                               "from pathlib import Path\n"
                               f"Path({str(self.calls)!r}).write_text(json.dumps(sys.argv[1:]))\n"
                               "print(json.dumps({'ok': True, 'command': sys.argv[-1]}))\n")

    def run_agent(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = agent.agent_main(["muse", *args], registry_path=self.registry)
        return code, json.loads(output.getvalue()) if output.getvalue() else None

    def test_optional_registration_and_nickname_routing(self):
        code, result = self.run_agent("list")
        self.assertEqual((code, result["agents"]), (0, []))
        self.assertFalse(self.registry.exists())

        code, result = self.run_agent("register", "Buddy", "--client", str(self.client), "--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(result["registered"])
        self.assertFalse(self.registry.exists())

        code, result = self.run_agent("register", "Buddy", "--client", str(self.client))
        self.assertEqual(code, 0)
        self.assertTrue(result["registered"])
        self.assertEqual(self.registry.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_agent("list")[1]["agents"], ["Buddy"])
        self.assertEqual(self.run_agent("inspect", "buddy")[1]["sha256"], result["sha256"])

        repo = self.base / "repo"
        repo.mkdir()
        code, response = self.run_agent("project", "BUDDY", "--repo", str(repo))
        self.assertEqual(code, 0)
        self.assertEqual(response["nickname"], "Buddy")
        self.assertEqual(json.loads(self.calls.read_text()), ["--repo", str(repo), "project"])

        code, removed = self.run_agent("remove", "Buddy")
        self.assertEqual((code, removed["removed"]), (0, True))
        self.assertEqual(self.run_agent("list")[1]["agents"], [])
        self.assertTrue(self.client.exists())

    def test_changed_client_refuses_before_execution(self):
        self.run_agent("register", "Buddy", "--client", str(self.client))
        self.client.write_text(self.client.read_text() + "\n# changed\n")
        error = io.StringIO()
        with mock.patch("sys.stderr", error):
            self.assertEqual(agent.agent_main(["muse", "project", "Buddy"], registry_path=self.registry), 1)
        self.assertIn("adapter bytes changed", error.getvalue())
        self.assertFalse(self.calls.exists())

    def test_ambient_pythonpath_cannot_replace_adapter_imports(self):
        shadow = self.base / "shadow"
        shadow.mkdir()
        marker = self.base / "shadow-imported"
        (shadow / "json.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('loaded')\n")
        self.run_agent("register", "Buddy", "--client", str(self.client))
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(shadow)}):
            code, result = self.run_agent("project", "Buddy")
        self.assertEqual(code, 0)
        self.assertEqual(result["result"]["ok"], True)
        self.assertFalse(marker.exists())

    def test_adapter_runs_the_held_verified_bytes_after_source_changes(self):
        self.run_agent("register", "Buddy", "--client", str(self.client))
        original_run = subprocess.run

        def change_before_child(command, **kwargs):
            self.client.write_text("raise RuntimeError('changed source executed')\n")
            return original_run(command, **kwargs)

        with mock.patch.object(agent.subprocess, "run", side_effect=change_before_child):
            code, result = self.run_agent("project", "Buddy")
        self.assertEqual(code, 0)
        self.assertEqual(result["result"]["ok"], True)
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(self.run_agent("project", "Buddy")[0], 1)
        self.assertIn("adapter bytes changed", error.getvalue())

    def test_adapter_failure_keeps_bounded_diagnostics_and_uncertain_send(self):
        self.client.write_text("import sys\nprint('synthetic expired token', file=sys.stderr)\n"
                               "raise SystemExit(3)\n")
        self.run_agent("register", "Buddy", "--client", str(self.client))
        error = io.StringIO()
        with redirect_stderr(error):
            code, result = self.run_agent("send", "Buddy", str(self.base / "packet.json"))
        self.assertEqual((code, result["state"], result["adapter_exit_code"]), (1, "uncertain", 3))
        self.assertIn("synthetic expired token", error.getvalue())

    def test_bad_success_receipt_keeps_safe_bounded_diagnostics(self):
        self.client.write_text("import sys\nsys.stderr.write('x' * 5000 + '\\x1b')\n"
                               "print('{}')\n")
        self.run_agent("register", "Buddy", "--client", str(self.client))
        error = io.StringIO()
        with redirect_stderr(error):
            code, result = self.run_agent("send", "Buddy", str(self.base / "packet.json"))
        self.assertEqual((code, result["state"]), (1, "uncertain"))
        self.assertEqual(error.getvalue().count("x"), 4095)
        self.assertIn("\\u001b", error.getvalue())
        self.assertNotIn("\x1b", error.getvalue())

    def test_adapter_main_module_identity_supports_pickle(self):
        self.client.write_text("import json, pickle\nclass Packet: pass\n"
                               "pickle.dumps(Packet())\nprint(json.dumps({'ok': True}))\n")
        self.run_agent("register", "Buddy", "--client", str(self.client))
        code, result = self.run_agent("project", "Buddy")
        self.assertEqual((code, result["result"]["ok"]), (0, True))

    def test_timeout_distinguishes_uncertain_send_from_unavailable_read(self):
        self.run_agent("register", "Buddy", "--client", str(self.client))
        with mock.patch.object(agent.subprocess, "run", side_effect=subprocess.TimeoutExpired("adapter", 300)):
            send_code, send = self.run_agent("send", "Buddy", str(self.base / "packet.json"))
            read_code, read = self.run_agent("project", "Buddy")
        self.assertEqual((send_code, send["state"]), (1, "uncertain"))
        self.assertEqual((read_code, read["state"]), (1, "unavailable"))

    def test_malformed_remote_task_id_refuses_before_adapter(self):
        self.run_agent("register", "Buddy", "--client", str(self.client))
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(self.run_agent("status", "Buddy", "not-a-uuid")[0], 1)
        self.assertIn("exact server task UUID", error.getvalue())
        self.assertFalse(self.calls.exists())

    def test_bad_or_missing_reply_is_not_completion(self):
        self.run_agent("register", "Buddy", "--client", str(self.client))
        self.client.write_text("print('not json')\n")
        # Simulate a reviewed update through explicit removal and registration.
        self.run_agent("remove", "Buddy")
        self.run_agent("register", "Buddy", "--client", str(self.client))
        packet = self.base / "packet.json"
        packet.write_text("synthetic packet")
        code, result = self.run_agent("send", "Buddy", str(packet))
        self.assertEqual((code, result["state"]), (1, "uncertain"))
        self.assertIn("inspect", result["message"])

    def test_send_without_task_id_remains_uncertain(self):
        self.run_agent("register", "Buddy", "--client", str(self.client))
        code, result = self.run_agent("send", "Buddy", str(self.base / "packet.json"))
        self.assertEqual((code, result["state"]), (1, "uncertain"))
        self.assertIn("task ID", result["message"])

    def test_installed_dispatcher_offers_agent_without_enrollment(self):
        with mock.patch.object(agent, "_registry_path", return_value=self.registry):
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(["agent", "muse", "list"], command_alias_check=lambda: None)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["agents"], [])
        self.assertFalse(self.registry.exists())

    def test_installed_global_options_do_not_confuse_agent_helper(self):
        with mock.patch.object(agent, "_registry_path", return_value=self.registry):
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(["--json", "--repo", str(self.base), "agent", "muse", "list"],
                                command_alias_check=lambda: None)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["agents"], [])


if __name__ == "__main__":
    unittest.main()
