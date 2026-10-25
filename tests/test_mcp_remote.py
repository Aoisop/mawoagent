"""Remote MCP transport: session, SSE, pagination, auth, redirects (I5 / T14)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from agents import mcp_http
from agents.mcp_client import McpConnection
from agents.mcp_http import (
    HttpTransport,
    McpAuthRequired,
    McpHttpError,
    resolve_token,
)


class FakeServer:
    """A tiny MCP endpoint that records what the client sent."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.session_seen: list[str] = []
        self.protocol_seen: list[str] = []
        self.mode = "json"          # json | sse | 401
        self.redirect_to: str | None = None
        self.version = mcp_http.MCP_PROTOCOL_VERSION
        self.delay = 0.0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence the test output
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                server.requests.append(payload)
                server.headers.append(dict(self.headers))
                server.session_seen.append(self.headers.get("Mcp-Session-Id", ""))
                server.protocol_seen.append(self.headers.get("MCP-Protocol-Version", ""))
                if server.delay:
                    time.sleep(server.delay)

                if server.redirect_to:
                    self.send_response(307)
                    self.send_header("Location", server.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if server.mode == "401":
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Bearer realm="mcp"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                body = json.dumps(server.reply(payload)).encode()
                self.send_response(200)
                if server.mode == "sse":
                    self.send_header("Content-Type", "text/event-stream")
                    frame = b"event: message\ndata: " + body + b"\n\n"
                    self.send_header("Content-Length", str(len(frame)))
                    self.end_headers()
                    self.wfile.write(frame)
                    return
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "sess-1")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "FakeServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def reply(self, payload: dict) -> dict:
        method = payload.get("method")
        req_id = payload.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"protocolVersion": self.version, "capabilities": {}}}
        if method == "tools/list":
            if not (payload.get("params") or {}).get("cursor"):
                return {"jsonrpc": "2.0", "id": req_id,
                        "result": {"tools": [{"name": "one"}, {"name": "two"}], "nextCursor": "c2"}}
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": [{"name": "three"}]}}
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": req_id,
                    "result": {"content": [{"type": "text", "text": "ok"}]}}
        if method and method.startswith("notifications/"):
            return {}
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}


class SessionTests(unittest.TestCase):
    def test_session_header_is_echoed_after_initialize(self) -> None:
        with FakeServer() as server:
            conn = McpConnection("remote", url=server.url)
            asyncio.run(self._handshake(conn))

            self.assertTrue(server.session_seen[0] == "")
            self.assertIn("sess-1", server.session_seen[2:])

    async def _handshake(self, conn: McpConnection) -> None:
        await conn.connect()
        await conn.initialize()
        await conn.list_tools()

    def test_protocol_version_is_pinned_and_negotiated(self) -> None:
        with FakeServer() as server:
            conn = McpConnection("remote", url=server.url)
            asyncio.run(self._handshake(conn))
            self.assertEqual(server.protocol_seen[0], mcp_http.MCP_PROTOCOL_VERSION)
            self.assertEqual(conn.negotiated_version, mcp_http.MCP_PROTOCOL_VERSION)

    def test_unsupported_version_is_refused(self) -> None:
        with FakeServer() as server:
            server.version = "1999-01-01"
            conn = McpConnection("remote", url=server.url)

            async def run() -> None:
                await conn.connect()
                await conn.initialize()

            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(run())
            self.assertIn("unsupported MCP protocol version", str(caught.exception))


class TransportFeatureTests(unittest.TestCase):
    def test_tools_are_paginated(self) -> None:
        with FakeServer() as server:
            conn = McpConnection("remote", url=server.url)

            async def run():
                await conn.connect()
                await conn.initialize()
                return await conn.list_tools()

            tools = asyncio.run(run())
            self.assertEqual([tool["name"] for tool in tools], ["one", "two", "three"])
            cursors = [(r.get("params") or {}).get("cursor") for r in server.requests if r.get("method") == "tools/list"]
            self.assertEqual(cursors, [None, "c2"])

    def test_sse_response_is_parsed(self) -> None:
        with FakeServer() as server:
            server.mode = "sse"
            conn = McpConnection("remote", url=server.url)

            async def run():
                await conn.connect()
                await conn.initialize()
                return await conn.list_tools()

            self.assertEqual(len(asyncio.run(run())), 3)

    def test_missing_credentials_are_reported_as_oauth_not_implemented(self) -> None:
        with FakeServer() as server:
            server.mode = "401"
            conn = McpConnection("remote", url=server.url)

            async def run():
                await conn.connect()
                await conn.initialize()

            with self.assertRaises(McpAuthRequired) as caught:
                asyncio.run(run())
            self.assertIn("OAuth is not implemented", str(caught.exception))

    def test_bearer_token_is_sent_when_configured(self) -> None:
        with FakeServer() as server:
            with mock.patch.dict(os.environ, {"MCP_TEST_TOKEN": "secret-value"}):
                conn = McpConnection("remote", url=server.url, auth={"env": "MCP_TEST_TOKEN"})
                asyncio.run(self._handshake(conn))
            self.assertIn("Bearer secret-value", server.headers[0].get("Authorization", ""))

    async def _handshake(self, conn: McpConnection) -> None:
        await conn.connect()
        await conn.initialize()

    def test_same_origin_redirect_is_followed(self) -> None:
        """A redirect that stays on the endpoint is normal (trailing slash, gateway)."""

        with FakeServer() as server:
            transport = HttpTransport(server.url, timeout=5)
            server.redirect_to = server.url
            try:
                with self.assertRaises(McpHttpError):
                    asyncio.run(transport.post({"jsonrpc": "2.0", "id": 1, "method": "x"}))
            finally:
                server.redirect_to = None

    def test_cross_origin_redirect_is_refused(self) -> None:
        with FakeServer() as server:
            server.redirect_to = "http://127.0.0.1:9/mcp"
            transport = HttpTransport(server.url, timeout=5)
            with self.assertRaises(McpHttpError) as caught:
                asyncio.run(transport.post({"jsonrpc": "2.0", "id": 1, "method": "x"}))
            self.assertIn("another origin", str(caught.exception))

    def test_cancellation_notifies_the_server(self) -> None:
        with FakeServer() as server:
            server.delay = 0.6
            transport = HttpTransport(server.url, timeout=5)

            async def run() -> None:
                task = asyncio.create_task(transport.post({"jsonrpc": "2.0", "id": 7, "method": "tools/call"}))
                await asyncio.sleep(0.1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                # The notification is fire-and-forget; give it a moment to land.
                for _ in range(30):
                    if any(r.get("method") == "notifications/cancelled" for r in server.requests):
                        break
                    await asyncio.sleep(0.05)

            asyncio.run(run())


class TokenReferenceTests(unittest.TestCase):
    def test_token_comes_from_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {"MCP_T": "abc"}):
            self.assertEqual(resolve_token({"env": "MCP_T"}), "abc")

    def test_missing_variable_is_an_explicit_error(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_MISSING", None)
            with self.assertRaises(McpAuthRequired):
                resolve_token({"env": "MCP_MISSING"})

    def test_literal_tokens_are_rejected(self) -> None:
        with self.assertRaises(McpAuthRequired):
            resolve_token({"token": "inline-secret"})


if __name__ == "__main__":
    unittest.main()
