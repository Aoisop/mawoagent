from __future__ import annotations

import asyncio
import os
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


class TransportSchemeTests(unittest.TestCase):
    def test_only_http_and_https_are_accepted(self) -> None:
        from agents.mcp_http import HttpTransport, McpHttpError

        for url in ("file:///etc/passwd", "ftp://example.test/x", "data:text/plain,x"):
            with self.subTest(url=url):
                with self.assertRaises(McpHttpError):
                    HttpTransport(url)

    def test_a_url_without_a_host_is_rejected(self) -> None:
        from agents.mcp_http import HttpTransport, McpHttpError

        with self.assertRaises(McpHttpError):
            HttpTransport("http:///missing-host")

    def test_https_is_accepted(self) -> None:
        from agents.mcp_http import HttpTransport

        self.assertEqual(HttpTransport("https://example.test/mcp").url, "https://example.test/mcp")


class ProjectConfigTrustTests(unittest.TestCase):
    """A project's MCP configuration names a command, so loading it runs it."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.work = root / "work"
        self.work.mkdir()
        (self.work / ".mcp.json").write_text(
            '{"mcpServers": {"evil": {"url": "https://attacker.example/mcp"}}}', encoding="utf-8"
        )
        patcher = patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def configs(self) -> dict:
        from agents.workspace import WorkspaceContext, workspace_scope

        with workspace_scope(WorkspaceContext(root=self.work)):
            return McpManager()._load_configs()

    def test_an_untrusted_project_contributes_nothing(self) -> None:
        self.assertEqual(self.configs(), {})

    def test_a_trusted_project_is_loaded(self) -> None:
        from agents.project_trust import trust_project

        trust_project(self.work)
        self.assertEqual(list(self.configs()), ["evil"])


if __name__ == "__main__":
    unittest.main()
