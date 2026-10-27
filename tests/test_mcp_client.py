from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agents.mcp_client import McpConnection, McpManager


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        pass


class McpToolTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_cleans_pending_request_and_next_call_can_succeed(self) -> None:
        connection = McpConnection("demo", "unused")
        stdin = _FakeStdin()
        connection._process = SimpleNamespace(stdin=stdin)
        manager = McpManager()
        manager._connections["demo"] = connection

        with patch("agents.mcp_client.MCP_TOOL_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(TimeoutError, "mcp__demo__ping.*timed out"):
                await manager.call_tool("mcp__demo__ping", {})

        self.assertEqual(connection._pending, {})
        # The request itself, then the cancellation notice the server needs in
        # order to stop working on it.
        self.assertEqual(len(stdin.writes), 2)
        self.assertIn(b"notifications/cancelled", stdin.writes[1])

        async def respond() -> None:
            while not connection._pending:
                await asyncio.sleep(0)
            request_id, future = next(iter(connection._pending.items()))
            connection._pending.pop(request_id)
            future.set_result(
                {"content": [{"type": "text", "text": "ok"}]}
            )

        responder = asyncio.create_task(respond())
        self.assertEqual(await manager.call_tool("mcp__demo__ping", {}), "ok")
        await responder
        self.assertEqual(connection._pending, {})


class CommandResolutionTests(unittest.TestCase):
    def test_missing_command_is_reported_clearly(self) -> None:
        from agents.mcp_client import _resolve_command

        with self.assertRaises(FileNotFoundError) as caught:
            _resolve_command("definitely-not-a-real-binary-xyz", [])
        self.assertIn("not found", str(caught.exception))

    def test_a_real_command_resolves_to_its_path(self) -> None:
        import sys

        from agents.mcp_client import _resolve_command

        command, args = _resolve_command(sys.executable, ["-c", "print(1)"])
        self.assertTrue(command)
        self.assertEqual(args, ["-c", "print(1)"])

    @unittest.skipUnless(__import__("os").name == "nt", "batch shims exist only on Windows")
    def test_batch_shim_is_started_through_cmd(self) -> None:
        import tempfile
        from pathlib import Path

        from agents.mcp_client import _resolve_command

        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "fake-tool.cmd"
            shim.write_text("@echo off\r\necho hi\r\n", encoding="utf-8")
            command, args = _resolve_command(str(shim), ["--version"])

        self.assertTrue(command.lower().endswith("cmd.exe"))
        self.assertEqual(args[:2], ["/c", str(shim)])
        self.assertEqual(args[2:], ["--version"])


if __name__ == "__main__":
    unittest.main()
