"""Per-turn snapshots, diff and undo (iteration I2 / T06 and T07)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import snapshots, tools
from agents.snapshots import (
    begin_turn,
    build_diff,
    current_recorder,
    finish_turn,
    latest_turn,
    list_turns,
    mark_interrupted,
    snapshot_scope,
    undo_turn,
)


class SnapshotTestCase(unittest.TestCase):
    session = "session-under-test"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.addCleanup(setattr, snapshots, "MAX_FILE_BYTES", snapshots.MAX_FILE_BYTES)
        self.addCleanup(setattr, snapshots, "MAX_TURNS_PER_SESSION", snapshots.MAX_TURNS_PER_SESSION)

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def start(self) -> tuple:
        return begin_turn(self.session, self.root)

    def finish(self, recorder, token, status: str = "complete"):
        return finish_turn(recorder, token, status=status)


class RecordingTests(SnapshotTestCase):
    def test_first_change_keeps_the_original_bytes(self) -> None:
        path = self.write("notes.txt", "first\n")
        recorder, token = self.start()
        recorder.record(path)
        path.write_text("second\n", encoding="utf-8")
        turn = self.finish(recorder, token)

        entry = turn.entries[0]
        self.assertTrue(entry.existed_before)
        self.assertNotEqual(entry.before_sha, entry.after_sha)

    def test_repeated_changes_in_one_turn_keep_the_first_image(self) -> None:
        path = self.write("notes.txt", "first\n")
        recorder, token = self.start()
        recorder.record(path)
        path.write_text("second\n", encoding="utf-8")
        recorder.record(path)
        path.write_text("third\n", encoding="utf-8")
        turn = self.finish(recorder, token)

        self.assertEqual(len(turn.entries), 1)
        blob = turn.directory / "blobs" / turn.entries[0].before_sha
        self.assertEqual(blob.read_text(encoding="utf-8"), "first\n")

    def test_runtime_state_is_never_snapshotted(self) -> None:
        path = self.write(".mawoagent/settings.json", "{}\n")
        recorder, token = self.start()
        recorder.record(path)
        turn = self.finish(recorder, token)
        self.assertEqual(turn.entries, [])

    def test_oversized_files_are_skipped_with_a_note(self) -> None:
        path = self.write("big.log", "x" * 200)
        snapshots.MAX_FILE_BYTES = 64
        recorder, token = self.start()
        recorder.record(path)
        path.write_text("y" * 300, encoding="utf-8")
        turn = self.finish(recorder, token)

        self.assertEqual(len(turn.entries), 1)
        self.assertIn("larger than the snapshot limit", turn.entries[0].note)
        self.assertIsNone(turn.entries[0].before_sha)

    def test_files_outside_the_workspace_are_ignored(self) -> None:
        outside = Path(self.tmp.name).parent / "outside-snapshot-probe.txt"
        outside.write_text("x\n", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))

        recorder, token = self.start()
        recorder.record(outside)
        turn = self.finish(recorder, token)
        self.assertEqual(turn.entries, [])

    def test_context_binding_is_reset_after_a_turn(self) -> None:
        recorder, token = self.start()
        self.assertIs(current_recorder(), recorder)
        self.finish(recorder, token)
        self.assertIsNone(current_recorder())

    def test_scope_helper_restores_the_previous_binding(self) -> None:
        recorder, token = self.start()
        self.addCleanup(recorder.finish)
        self.addCleanup(lambda: snapshots._current_recorder.reset(token))
        with snapshot_scope(None):
            self.assertIsNone(current_recorder())
        self.assertIs(current_recorder(), recorder)


class DiffTests(SnapshotTestCase):
    def test_diff_shows_added_and_removed_lines(self) -> None:
        path = self.write("app.py", "alpha\nbeta\n")
        recorder, token = self.start()
        recorder.record(path)
        path.write_text("alpha\ngamma\n", encoding="utf-8")
        turn = self.finish(recorder, token)

        diff = build_diff(turn)
        self.assertIn("-beta", diff)
        self.assertIn("+gamma", diff)
        self.assertIn("a/app.py", diff)

    def test_new_file_diff_is_against_dev_null(self) -> None:
        recorder, token = self.start()
        recorder.record(self.root / "new.txt")
        self.write("new.txt", "hello\n")
        turn = self.finish(recorder, token)

        diff = build_diff(turn)
        self.assertIn("/dev/null", diff)
        self.assertIn("+hello", diff)

    def test_binary_changes_are_reported_without_a_text_diff(self) -> None:
        path = self.root / "image.bin"
        path.write_bytes(b"\x00\x01\x02")
        recorder, token = self.start()
        recorder.record(path)
        path.write_bytes(b"\x00\x01\x03\x04")
        turn = self.finish(recorder, token)

        self.assertIn("binary", build_diff(turn))


class UndoTests(SnapshotTestCase):
    def test_undo_restores_the_previous_content(self) -> None:
        path = self.write("app.py", "keep me\n")
        recorder, token = self.start()
        recorder.record(path)
        path.write_text("overwritten\n", encoding="utf-8")
        turn = self.finish(recorder, token)

        result = undo_turn(turn, self.root)
        self.assertEqual(result["restored"], ["app.py"])
        self.assertEqual(path.read_text(encoding="utf-8"), "keep me\n")

    def test_undo_removes_files_created_during_the_turn(self) -> None:
        recorder, token = self.start()
        recorder.record(self.root / "created.txt")
        path = self.write("created.txt", "new\n")
        turn = self.finish(recorder, token)

        result = undo_turn(turn, self.root)
        self.assertEqual(result["removed"], ["created.txt"])
        self.assertFalse(path.exists())

    def test_undo_refuses_when_the_file_changed_after_the_turn(self) -> None:
        path = self.write("app.py", "original\n")
        recorder, token = self.start()
        recorder.record(path)
        path.write_text("from the turn\n", encoding="utf-8")
        turn = self.finish(recorder, token)

        path.write_text("edited afterwards\n", encoding="utf-8")
        result = undo_turn(turn, self.root)

        self.assertEqual(result["conflicts"], ["app.py"])
        self.assertEqual(result["restored"], [])
        self.assertEqual(path.read_text(encoding="utf-8"), "edited afterwards\n")

    def test_undo_reports_files_deleted_after_the_turn_as_conflicts(self) -> None:
        path = self.write("app.py", "original\n")
        recorder, token = self.start()
        recorder.record(path)
        path.write_text("from the turn\n", encoding="utf-8")
        turn = self.finish(recorder, token)

        path.unlink()
        result = undo_turn(turn, self.root)
        self.assertEqual(result["conflicts"], ["app.py"])

    def test_undo_is_idempotent_after_restoring(self) -> None:
        path = self.write("app.py", "original\n")
        recorder, token = self.start()
        recorder.record(path)
        path.write_text("from the turn\n", encoding="utf-8")
        turn = self.finish(recorder, token)

        undo_turn(turn, self.root)
        second = undo_turn(turn, self.root)
        self.assertEqual(second["restored"], [])
        self.assertEqual(second["conflicts"], ["app.py"])
        self.assertEqual(path.read_text(encoding="utf-8"), "original\n")


class ToolIntegrationTests(SnapshotTestCase):
    """The real file tools, not a reimplementation of them."""

    def test_write_file_is_undone_through_the_turn_snapshot(self) -> None:
        path = self.write("doc.md", "before\n")
        recorder, token = self.start()
        try:
            tools._write_file({"file_path": str(path), "content": "after\n"})
        finally:
            turn = self.finish(recorder, token)

        self.assertEqual(path.read_text(encoding="utf-8"), "after\n")
        diff = build_diff(turn)
        self.assertIn("-before", diff)
        self.assertIn("+after", diff)

        result = undo_turn(turn, self.root)
        self.assertEqual(result["restored"], ["doc.md"])
        self.assertEqual(path.read_text(encoding="utf-8"), "before\n")

    def test_edit_file_is_captured_too(self) -> None:
        path = self.write("doc.md", "alpha\nbeta\n")
        tools._read_file({"file_path": str(path)})
        recorder, token = self.start()
        try:
            tools._edit_file({"file_path": str(path), "old_string": "beta", "new_string": "gamma"})
        finally:
            turn = self.finish(recorder, token)

        result = undo_turn(turn, self.root)
        self.assertEqual(result["restored"], ["doc.md"])
        self.assertEqual(path.read_text(encoding="utf-8"), "alpha\nbeta\n")


class LifecycleTests(SnapshotTestCase):
    def test_open_turn_is_reclassified_when_a_new_one_starts(self) -> None:
        abandoned, token = self.start()
        abandoned.record(self.write("left.txt", "x\n"))
        # Simulate a process that died before finishing: no finish() call.
        self.addCleanup(lambda: snapshots._current_recorder.reset(token))

        fresh, fresh_token = self.start()
        fresh.record(self.write("fresh.txt", "y\n"))
        self.finish(fresh, fresh_token)

        statuses = {turn.turn_id: turn.status for turn in list_turns(self.session, self.root)}
        self.assertEqual(statuses[abandoned.turn_id], "interrupted")
        self.assertEqual(statuses[fresh.turn_id], "complete")

    def test_interrupted_turns_stay_visible_with_their_status(self) -> None:
        # An interrupted turn is still useful to inspect and to undo: whatever
        # cannot be restored safely shows up as a conflict.
        abandoned, token = self.start()
        abandoned.record(self.write("touched.txt", "x\n"))
        self.addCleanup(lambda: snapshots._current_recorder.reset(token))
        mark_interrupted(self.session, self.root)

        turn = latest_turn(self.session, self.root)
        self.assertIsNotNone(turn)
        self.assertEqual(turn.status, "interrupted")

    def test_retention_keeps_only_the_newest_turns(self) -> None:
        snapshots.MAX_TURNS_PER_SESSION = 2
        for index in range(4):
            recorder, token = begin_turn(self.session, self.root)
            recorder.record(self.root / f"file{index}.txt")
            self.write(f"file{index}.txt", "x\n")
            finish_turn(recorder, token)

        turns = list_turns(self.session, self.root)
        self.assertEqual([turn.index for turn in turns], [3, 4])

    def test_meta_is_readable_json_on_disk(self) -> None:
        path = self.write("a.txt", "x\n")
        recorder, token = self.start()
        recorder.record(path)
        turn = self.finish(recorder, token)

        meta = json.loads((turn.directory / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "complete")
        self.assertEqual(meta["entries"][0]["path"], "a.txt")
        self.assertIsNotNone(meta["finished_at"])

    def test_turn_without_file_changes_writes_nothing(self) -> None:
        recorder, token = self.start()
        turn = self.finish(recorder, token)

        self.assertEqual(turn.status, "empty")
        self.assertEqual(turn.entries, [])
        self.assertIsNone(turn.directory)
        self.assertEqual(list_turns(self.session, self.root), [])

    def test_finish_twice_does_not_append_entries(self) -> None:
        recorder, token = self.start()
        self.write("x.txt", "1\n")
        first = self.finish(recorder, token)
        second = self.finish(recorder, token)
        self.assertEqual(len(first.entries), len(second.entries))


class ChatIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """A real chat turn, with only the model stream replaced."""

    @staticmethod
    def _tool_response(path: Path) -> dict:
        return {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "type": "function",
                        "id": "call-1",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"file_path": str(path), "content": "after\n"}),
                        },
                    }],
                },
            }]
        }

    @staticmethod
    def _final_response() -> dict:
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}]}

    async def test_chat_turn_is_snapshotted_and_undone(self) -> None:
        from agents.agent import Agent
        from agents.workspace import WorkspaceContext
        from unittest.mock import AsyncMock, Mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "created.md"
            with patch("agents.agent.build_system_prompt", return_value="test"):
                agent = Agent(
                    api_base="https://example.test/v1",
                    api_key="fake",
                    custom_system_prompt="test",
                    enable_mcp=False,
                    enable_memory_prefetch=False,
                    enable_online_evolution=False,
                    enable_folding=False,
                    permission_mode="acceptEdits",
                    workspace=WorkspaceContext(root=root, workspace_only_writes=False),
                )
            self.addAsyncCleanup(agent.aclose)
            agent._auto_save = Mock()
            agent._call_openai_stream = AsyncMock(
                side_effect=[self._tool_response(target), self._final_response()]
            )

            await agent.chat("create the file")

            self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
            turn = latest_turn(agent.session_id, root)
            self.assertIsNotNone(turn)
            self.assertEqual(turn.status, "complete")
            self.assertEqual([entry.path for entry in turn.entries], ["created.md"])
            self.assertFalse(turn.entries[0].existed_before)

            result = undo_turn(turn, root)
            self.assertEqual(result["removed"], ["created.md"])
            self.assertFalse(target.exists())


class WebEndpointTests(unittest.TestCase):
    def test_diff_and_undo_are_reachable_over_the_api(self) -> None:
        from agents.web import WebApp
        from agents.workspace import WorkspaceContext

        app = WebApp(options={"api_key": None, "model": "test"})
        self.addCleanup(app.close)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app.workspace = WorkspaceContext(root=root)
            state = app.call(app.dispatch("POST", "/api/sessions", {}))
            session = state["id"]

            recorder, token = begin_turn(session, root)
            recorder.record(root / "a.txt")
            (root / "a.txt").write_text("x\n", encoding="utf-8")
            finish_turn(recorder, token)

            diff = app.call(app.dispatch("GET", f"/api/sessions/{session}/diff"))
            self.assertEqual(diff["turn"], 1)
            self.assertIn("+x", diff["diff"])

            undone = app.call(app.dispatch("POST", f"/api/sessions/{session}/undo", {"n": 1}))
            self.assertEqual(undone["removed"], ["a.txt"])
            self.assertFalse((root / "a.txt").exists())


if __name__ == "__main__":
    unittest.main()
