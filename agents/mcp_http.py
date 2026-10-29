"""HTTP transport for remote MCP servers (iteration I5 / T14).

Streamable HTTP is the current remote transport: a JSON-RPC message is posted,
and the reply arrives either as ``application/json`` or as a ``text/event-stream``
that carries one or more messages. Sessions are identified by the
``Mcp-Session-Id`` header the server returns from ``initialize`` and must be
echoed afterwards.

Deliberate limits, because a remote endpoint is not under our control:

* A pinned protocol version. A server answering with a version outside the
  supported set is rejected instead of being half-understood.
* Redirects are followed at most three times and only within the same origin.
  A redirect that leaves the origin would forward the bearer token elsewhere.
* Authentication is a bearer token referenced from the environment. OAuth is not
  implemented, and a 401 says so explicitly rather than retrying blindly.
* A cancelled request sends ``notifications/cancelled``; the server may keep
  working otherwise, and a long-running tool would never learn it was abandoned.
* Only ``http`` and ``https`` are accepted. ``urllib`` would otherwise honour
  ``file://`` through its default opener, turning a configured endpoint into an
  arbitrary local file read.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request

MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class McpAuthRequired(RuntimeError):
    """The server wants credentials this runtime cannot obtain by itself."""


class McpHttpError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = status


class _Redirect(Exception):
    def __init__(self, status: int, location: str):
        super().__init__(location)
        self.status = status
        self.location = location


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface redirects instead of following them implicitly."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise _Redirect(code, newurl)


def _origin(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, f"{parsed.hostname}:{port}"


def _decode_body(raw: bytes, content_type: str) -> list[dict]:
    """Return JSON-RPC messages from a JSON body or an SSE stream."""

    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" not in content_type:
        if not text.strip():
            return []
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            raise McpHttpError("response is not valid JSON") from None
        return value if isinstance(value, list) else [value]

    messages: list[dict] = []
    for chunk in text.replace("\r\n", "\n").split("\n\n"):
        data = [line[5:].lstrip() for line in chunk.split("\n") if line.startswith("data:")]
        if not data:
            continue
        payload = "\n".join(data)
        if payload.strip() in {"", "[DONE]"}:
            continue
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        messages.extend(value if isinstance(value, list) else [value])
    return messages


class HttpTransport:
    """One remote MCP endpoint, with a session that survives between calls."""

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ):
        parsed = urllib.parse.urlsplit(str(url))
        if parsed.scheme not in {"http", "https"}:
            raise McpHttpError(
                f"unsupported MCP transport scheme: {parsed.scheme or '(missing)'}; only http and https are allowed"
            )
        if not parsed.netloc:
            raise McpHttpError("MCP url is missing a host")
        self.url = url
        self.token = token
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.session_id: str | None = None
        self.protocol_version = MCP_PROTOCOL_VERSION
        self._opener = urllib.request.build_opener(_NoRedirect)

    # -- request building --------------------------------------------------
    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version,
            **self.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _post_once(self, payload: dict, url: str) -> tuple[bytes, str, dict]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=self._request_headers(), method="POST")
        with self._opener.open(request, timeout=self.timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise McpHttpError(f"response exceeds {MAX_RESPONSE_BYTES} bytes")
            return raw, response.headers.get("Content-Type", ""), dict(response.headers)

    def _post(self, payload: dict) -> tuple[bytes, str, dict]:
        url = self.url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                return self._post_once(payload, url)
            except _Redirect as redirect:
                if _origin(redirect.location) != _origin(self.url):
                    raise McpHttpError("refusing to follow a redirect to another origin", status=redirect.status)
                url = redirect.location
        raise McpHttpError("too many redirects")

    # -- protocol ----------------------------------------------------------
    async def post(self, payload: dict) -> dict | None:
        """Send one message and return the response with the same id, if any."""

        try:
            raw, content_type, headers = await asyncio.to_thread(self._post, payload)
        except urllib.error.HTTPError as exc:
            return await self._handle_http_error(exc)

        session = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if session:
            self.session_id = session

        messages = _decode_body(raw, content_type)
        if not messages:
            return None
        wanted = payload.get("id")
        if wanted is None:
            return None
        for message in messages:
            if isinstance(message, dict) and message.get("id") == wanted:
                return message
        raise McpHttpError("response did not contain the requested id")

    async def _handle_http_error(self, exc: urllib.error.HTTPError) -> dict | None:
        try:
            detail = exc.read(4096).decode("utf-8", errors="replace")
        except OSError:
            detail = ""
        if exc.code == 401:
            raise McpAuthRequired(
                "remote MCP server requires authentication; OAuth is not implemented — "
                "supply a bearer token through the configured environment variable"
                + (f" (server said: {detail[:120]})" if detail else "")
            ) from None
        if exc.code == 404 and self.session_id:
            raise McpHttpError("MCP session expired; reconnect the server", status=404) from None
        raise McpHttpError(f"HTTP {exc.code}: {detail[:200]}", status=exc.code) from None

    async def notify(self, payload: dict) -> None:
        """Send a notification; failures are not fatal."""

        try:
            await asyncio.to_thread(self._post, payload)
        except Exception:  # noqa: BLE001 - notifications are best effort
            pass


def resolve_token(auth: dict | None) -> str | None:
    """Read the token from the environment, never from the config file itself."""

    import os

    if not auth:
        return None
    env_name = auth.get("env") if isinstance(auth, dict) else None
    if not env_name:
        raise McpAuthRequired("auth must reference an environment variable, e.g. {\"env\": \"MCP_TOKEN\"}")
    value = os.environ.get(str(env_name))
    if not value:
        raise McpAuthRequired(f"environment variable {env_name} is not set")
    return value
