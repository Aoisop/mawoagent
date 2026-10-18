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
        self.assertEqual(len(stdin.writes), 1)

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


if __name__ == "__main__":
    unittest.main()
