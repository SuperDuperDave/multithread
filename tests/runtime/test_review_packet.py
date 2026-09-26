"""Private scoped review packets use only tracked, bounded Git text."""

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import provider, review_packet


class ReviewPacketTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="review-packet-test-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "tracked.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture.invalid", "commit", "-qm", "base"], check=True)
        (self.repo / "tracked.txt").write_text("after\n", encoding="utf-8")
        self.packet = self.base / "packet.txt"

    def run_packet(self, *extra, paths=("tracked.txt",)):
        output = io.StringIO()
        arguments = ["packet", "--repo", str(self.repo), "--output-file", str(self.packet),
                     "--json", *(item for path in paths for item in ("--path", path)), *extra]
        with redirect_stdout(output):
            code = provider.peer_main(arguments)
        return code, json.loads(output.getvalue())

    def test_packet_freezes_selected_diff_digest_and_refuses_overwrite(self):
        marker = self.base / "external-ran"
        subprocess.run(["git", "-C", str(self.repo), "config", "diff.external",
                        f"sh -c 'touch {marker}'"], check=True)
        code, summary = self.run_packet()
        self.assertEqual(0, code, summary)
        body = self.packet.read_bytes()
        diff = body.split(b"----- BEGIN DIFF -----\n", 1)[1].rsplit(b"----- END DIFF -----\n", 1)[0]
        self.assertEqual(hashlib.sha256(diff).hexdigest(), summary["diff_sha256"])
        self.assertEqual(len(diff), summary["diff_bytes"])
        self.assertIn(b"-before\n+after\n", diff)
        self.assertEqual(0o600, self.packet.stat().st_mode & 0o777)
        self.assertFalse(marker.exists())
        code, failed = self.run_packet()
        self.assertEqual(1, code)
        self.assertEqual("unavailable", failed["state"])
        self.assertEqual(body, self.packet.read_bytes())

    def test_untracked_binary_and_oversized_changes_are_refused_before_write(self):
        group = self.repo / "group"
        group.mkdir()
        (group / "tracked.txt").write_text("old\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "group/tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture.invalid", "commit", "-qm", "group"], check=True)
        (group / "tracked.txt").write_text("new\n", encoding="utf-8")
        (group / "untracked.txt").write_text("private\n", encoding="utf-8")
        code, result = self.run_packet(paths=("group",))
        self.assertEqual(1, code)
        self.assertIn("untracked", result["message"])
        (self.repo / "tracked.txt").write_bytes(b"binary\0change")
        code, result = self.run_packet()
        self.assertEqual(1, code)
        self.assertIn("binary", result["message"])
        (self.repo / "tracked.txt").write_text("after\n", encoding="utf-8")
        with mock.patch.object(review_packet, "_MAX_DIFF", 10):
            code, result = self.run_packet()
        self.assertEqual(1, code)
        self.assertIn("1 MiB", result["message"])
        self.assertFalse(self.packet.exists())

    def test_changed_worktree_during_inspection_is_refused(self):
        original = review_packet._git
        seen = 0

        def git(repo, *arguments):
            nonlocal seen
            result = original(repo, *arguments)
            if "--full-index" in arguments:
                seen += 1
                if seen == 1:
                    (self.repo / "tracked.txt").write_text("changed during inspection\n", encoding="utf-8")
            return result

        with mock.patch.object(review_packet, "_git", side_effect=git):
            code, result = self.run_packet()
        self.assertEqual(1, code)
        self.assertIn("changed during inspection", result["message"])
        self.assertFalse(self.packet.exists())

    def test_binary_change_after_numstat_is_refused(self):
        original = review_packet._git

        def git(repo, *arguments):
            result = original(repo, *arguments)
            if "--numstat" in arguments:
                (self.repo / "tracked.txt").write_bytes(b"binary\0after-numstat")
            return result

        with mock.patch.object(review_packet, "_git", side_effect=git):
            code, result = self.run_packet()
        self.assertEqual(1, code)
        self.assertIn("binary", result["message"])
        self.assertFalse(self.packet.exists())


if __name__ == "__main__":
    unittest.main()
