"""Exact-session recipients, bounded backlog and durable consumption."""
import sys
import json
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).parent))
import test_relay as fixtures
from relay_core.protocol import ConflictError, session_target
from relay_core import cli

class SessionInboxTests(fixtures.RelayTestCase):
    def handoff(self, store, index, target=None):
        return store.emit(self.valid_event(
            id=f"evt:session-handoff-{index:04d}", kind="work.handoff",
            target=target, artifact="sha256:" + "a" * 64,
            summary=f"Synthetic handoff {index}",
        ))["event"]["seq"]

    def test_exact_session_priority_and_complete_cursor_pages(self):
        with self.open_store() as store:
            generic = [self.handoff(store, i, "codex") for i in range(8)]
            first = self.handoff(store, 20, session_target("codex", "one"))
            second = self.handoff(store, 21, session_target("codex", "one"))
            other = self.handoff(store, 22, session_target("codex", "two"))
            before = store.events()
            brief = store.brief("codex", session="one")
            self.assertEqual([second, first, *generic[:3]],
                             [item["seq"] for item in brief["pending_signals"]])
            self.assertEqual(10, brief["pending_count"])
            self.assertEqual(2, brief["targeted_count"])
            self.assertTrue(brief["truncated"]["pending_signals"])
            cursor, found = 0, []
            while True:
                page = store.inbox("codex", session="one", after=cursor, limit=3)
                found += [item["seq"] for item in page["pending_signals"]]
                if not page["has_more"]:
                    break
                self.assertGreater(page["next_after"], cursor)
                cursor = page["next_after"]
            self.assertEqual([*generic, first, second], found)
            self.assertNotIn(other, found)
            self.assertEqual(before, store.events())
            context = cli._render_brief(brief)
            self.assertIn("pending=10 exact_session=2", context)
            self.assertIn("inbox --agent", context)

    def test_wrong_session_refuses_without_write_and_exact_ack_is_idempotent(self):
        with self.open_store() as store:
            seq = self.handoff(store, 1, session_target("codex", "one"))
            before = store.events()
            for agent, session in (("codex", "two"), ("claude", "one")):
                with self.assertRaises(ConflictError):
                    store.acknowledge(seq, agent=agent, session=session)
            self.assertEqual(before, store.events())
            receipt = store.acknowledge(seq, agent="codex", session="one")
            self.assertEqual("codex", receipt["event"]["agent"])
            self.assertEqual(session_target("codex", "one"), receipt["event"]["target"])
            self.assertTrue(store.acknowledge(seq, agent="codex", session="one")["duplicate"])
            self.assertEqual([], store.brief("codex", session="one")["pending_signals"])
            self.assertEqual([], store.brief("codex", session="two")["pending_signals"])

    def test_generic_and_broadcast_consumption_remains_agent_scoped(self):
        with self.open_store() as store:
            generic = self.handoff(store, 1, "codex")
            broadcast = self.handoff(store, 2)
            store.acknowledge(generic, agent="codex", session="one")
            store.acknowledge(broadcast, agent="codex", session="one")
            self.assertTrue(store.acknowledge(generic, agent="codex", session="two")["duplicate"])
            self.assertEqual([], store.brief("codex", session="two")["pending_signals"])
            self.assertEqual([broadcast], [item["seq"] for item in store.inbox("claude")["pending_signals"]])

    def test_cli_receives_separate_actor_and_session(self):
        with self.open_store() as store:
            seq = self.handoff(store, 1, session_target("codex", "one"))
        response = self.run_cli("inbox", "--agent", "codex", "--session", "one", "--limit", "1")
        self.assertEqual(0, response.returncode, response.stderr)
        result = json.loads(response.stdout)
        self.assertEqual([seq], [item["seq"] for item in result["pending_signals"]])
        response = self.run_cli("inbox", "--agent", "codex", "--session", "two")
        self.assertEqual(0, response.returncode, response.stderr)
        self.assertEqual([], json.loads(response.stdout)["pending_signals"])

    def test_legacy_generic_colon_label_cannot_consume_an_exact_recipient(self):
        with self.open_store() as store:
            exact = self.handoff(store, 1, session_target("codex", "one"))
            generic = self.handoff(store, 2, "codex:one")
            self.assertEqual([generic], [e["seq"] for e in store.inbox("codex:one", session="two")["pending_signals"]])
            before = store.events()
            with self.assertRaises(ConflictError):
                store.acknowledge(exact, agent="codex:one", session="two")
            self.assertEqual(before, store.events())
            store.acknowledge(generic, agent="codex:one", session="two")
            self.assertEqual([exact], [e["seq"] for e in store.inbox("codex", session="one")["pending_signals"]])
            nested = self.handoff(store, 3, session_target("codex", "one:two"))
            self.assertNotEqual(session_target("codex", "one:two"), session_target("codex:one", "two"))
            self.assertEqual([nested], [e["seq"] for e in store.inbox("codex", session="one:two")["pending_signals"]])
            store.acknowledge(nested, agent="codex", session="one:two")

    def test_signal_cli_encodes_exact_target_without_changing_actor(self):
        result = self.run_cli("signal", "work.handoff", "--agent", "sender", "--session", "author",
                              "--target", "codex:one", "--target-session", "two:three",
                              "--artifact", "sha256:" + "a" * 64, "--summary", "Synthetic exact delivery")
        self.assertEqual(0, result.returncode, result.stderr)
        event = json.loads(result.stdout)["event"]
        self.assertEqual(("sender", "author", session_target("codex:one", "two:three")),
                         (event["agent"], event["session"], event["target"]))
        refused = self.run_cli("signal", "work.handoff", "--agent", "sender", "--session", "author",
                               "--target-session", "one", "--artifact", "sha256:" + "a" * 64,
                               "--summary", "Missing agent")
        self.assertNotEqual(0, refused.returncode)

    def test_direct_exact_targets_normalize_json_spacing_and_refuse_malformed_pairs(self):
        from relay_core.protocol import ValidationError
        with self.open_store() as store:
            seq = self.handoff(store, 1, json.dumps(["codex", "one"]))
            self.assertEqual([seq], [e["seq"] for e in store.inbox("codex", session="one")["pending_signals"]])
            store.acknowledge(seq, agent="codex", session="one")
            before = store.events()
            for target in ('[broken', '["codex"]', '["codex",null]', '["codex","one","two"]'):
                with self.subTest(target=target), self.assertRaises(ValidationError):
                    self.handoff(store, 2, target)
            self.assertEqual(before, store.events())

    def test_long_valid_session_keeps_generic_inbox_and_ack_compatible(self):
        session = "s" * 200
        with self.open_store() as store:
            seq = self.handoff(store, 1, "codex")
            self.assertEqual([seq], [item["seq"] for item in store.brief("codex", session=session)["pending_signals"]])
            receipt = store.acknowledge(seq, agent="codex", session=session)
            self.assertEqual(session, receipt["event"]["session"])
            self.assertEqual([], store.inbox("codex", session=session)["pending_signals"])

    def test_maximum_agent_and_session_pair_can_receive_and_ack_exact_target(self):
        agent, session = "a" * 200, "s" * 200
        with self.open_store() as store:
            seq = self.handoff(store, 1, session_target(agent, session))
            self.assertEqual([seq], [item["seq"] for item in store.inbox(agent, session=session)["pending_signals"]])
            receipt = store.acknowledge(seq, agent=agent, session=session)
            self.assertEqual(session_target(agent, session), receipt["event"]["target"])
            self.assertEqual([], store.inbox(agent, session=session)["pending_signals"])

if __name__ == "__main__":
    unittest.main()
