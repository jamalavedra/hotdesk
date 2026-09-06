import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hotdesk.config import load_config
from hotdesk.state import ControlConflict, State


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "hotdesk.toml"
        path.write_text(
            '[profiles.one]\n[profiles.two]\n[desktops.one]\nprofile="one"\n[desktops.two]\nprofile="two"\n'
        )
        self.config = load_config(path)
        self.state = State(self.config)
        self.addCleanup(lambda: self.state.db.close())

    def agent(self, name="one", owner="Agent"):
        token = self.state.register(name, owner, "Test task")
        return token, self.state.authenticate(token, name)

    def test_workspace_scoping_exclusive_ownership_and_independent_desktops(self):
        token, first = self.agent()
        _, second = self.agent(owner="Another agent")
        _, other = self.agent("two")
        with self.assertRaises(ControlConflict):
            self.state.authenticate(token, "two")
        self.state.acquire(first)
        with self.assertRaises(ControlConflict):
            self.state.acquire(second)
        self.assertEqual(self.state.acquire(other)["state"], "reserved")
        operation = self.state.begin(first, "click")
        with self.assertRaises(ControlConflict):
            self.state.release(first)
        self.state.finish(operation, "succeeded")
        self.state.release(first)
        self.assertEqual(self.state.acquire(second)["owner"], "Another agent")
        self.assertNotIn(token, str(self.state.db.execute("SELECT * FROM agents").fetchall()))
        self.assertNotIn("agent", self.state.public("one"))

    def test_expiry_rejects_stale_owner_and_preserves_uncertain_operations(self):
        _, first = self.agent()
        with patch("hotdesk.state.time.time", return_value=1000):
            self.state.acquire(first, seconds=10)
        with patch("hotdesk.state.time.time", return_value=1011):
            with self.assertRaises(ControlConflict):
                self.state.check(first)
            self.assertEqual(self.state.public("one")["state"], "idle")
            self.state.acquire(first, seconds=10)
            operation = self.state.begin(first, "shell")
        with patch("hotdesk.state.time.time", return_value=1022):
            self.assertEqual(self.state.public("one")["state"], "recovery")
            with self.assertRaises(ControlConflict):
                self.state.acquire(first)
            self.state.finish(operation, "succeeded")
            self.assertEqual(self.state.public("one")["state"], "recovery")

    def test_restart_invalidates_credentials_and_recovers_inflight_work(self):
        token, first = self.agent()
        self.state.acquire(first)
        operation = self.state.begin(first, "shell")
        self.state.set_mode("two", "human")
        self.state.db.close()
        self.state = State(self.config)
        self.assertEqual(self.state.public("one")["state"], "recovery")
        self.assertEqual(self.state.public("two")["state"], "human")
        self.assertEqual(self.state.history()["operations"][0]["outcome"], "unknown")
        self.assertEqual(self.state.history()["operations"][0]["id"], operation)
        with self.assertRaises(ControlConflict):
            self.state.authenticate(token, "one")
        self.assertEqual(self.state.path.stat().st_mode & 0o777, 0o600)

    def test_unknown_outcomes_block_reassignment_and_history_is_bounded(self):
        _, agent = self.agent()
        self.state.acquire(agent)
        operation = self.state.begin(agent, "browser_submit")
        self.state.finish(operation, "unknown")
        with self.assertRaises(ControlConflict):
            self.state.acquire(agent)
        for index in range(510):
            self.state.event("one", f"event {index}")
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 500)
        self.assertEqual(len(self.state.history()["events"]), 100)
        self.assertNotIn("agent", self.state.history()["operations"][0])

    def test_release_is_idempotent_without_releasing_another_owner(self):
        _, first = self.agent()
        self.assertEqual(self.state.release(first)["state"], "idle")
        self.state.acquire(first)
        self.assertEqual(self.state.release(first)["state"], "idle")
        self.assertEqual(self.state.release(first)["state"], "idle")
        with patch("hotdesk.state.time.time", return_value=1000):
            self.state.acquire(first, seconds=10)
        with patch("hotdesk.state.time.time", return_value=1011):
            self.assertEqual(self.state.release(first)["state"], "idle")
        _, second = self.agent(owner="Other agent")
        self.state.acquire(second)
        with self.assertRaises(ControlConflict):
            self.state.release(first)
        self.assertEqual(self.state.check(second)["owner"], "Other agent")
        for mode in ("human", "recovery"):
            self.state.set_mode("one", mode)
            with self.assertRaises(ControlConflict):
                self.state.release(first)


if __name__ == "__main__":
    unittest.main()
