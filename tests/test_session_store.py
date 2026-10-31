"""Session ids become file names; both directions must agree (C28)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents import session


class SessionIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(session, "SESSION_DIR", Path(self.tmp.name) / "sessions")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_normal_id_round_trips(self) -> None:
        session.save_session("20261028-abc_1", {"messages": []})
        self.assertEqual(session.load_session("20261028-abc_1")["messages"], [])

    def test_a_traversal_id_is_refused_on_write(self) -> None:
        for bad in ("../escape", "a/b", "a\\b", "", "x.json"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    session.save_session(bad, {})

    def test_a_traversal_id_is_refused_on_read(self) -> None:
        self.assertIsNone(session.load_session("../../etc/passwd"))

    def test_the_folded_memory_writer_uses_the_same_rule(self) -> None:
        with mock.patch.object(session, "get_project_session_dir", return_value=Path(self.tmp.name)):
            with self.assertRaises(ValueError):
                session.save_folded_session_memory("../escape", {"x": 1})


if __name__ == "__main__":
    unittest.main()
