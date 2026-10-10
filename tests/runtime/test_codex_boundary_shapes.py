"""Synthetic malformed-boundary and terminal-publication witnesses."""
import json
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import provider
import test_codex_protocol as protocol


class BoundaryShapeTests(unittest.TestCase):
    setUp = protocol.CodexProtocolTests.setUp
    executable = staticmethod(protocol.CodexProtocolTests.executable)
    configure = protocol.CodexProtocolTests.configure
    invoke = protocol.CodexProtocolTests.invoke
    recorded_requests = protocol.CodexProtocolTests.recorded_requests

    def test_known_disallowed_early_activity_stops_without_waiting_for_inventory(self):
        cases = [(('--allow-plugins',), 'codex_apps'),
                 (('--allow-apps',), 'fixture-plugin'),
                 (('--allow-plugins', '--allow-apps'), 'bad' + chr(10)),
                 (('--allow-plugins', '--allow-apps'), [])]
        for flags, name in cases:
            with self.subTest(flags=flags, name=name):
                self.configure(before_mcp_response=[{
                    'method': 'mcpServer/startupStatus/updated',
                    'params': {'name': name, 'status': 'starting'}}],
                    mcp_stall_after_activity=True)
                started = time.monotonic()
                code, result, _ = self.invoke(*flags, '--timeout', '2')
                self.assertNotEqual(0, code, result)
                self.assertIn('MCP server activity', result['message'])
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertEqual('not_submitted', result['task_submission'])
                self.assertNotIn('turn/start', [x['method'] for x in self.recorded_requests()])

    def test_malformed_security_settings_revoke_after_completion_on_any_thread(self):
        base = protocol.settings_notification()["params"]["threadSettings"]
        shapes = [None, [], "settings"] + [
            dict(base, sandboxPolicy=policy) for policy in
            (None, [], "workspaceWrite", {"type": "readOnly", "networkAccess": None},
             {"type": "readOnly", "networkAccess": 0}, {"type": "readOnly", "networkAccess": "false"})]
        for thread in (protocol.THREAD, protocol.OTHER_THREAD):
            for settings in shapes:
                with self.subTest(thread=thread, settings=settings):
                    update = {"method": "thread/settings/updated",
                              "params": {"threadId": thread, "threadSettings": settings}}
                    self.configure(events=[protocol.item(), protocol.completed(), update])
                    code, result, _ = self.invoke()
                    self.assertNotEqual(0, code, result)
                    self.assertEqual(("uncertain", None), (result["state"], result["result"]))

    def test_malformed_mcp_identities_refuse_before_task_even_with_opt_in(self):
        cases = [(chr(10), "p"), ("notes", chr(10)), ("x" * 257, "p"),
                 ("notes", "p" * 257), ("bad" + chr(127) + "name", "p"), ("notes", "bad" + chr(0) + "id")]
        for name, plugin in cases:
            with self.subTest(name=name, plugin=plugin):
                self.configure(mcp_pages=[[{"name": name, "pluginId": plugin}]])
                code, result, _ = self.invoke("--allow-plugins")
                self.assertNotEqual(0, code, result)
                self.assertEqual("not_submitted", result["task_submission"])
                self.assertNotIn("turn/start", [x.get("method") for x in self.recorded_requests()])

    def test_mcp_page_overflow_refuses_before_task(self):
        self.configure(mcp_pages=[[{"name": f"synthetic-{i}", "pluginId": "fixture"}
                                   for i in range(101)]])
        code, result, _ = self.invoke("--allow-plugins")
        self.assertNotEqual(0, code, result)
        self.assertEqual("not_submitted", result["task_submission"])
        self.assertNotIn("turn/start", [x.get("method") for x in self.recorded_requests()])

    def test_exact_mcp_page_and_total_bounds_still_admit(self):
        for pages in (1, 5):
            with self.subTest(pages=pages):
                self.configure(mcp_pages=[[{"name": f"synthetic-{page}-{i}", "pluginId": "fixture"}
                                           for i in range(100)] for page in range(pages)])
                code, result, _ = self.invoke("--allow-plugins")
                self.assertEqual(0, code, result)
                self.assertEqual(protocol.ANSWER, result["result"])
                self.assertEqual(pages * 100, result["provider_plugins"]["server_count"])

    def test_terminal_receipt_write_failure_withdraws_capture_completion(self):
        original = provider._atomic_record
        def record(directory, name, value, **kwargs):
            if name == "result.json":
                raise OSError("synthetic terminal receipt failure")
            return original(directory, name, value, **kwargs)
        with mock.patch.object(provider, "_atomic_record", side_effect=record):
            code, result, directory = self.invoke("--capture-transport", "e" * 64)
        self.assertNotEqual(0, code)
        self.assertEqual(protocol.ANSWER, result["result"])
        self.assertEqual("incomplete", result["transport_capture"]["status"])
        self.assertNotIn("inventory_sha256", result["transport_capture"])
        self.assertFalse((directory / "result.json").exists())
        self.assertEqual("inventory_only", json.loads(
            (directory / "transport/inventory.json").read_text())["status"])
