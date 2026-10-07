"""Reviewing an exact Claude Code binary for restricted calls, and calls admitting only reviewed binaries."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from relay_runtime import claude_peer as claude_review  # noqa: E402

HELP = """Usage: claude [options]

Options:
  --print                     Print the response and exit
  --restricted                Restricted mode: ignores user, project and local
                              settings files
  --strict-mcp-config         Only use MCP servers from --mcp-config
  --tools <tools...>          Specify the list of available tools
"""

# A native stand-in: without --restricted it obeys the checkout's hooks and MCP servers, as Claude Code does.
FAKE = r'''
import json, os, pathlib, shlex, subprocess, sys
spec = json.loads(pathlib.Path(__file__).with_suffix(".json").read_text())
args = sys.argv[1:]
if args == ["--version"]:
    print(spec["version"] + " (Claude Code)"); raise SystemExit(0)
if args == ["--help"]:
    print(HELP_TEXT); raise SystemExit(0)
restricted = "--restricted" in args
sys.stdin.readline()
if (not restricted and spec.get("control_fires", True)) or (restricted and spec.get("restricted_fires")):
    for name in (".claude/settings.json", ".claude/settings.local.json"):
        for groups in json.loads(pathlib.Path(name).read_text()).get("hooks", {}).values():
            for group in groups:
                for hook in group["hooks"]:
                    subprocess.run(hook["command"], shell=True, check=False)
    for server in json.loads(pathlib.Path(".mcp.json").read_text())["mcpServers"].values():
        subprocess.run([server["command"], *[a.replace("sleep 20", "true") for a in server["args"]]], check=False)
print(json.dumps({"type": "system", "subtype": "init", "claude_code_version": spec.get("reported", spec["version"]),
                  "tools": [] if restricted else ["mcp__mail__send"],
                  "mcp_servers": [] if restricted else [{"name": "canary", "status": "connected"}],
                  "plugins": [{"name": p} for p in spec.get("plugins", ["cc-plugin-telemetry"])],
                  "agents": spec.get("agents", ["claude"])}))
print(json.dumps({"type": "result", "result": "ready", "is_error": False}))
'''


class ReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-claude-review-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.binary = self.base / "bin" / "claude"
        self.binary.parent.mkdir()
        # The surface strings sit in the file verbatim, as they do in a native binary.
        self.binary.write_text("#!" + sys.executable + "\n" + FAKE.replace("HELP_TEXT", repr(HELP))
                               + "".join("# " + token + "\n" for token in claude_review.SURFACE_TOKENS))
        self.binary.chmod(0o755)
        self.spec({})
        surface, _ = claude_review.surface(str(self.binary))
        anchor = {"version": "9.9.8", "surface_sha256": surface, "plugins": ["cc-plugin-telemetry"], "agents": ["claude"]}
        patch = mock.patch.dict(claude_review.BUILT_IN, {"0" * 64: anchor}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.out = self.base / "evidence"
        self.out.mkdir()

    def spec(self, changes):
        self.binary.with_suffix(".json").write_text(json.dumps({"version": "9.9.9", **changes}))

    def review(self):
        return claude_review.review(str(self.binary), self.out, "fixture-model")

    def test_a_binary_that_ignores_live_canaries_under_the_restricted_flags_passes(self):
        report = self.review()
        self.assertEqual(("pass", []), (report["verdict"], report["reasons"]), report)
        self.assertEqual(sorted(claude_review.CANARIES), report["control_fired"], "the control proves the canaries live")
        self.assertEqual([], report["restricted_fired"])
        self.assertEqual(("9.9.9", claude_review.binary_identity(str(self.binary))[1]),
                         (report["version"], report["binary_sha256"]))

    def test_every_departure_needs_review_and_is_never_a_pass(self):
        cases = {
            "restricted fires": ({"restricted_fires": True}, "fired under the restricted flags"),
            "silent control": ({"control_fires": False}, "silence would prove nothing"),
            "version mismatch": ({"reported": "9.9.10"}, "reported version differs"),
            "new plugin": ({"plugins": ["cc-plugin-telemetry", "cc-plugin-mail"]}, "no hand review covered"),
            "new agent": ({"agents": ["claude", "mailer"]}, "no hand review covered"),
        }
        for name, (changes, why) in cases.items():
            with self.subTest(case=name):
                self.spec(changes)
                report = self.review()
                self.assertEqual("needs_review", report["verdict"])
                self.assertTrue(any(why in reason for reason in report["reasons"]), report["reasons"])

    def test_a_changed_restricted_surface_needs_review(self):
        with mock.patch.dict(claude_review.BUILT_IN, {"0" * 64: {**claude_review.BUILT_IN["0" * 64],
                                                                 "surface_sha256": "f" * 64}}):
            report = self.review()
        self.assertEqual("needs_review", report["verdict"])
        self.assertIn("the restricted surface differs from every hand-reviewed one", report["reasons"])

    def test_the_surface_is_the_present_strings_and_our_flags_whole_help(self):
        _, value = claude_review.surface(str(self.binary))
        self.assertEqual(list(claude_review.SURFACE_TOKENS), value["tokens"])
        self.assertEqual("--restricted Restricted mode: ignores user, project and local settings files",
                         value["flags"]["--restricted"])
        self.assertNotIn("--add-dir", value["flags"])

    def test_recorded_reviews_are_read_through_the_launcher_and_unavailable_is_not_unreviewed(self):
        digest = "a" * 64
        launcher = self.base / "multithread"
        surface = claude_review.BUILT_IN["0" * 64]["surface_sha256"]
        def answer(**changes):
            meta = {"binary_sha256": digest, "version": "9.9.9", "surface_sha256": surface, "plugins": "none",
                    "agents": "claude", **changes}
            launcher.write_text("#!/bin/sh\necho '" + json.dumps({"reviews": [{"seq": 7, "meta": meta}]}) + "'\n")
            launcher.chmod(0o755)
        answer()
        self.assertEqual({"version": "9.9.9", "surface_sha256": surface, "plugins": [], "agents": ["claude"],
                          "source": "ledger:7"}, claude_review.reviewed(digest, launcher, self.base))
        # A record admits nothing a hand review did not cover, however it was written.
        for changes in ({"surface_sha256": "c" * 64}, {"agents": "claude,mailer"}, {"plugins": "cc-plugin-mail"}):
            with self.subTest(record=changes):
                answer(**changes)
                with self.assertRaisesRegex(claude_review.ReviewError, "no hand review covered"):
                    claude_review.reviewed(digest, launcher, self.base)
        answer()
        self.assertIsNone(claude_review.reviewed("d" * 64, launcher, self.base))
        launcher.write_text("#!/bin/sh\necho 'ledger unavailable' >&2\nexit 3\n")
        with self.assertRaisesRegex(claude_review.ReviewError, "could not be read"):
            claude_review.reviewed(digest, launcher, self.base)
        self.assertEqual("built_in", claude_review.reviewed("0" * 64, launcher, self.base)["source"],
                         "the hand-reviewed baseline needs no ledger")


if __name__ == "__main__":
    unittest.main()
