"""Recurring caller errors answered at the interface: accepted spellings and refusals that say what to change."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest import mock

SOURCE_DIR = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SOURCE_DIR))

from relay_core import cli  # noqa: E402
from relay_core.protocol import ValidationError, normalize_event  # noqa: E402
from relay_runtime import enrollment  # noqa: E402


class CallerFrictionTests(unittest.TestCase):
    def test_the_spellings_callers_reach_for_mean_the_command(self):
        parser = cli.build_parser()
        self.assertEqual(("acknowledge", 7), (lambda a: (a.command, a.seq))(cli.parse(parser, ["ack", "7"])))
        self.assertEqual(("roles", "driver"), (lambda a: (a.command, a.role))(cli.parse(parser, ["role", "driver"])))
        self.assertEqual("roles", cli.parse(parser, ["roles"]).command)

    def test_an_over_long_line_says_by_how_much(self):
        raw = {"kind": "work.intent", "agent": "claude", "session": "s1", "work_id": "w", "summary": "x" * 520}
        with self.assertRaisesRegex(ValidationError, r"summary exceeds 500 characters \(520 given, 20 over\)"):
            normalize_event(raw)
        help_text = cli.build_parser()._subparsers._group_actions[0].choices["signal"].format_help()
        self.assertIn("at most 500 characters; put detail in the artifact", " ".join(help_text.split()))

    def test_an_uncontrolled_ancestor_is_named_with_its_remedy(self):
        directory = enrollment._Directory(custody=mock.Mock(), path=Path("/mapped/home"), fd=-1, parent=None,
                                          initial=os.stat("/"))
        foreign = os.stat_result((0o40755, 0, 0, 0, 65534, 65534, 0, 0, 0, 0))
        with self.assertRaises(enrollment.EnrollmentError) as refused:
            directory.validate(foreign)
        message = str(refused.exception)
        self.assertIn("'/mapped/home' has owner uid 65534", message)
        self.assertIn("run multithread through the host's approved route outside it", message)


if __name__ == "__main__":
    unittest.main()
