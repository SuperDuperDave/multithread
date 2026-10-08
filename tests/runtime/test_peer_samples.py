"""Published sample records: what a peer call actually emits, so consumers test against it instead of fakes.

Each sample is a record this suite produces through the real CLI, normalized (paths, IDs, times). The test fails
when a live record's shape (its key paths and value types) drifts from the committed sample; regenerate with
MULTITHREAD_WRITE_PEER_SAMPLES=1 after an intended change, and say so in the release notes.
"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import shutil
import unittest
from unittest import mock

import test_claude_integration as integration
import test_claude_protocol as protocol
from relay_runtime import provider as peer

SAMPLES = Path(__file__).resolve().parents[2] / "docs" / "samples" / "peer"
WRITE = os.environ.get("MULTITHREAD_WRITE_PEER_SAMPLES") == "1"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")


def normalize(value, base):
    if isinstance(value, dict):
        return {key: normalize(item, base) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item, base) for item in value]
    if isinstance(value, str):
        value = value.replace(os.path.realpath(base), "/path/to").replace(str(base), "/path/to")
        return _TIME.sub("2026-01-01T00:00:00.000Z", _UUID.sub("00000000-0000-4000-8000-000000000000", value))
    return value


def shape(value):
    """Key paths and value types; numbers are one type, and a list is described by its first item."""
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return "null" if value is None else type(value).__name__


def paths(value, prefix=""):
    if isinstance(value, dict):
        return {path for key, item in value.items() for path in paths(item, f"{prefix}.{key}" if prefix else key)}
    if isinstance(value, list):
        return {f"{prefix}[]"} | (paths(value[0], f"{prefix}[]") if value else set())
    return {f"{prefix}: {value}"}


def png():
    import struct, zlib
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xdc\x1e\x1e")) + chunk(b"IEND", b""))


class PeerSampleTests(unittest.TestCase):
    # The restricted integration harness, borrowed without its own tests.
    Restricted = integration.RestrictedCallTests
    executable, configure = staticmethod(Restricted.executable), Restricted.configure
    plan_claude, invoke, restricted_steps = Restricted.plan_claude, Restricted.invoke, Restricted.restricted_steps

    def setUp(self):
        integration.ClaudeIntegrationTests.setUp(self)
        for name in ("_WSL_REG", "_WSL_CLAUDE_POLICY"):
            isolated = mock.patch.object(peer, name, Path("/nonexistent") / name)
            isolated.start()
            self.addCleanup(isolated.stop)
        review = mock.patch("relay_runtime.claude_peer.reviewed", return_value=protocol.REVIEWED)
        review.start()
        self.addCleanup(review.stop)

    def check(self, name, record):
        sample = SAMPLES / f"{name}.json"
        if WRITE:
            SAMPLES.mkdir(parents=True, exist_ok=True)
            sample.write_text(json.dumps(normalize(record, self.base), indent=2, sort_keys=True, ensure_ascii=False) + "\n")
            return
        published = json.loads(sample.read_text())
        live, kept = paths(shape(record)), paths(shape(published))
        self.assertEqual(kept, live, f"{sample.name} no longer matches what the CLI emits: added {sorted(live - kept)}, "
                                     f"removed {sorted(kept - live)}. Regenerate with MULTITHREAD_WRITE_PEER_SAMPLES=1 "
                                     "and note the change for consumers.")

    def fresh(self):
        shutil.rmtree(self.base / "stream-evidence", ignore_errors=True)

    def test_restricted_returned(self):
        self.fresh()
        code, value, _ = self.invoke(self.restricted_steps(), restricted=True)
        self.assertEqual((0, "returned", []), (code, value["state"], value["attachments"]))
        self.check("claude-restricted-returned", value)

    def test_restricted_returned_with_an_attachment(self):
        self.fresh()
        image = self.base / "sample.png"
        image.write_bytes(png())
        code, value, _ = self.invoke(self.restricted_steps(), restricted=True, extra=("--attach", str(image)))
        self.assertEqual((0, "returned", "image/png"), (code, value["state"], value["attachments"][0]["media_type"]))
        self.check("claude-restricted-returned-with-attachment", value)

    def test_restricted_withheld_after_a_fault(self):
        self.fresh()
        late = protocol.ClaudeProtocolTests.restricted(tools=["Bash"])
        code, value, _ = self.invoke(self.restricted_steps({"emit": late}), restricted=True)
        self.assertEqual(("uncertain", None), (value["state"], value["result"]))
        self.assertNotEqual(0, code)
        self.check("claude-restricted-withheld", value)

    def test_restricted_refused_before_launch(self):
        self.fresh()
        self.plan_claude()
        directory = self.base / "stream-evidence"
        output = io.StringIO()
        with (redirect_stdout(output), redirect_stderr(io.StringIO()),
              mock.patch.dict(os.environ, self.environment, clear=True),
              mock.patch.object(peer, "_managed_claude_sources", return_value=["/etc/claude-code"])):
            code = peer.peer_main(["claude", "--tools", "none", "--repo", str(self.repo), "--multithread", str(self.relay),
                                   "--provider", str(self.provider), "--task-file", str(self.task),
                                   "--output-dir", str(directory), "--json"])
        value = json.loads(output.getvalue())
        self.assertNotEqual(0, code)
        self.assertEqual(("unavailable", False, []), (value["state"], value["provider_started"], value["attachments"]))
        self.assertEqual(value, json.loads((directory / "result.json").read_text()), "the refusal's record is the output")
        self.check("claude-restricted-refused", value)


if __name__ == "__main__":
    unittest.main()
