"""Recorded Claude Code reviews: attributed, append-only, and only for a pass whose control fired."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE_DIR = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SOURCE_DIR))

from relay_core.protocol import ValidationError, canonical_json, normalize_event  # noqa: E402
from relay_core.store import RelayStore  # noqa: E402

DIGEST = "b" * 64
REPORT = {
    "schema": 1, "provider": "claude", "binary_path": "/opt/claude/9.9.9", "binary_sha256": DIGEST,
    "version": "9.9.9", "surface_sha256": "c" * 64, "plugins": ["cc-plugin-telemetry"], "agents": ["claude", "Explore"],
    "control_fired": ["project-hook", "local-hook", "mcp-server"], "restricted_fired": [], "verdict": "pass",
    "reasons": [], "model": "fixture",
}


class ProviderReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="relay-provider-review-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True, capture_output=True, timeout=15)
        common = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                check=True, capture_output=True, text=True, timeout=15).stdout.strip()
        binding = mock.patch("relay_core.store._expected_workspace_binding", return_value=Path(common))
        binding.start()
        self.addCleanup(binding.stop)
        self.home = self.root / "state"

    def store(self):
        return RelayStore.open(repo=self.repo, state_home=self.home)

    def test_a_pass_is_recorded_attributed_and_found_by_its_exact_binary(self):
        with self.store() as store:
            receipt = store.provider_review(REPORT, agent="claude", session="reviewer-1")
            self.assertFalse(receipt["duplicate"])
            [event] = store.provider_reviews(DIGEST)
            self.assertEqual([], store.provider_reviews("d" * 64))
        self.assertEqual(("provider.reviewed", "claude", "reviewer-1"), (event["kind"], event["agent"], event["session"]))
        self.assertEqual({"provider": "claude", "binary_sha256": DIGEST, "version": "9.9.9", "surface_sha256": "c" * 64,
                          "plugins": "cc-plugin-telemetry", "agents": "Explore,claude",
                          "control_fired": "local-hook,mcp-server,project-hook", "restricted_fired": "none"},
                         event["meta"])
        digest = hashlib.sha256(canonical_json(REPORT).encode()).hexdigest()
        self.assertEqual(f"sha256:{digest}", event["artifact"], "the report itself is the evidence")

    def test_only_a_pass_whose_control_fired_every_canary_and_restricted_none_is_recorded(self):
        cases = {
            "needs review": ({"verdict": "needs_review"}, "only a passing"),
            "silent control": ({"control_fired": ["project-hook", "local-hook"]}, "control fired every canary"),
            "restricted fired": ({"restricted_fired": ["mcp-server"]}, "fired no canary"),
            "other provider": ({"provider": "codex"}, "claude only"),
            "short digest": ({"binary_sha256": "abc"}, "SHA-256"),
            "odd version": ({"version": "latest"}, "MAJOR.MINOR.PATCH"),
            "names not a list": ({"agents": "claude"}, "arrays of names"),
        }
        with self.store() as store:
            for name, (change, why) in cases.items():
                with self.subTest(case=name):
                    report = {**copy.deepcopy(REPORT), **change}
                    with self.assertRaisesRegex(ValidationError, why):
                        store.provider_review(report, agent="claude", session="reviewer-1")
            self.assertEqual([], store.provider_reviews(DIGEST))

    def test_a_review_is_written_only_by_its_dedicated_transaction(self):
        raw = {"kind": "provider.reviewed", "agent": "claude", "session": "s1", "summary": "forged",
               "artifact": "sha256:" + "e" * 64, "meta": {}}
        with self.assertRaisesRegex(ValidationError, "dedicated Multithread transaction"):
            normalize_event(raw)
        with self.store() as store:
            with self.assertRaisesRegex(ValidationError, "dedicated Multithread transaction"):
                store.emit(raw)
            # A recorded review reads back through every validating reader.
            store.provider_review(REPORT, agent="claude", session="reviewer-1")
            self.assertEqual("provider.reviewed", store.events(after=0, limit=10)[-1]["kind"])
            store.brief(agent="claude", session="reviewer-1")

    def test_the_command_line_records_from_stdin_and_shows_by_binary(self):
        # A child binds only to a ledger whose candidate copy lives inside it (see test_relay_decisions).
        package = self.repo / "src" / "relay_core"
        package.mkdir(parents=True)
        for name in ("__init__.py", "cli.py", "protocol.py", "store.py"):
            (package / name).write_bytes((SOURCE_DIR / "relay_core" / name).read_bytes())
        (self.repo / "src" / "relay.py").write_bytes((SOURCE_DIR / "relay.py").read_bytes())
        def run(*arguments, data=None):
            return subprocess.run([sys.executable, str(self.repo / "src" / "relay.py"), "--repo", str(self.repo),
                                   "--home", str(self.home), "--json", "provider-review", *arguments],
                                  input=data, capture_output=True, text=True, timeout=15,
                                  env={"PATH": "/usr/bin:/bin", "HOME": str(self.root)})
        import json
        recorded = run("record", "--agent", "claude", "--session", "reviewer-2", data=json.dumps(REPORT))
        self.assertEqual(0, recorded.returncode, recorded.stderr)
        refused = run("record", "--agent", "claude", "--session", "reviewer-2",
                      data=json.dumps({**REPORT, "verdict": "needs_review"}))
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("only a passing", refused.stderr)
        shown = run("show", "--binary-sha256", DIGEST)
        self.assertEqual(0, shown.returncode, shown.stderr)
        [event] = json.loads(shown.stdout)["reviews"]
        self.assertEqual(("reviewer-2", "9.9.9"), (event["session"], event["meta"]["version"]))


if __name__ == "__main__":
    unittest.main()
