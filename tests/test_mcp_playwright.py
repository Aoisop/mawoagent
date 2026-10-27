"""Playwright MCP end to end: real browser, real page, no leftovers (I5 / T13).

This test needs Node and a browser, so it only runs when ``MAWO_PLAYWRIGHT_E2E=1``.
The default suite stays fast and offline; run it explicitly after touching the
MCP transport or the browser configuration:

    MAWO_PLAYWRIGHT_E2E=1 python -m unittest tests.test_mcp_playwright -v

"Nothing left behind" is checked by starting a second session on the *same*
profile directory: a surviving browser holds a singleton lock on it, so the
second start would fail. That is stronger than looking for a process name, which
would also match browsers the user opened themselves.
"""

from __future__ import annotations

import asyncio
import functools
import os
import shutil
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agents.mcp_client import McpConnection

PACKAGE = "@playwright/mcp@0.0.83"
PAGE = (
    "<!doctype html><html><head><title>MCP Fixture</title></head>"
    "<body><h1 id='headline'>hello from the fixture</h1></body></html>"
)


def _gate_reason() -> str | None:
    if os.environ.get("MAWO_PLAYWRIGHT_E2E") != "1":
        return "set MAWO_PLAYWRIGHT_E2E=1 to run the browser end-to-end"
    if shutil.which("npx") is None:
        return "npx is not installed"
    return None


@unittest.skipIf(_gate_reason() is not None, _gate_reason() or "")
class PlaywrightEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "page.html").write_text(PAGE, encoding="utf-8")
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(root))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/page.html"
        cls.profile = root / "profile"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def _connection(self) -> McpConnection:
        return McpConnection(
            "playwright",
            "npx",
            [PACKAGE, "--headless", "--browser", "msedge", "--user-data-dir", str(self.profile)],
        )

    async def _session(self, conn: McpConnection) -> str:
        await conn.connect()
        await asyncio.wait_for(conn.initialize(), timeout=240)
        tools = await asyncio.wait_for(conn.list_tools(), timeout=60)
        names = {tool["name"] for tool in tools}
        self.assertIn("browser_navigate", names)
        await asyncio.wait_for(conn.call_tool("browser_navigate", {"url": self.url}), timeout=120)
        return await asyncio.wait_for(conn.call_tool("browser_snapshot", {}), timeout=120)

    async def _run_once(self) -> str:
        """One session, closed on the same event loop that created it."""

        conn = self._connection()
        try:
            return await self._session(conn)
        finally:
            process = conn._process
            await conn.aclose()
            self.returncode = process.returncode if process else None

    def test_navigate_and_read_a_local_page_then_leave_nothing_behind(self) -> None:
        snapshot = asyncio.run(self._run_once())
        self.assertIn("hello from the fixture", snapshot)
        self.assertIn("MCP Fixture", snapshot)
        self.assertIsNotNone(self.returncode, "the server process should have exited")

        # Same profile again: a surviving browser would still hold its lock.
        snapshot = asyncio.run(self._run_once())
        self.assertIn("hello from the fixture", snapshot)


if __name__ == "__main__":
    unittest.main()
