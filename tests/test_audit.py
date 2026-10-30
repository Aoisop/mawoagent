"""Audit trail: read-only events, redaction, and failure diagnostics (I6 / T18)."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agents import audit as audit_module
from agents.audit import (
    REDACTED,
    AuditEvent,
    AuditLog,
    CallRecord,
    audit_path,
    read_events,
    summarize_arguments,
)
from agents.tool_runtime import ToolExecution


def _drain(coro):
    return asyncio.run(coro)


class SummaryTests(unittest.TestCase):
    def test_a_token_inside_a_command_string_is_redacted(self) -> None:
        # Key-name matching misses this: the secret is inside a value.
        text = summarize_arguments({"command": "curl -H 'Authorization: Bearer sk-live-ABCDEF1234567890'"})
        self.assertNotIn("sk-live-ABCDEF1234567890", text)
        self.assertIn(REDACTED, text)

    def test_secrets_are_redacted_wherever_they_appear(self) -> None:
        text = summarize_arguments({"token": "abc123", "nested": {"api_key": "k"}})
        self.assertIn(REDACTED, text)
        self.assertNotIn("abc123", text)
        self.assertNotIn('"k"', text)

    def test_long_arguments_are_truncated(self) -> None:
        text = summarize_arguments({"content": "x" * 5000})
        self.assertLessEqual(len(text), 240)
        self.assertTrue(text.endswith("..."))

    def test_plain_arguments_are_readable(self) -> None:
        text = summarize_arguments({"file_path": "a.txt"})
        self.assertIn("a.txt", text)


class LogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def events(self) -> list[dict]:
        path = audit_path(self.root)
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_a_call_writes_a_start_and_an_end(self) -> None:
        log = AuditLog(audit_path(self.root))
        record = CallRecord(log, session_id="s1", tool="read_file", arguments={"file_path": "a.txt"})
        record.start("allow")
        record.finish()

        phases = [event["phase"] for event in self.events()]
        self.assertEqual(phases, ["start", "end"])
        self.assertEqual(self.events()[0]["call_id"], self.events()[1]["call_id"])

    def test_outcome_and_error_are_recorded(self) -> None:
        log = AuditLog(audit_path(self.root))
        record = CallRecord(log, session_id="s1", tool="run_shell", arguments={"command": "ls"})
        record.start("confirm")
        record.fail("error", "boom")
        record.finish()

        end = self.events()[-1]
        self.assertEqual(end["outcome"], "error")
        self.assertEqual(end["error"], "boom")
        self.assertEqual(end["permission"], "confirm")

    def test_subscribers_see_a_frozen_event(self) -> None:
        log = AuditLog(audit_path(self.root))
        seen: list[AuditEvent] = []
        log.subscribe(seen.append)

        record = CallRecord(log, session_id="s1", tool="x", arguments={})
        record.start()
        record.finish()

        self.assertEqual(len(seen), 2)
        with self.assertRaises(Exception):
            seen[0].tool = "tampered"  # type: ignore[misc]

    def test_a_broken_subscriber_does_not_break_the_call(self) -> None:
        log = AuditLog(audit_path(self.root))
        log.subscribe(lambda event: 1 / 0)

        record = CallRecord(log, session_id="s1", tool="x", arguments={})
        record.start()
        record.finish()
        self.assertEqual(len(self.events()), 2)

    def test_unsubscribing_stops_delivery(self) -> None:
        log = AuditLog(audit_path(self.root))
        seen: list[AuditEvent] = []
        unsubscribe = log.subscribe(seen.append)
        unsubscribe()

        record = CallRecord(log, session_id="s1", tool="x", arguments={})
        record.start()
        self.assertEqual(seen, [])

    def test_an_unwritable_log_does_not_raise(self) -> None:
        blocker = self.root / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        log = AuditLog(blocker / "audit.jsonl")
        record = CallRecord(log, session_id="s1", tool="x", arguments={})
        record.start()
        record.finish()  # must not raise

    def test_the_log_rotates_when_it_grows(self) -> None:
        with mock.patch.object(audit_module, "MAX_LOG_BYTES", 200):
            log = AuditLog(audit_path(self.root))
            for _ in range(20):
                record = CallRecord(log, session_id="s1", tool="x", arguments={"a": "b"})
                record.start()
                record.finish()

        self.assertTrue(audit_path(self.root).with_suffix(".jsonl.1").exists())

    def test_rotation_keeps_older_generations_readable(self) -> None:
        with mock.patch.object(audit_module, "MAX_LOG_BYTES", 900):
            log = AuditLog(audit_path(self.root))
            for number in range(60):
                record = CallRecord(log, session_id="s1", tool=f"t{number}", arguments={})
                record.start()
                record.finish()

        self.assertTrue(audit_path(self.root).with_name("audit.jsonl.1").exists())
        # More is readable than the current file alone holds: the rotated
        # generations are part of the trail, not discarded history.
        current = len(audit_path(self.root).read_text(encoding="utf-8").splitlines())
        events = read_events(self.root, limit=200)
        self.assertGreater(len(events), current)

    def test_concurrent_appends_do_not_interleave(self) -> None:
        import threading

        log = AuditLog(audit_path(self.root))

        def writer(prefix: str) -> None:
            for number in range(40):
                record = CallRecord(log, session_id="s1", tool=f"{prefix}{number}", arguments={})
                record.start()
                record.finish()

        threads = [threading.Thread(target=writer, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        for line in audit_path(self.root).read_text(encoding="utf-8").splitlines():
            if line.strip():
                json.loads(line)  # every line is a complete record

    def test_read_events_returns_newest_first(self) -> None:
        log = AuditLog(audit_path(self.root))
        for number in range(3):
            record = CallRecord(log, session_id="s1", tool=f"t{number}", arguments={})
            record.start()
            record.finish()

        events = read_events(self.root, limit=2)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["tool"], "t2")

    def test_missing_log_reads_as_empty(self) -> None:
        self.assertEqual(read_events(self.root), [])


class _Host(ToolExecution):
    """Minimal host exposing the surface ToolExecution expects."""

    def __init__(self, root: Path, *, execute=None, permission_mode: str = "dontAsk"):
        self.tools = [{
            "name": "echo",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
        }, {
            "name": "write_file",
            "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}},
        }]
        self.permission_mode = permission_mode
        self._plan_file_path = None
        self.workspace = SimpleNamespace(root=root)
        self.session_id = "s1"
        self._execute = execute or (lambda name, inp: "echoed")

    async def _execute_tool_call(self, name, inp):
        result = self._execute(name, inp)
        return await result if asyncio.iscoroutine(result) else result

    async def _confirm_tool_call(self, name, permission):
        return True


class ToolIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def events(self) -> list[dict]:
        return read_events(self.root, limit=50)

    def test_a_successful_call_is_audited_and_returns_the_same_text(self) -> None:
        host = _Host(self.root)
        result = _drain(host._run_checked_tool("echo", {"text": "hi"}))

        self.assertEqual(result, "echoed")
        end = [event for event in self.events() if event["phase"] == "end"][0]
        self.assertEqual(end["outcome"], "ok")
        self.assertEqual(end["tool"], "echo")
        self.assertEqual(end["session_id"], "s1")

    def test_a_denied_call_is_marked_denied(self) -> None:
        host = _Host(self.root, permission_mode="plan")
        result = _drain(host._run_checked_tool("write_file", {"file_path": "a.txt", "content": "x"}))

        self.assertIn("denied", result.lower())
        end = [event for event in self.events() if event["phase"] == "end"][0]
        self.assertEqual(end["outcome"], "denied")

    def test_an_exception_is_diagnosable(self) -> None:
        def boom(name, inp):
            raise RuntimeError("kaboom")

        host = _Host(self.root, execute=boom)
        result = _drain(host._run_checked_tool("echo", {"text": "hi"}))

        self.assertIn("kaboom", result)
        end = [event for event in self.events() if event["phase"] == "end"][0]
        self.assertEqual(end["outcome"], "error")
        self.assertIn("kaboom", end["error"])

    def test_invalid_arguments_are_marked_invalid(self) -> None:
        host = _Host(self.root)
        result = _drain(host._run_checked_tool("echo", {"text": 5}))

        self.assertIn("invalid arguments", result)
        end = [event for event in self.events() if event["phase"] == "end"][0]
        self.assertEqual(end["outcome"], "invalid")

    def test_unknown_tool_is_marked_unavailable(self) -> None:
        host = _Host(self.root)
        _drain(host._run_checked_tool("nope", {}))

        end = [event for event in self.events() if event["phase"] == "end"][0]
        self.assertEqual(end["outcome"], "unavailable")

    def test_cancellation_is_marked_cancelled(self) -> None:
        async def scenario():
            host = _Host(self.root, execute=lambda name, inp: asyncio.sleep(5))
            task = asyncio.create_task(host._run_checked_tool("echo", {"text": "x"}))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        _drain(scenario())
        ends = [event for event in self.events() if event["phase"] == "end"]
        self.assertEqual(ends[0]["outcome"], "cancelled")


if __name__ == "__main__":
    unittest.main()
