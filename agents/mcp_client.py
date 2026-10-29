"""
MCP 客户端模块。

这个文件负责连接基于 stdio 的 MCP Server，完成工具发现，并把 Agent 发起的工具调用
转发到对应的 MCP Server。

实现方式：
- 不依赖 MCP SDK，直接通过标准输入/标准输出传输 JSON-RPC 消息。
- 每个 MCP Server 都由一个子进程承载。
- 每个 MCP 工具都会被包装成 `mcp__serverName__toolName` 形式，避免和本地工具重名。

配置来源：
- 全局配置：`~/.mawoagent/settings.json`
- 项目配置：`.mawoagent/settings.json`
- Claude Code 约定配置：`.mcp.json`

配置格式示例：
{
    "mcpServers": {
        "name": {
            "command": "...",
            "args": [...],
            "env": {...}
        }
    }
}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import subprocess
import signal
from pathlib import Path
from typing import Any, Awaitable, Callable

from .mcp_http import (
    MCP_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    HttpTransport,
    McpAuthRequired,
    McpHttpError,
    resolve_token,
)
from .tool_content import capability_report, from_mcp_content

_logger = logging.getLogger(__name__)
from .ui import print_error, print_info

MCP_TOOL_TIMEOUT_SECONDS = 60.0
_logger = logging.getLogger(__name__)
MCP_MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class McpToolError(RuntimeError):
    """A valid MCP response explicitly reported a failed tool operation."""

    def __init__(self, result: dict):
        self.result = result
        content = result.get("content") or []
        text = "\n".join(str(item.get("text", "")) for item in content
                         if isinstance(item, dict) and item.get("type") == "text")
        super().__init__("MCP tool failed: " + (text or json.dumps(result, ensure_ascii=False)))


# ─── 单个 MCP 连接：一个 McpConnection 对应一个 MCP Server 子进程 ──────────────────


class BorrowedMcpManager:
    """A child may call its inherited tools, but never owns parent processes."""

    def __init__(self, owner: McpManager, allowed_tools: set[str]):
        self._owner = owner
        self._allowed_tools = frozenset(allowed_tools)

    def is_mcp_tool(self, name: str) -> bool:
        return name in self._allowed_tools

    async def call_tool(self, name: str, args: dict) -> str:
        if name not in self._allowed_tools:
            raise ValueError(f"MCP tool not available in this child: {name}")
        return await self._owner.call_tool(name, args)

    async def disconnect_all(self) -> None:
        # The parent's lifetime encloses all child calls and owns cleanup.
        pass


def _resolve_command(command: str, args: list[str]) -> tuple[str, list[str]]:
    """Resolve a configured command to something CreateProcess can start.

    On Windows ``npx`` is ``npx.cmd``: ``create_subprocess_exec`` does not apply
    PATHEXT, so the plain name raises FileNotFoundError and the server never
    starts. Batch shims also need ``cmd /c`` to be executed at all.
    """

    resolved = shutil.which(command) if command else None
    if resolved is None:
        raise FileNotFoundError(f"MCP command not found: {command}")
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        return os.environ.get("ComSpec", "cmd.exe"), ["/c", resolved, *args]
    return resolved, list(args)


class McpConnection:
    """管理单个 MCP Server 子进程，以及和它之间的 JSON-RPC 通信。"""

    def __init__(self, server_name: str, command: str | None = None, args: list[str] | None = None,
                 env: dict[str, str] | None = None, *, url: str | None = None,
                 headers: dict[str, str] | None = None, auth: dict | None = None):
        # MCP Server 在配置中的名称，用于后续生成工具名前缀和路由工具调用。
        self.server_name = server_name
        # 启动 MCP Server 的命令，例如 `node`、`python`、某个可执行文件路径等。
        self.command = command
        # 启动命令附带的参数。
        self.args = args or []
        # 额外环境变量。连接时会和当前进程环境变量合并。
        self.env = env or {}
        # Remote transport. When a url is configured the server is reached over
        # HTTP instead of being spawned, and `command` is unused.
        self.url = url
        self.headers = headers or {}
        self.auth = auth
        self._http: HttpTransport | None = None
        self.negotiated_version: str | None = None
        # MCP Server 子进程对象。连接成功前为空。
        self._process: asyncio.subprocess.Process | None = None
        # JSON-RPC 请求 id 自增计数器，用于把请求和响应对应起来。
        self._next_id = 1
        # 正在等待响应的请求。key 是 JSON-RPC id，value 是 Future。
        self._pending: dict[int, asyncio.Future] = {}
        # 后台读取 stdout 的任务。MCP Server 的响应会从 stdout 持续读出。
        self._reader_task: asyncio.Task | None = None
        # 后台消费 stderr 的任务，避免 stderr 缓冲区写满阻塞 server。
        self._stderr_task: asyncio.Task | None = None
        self._reader_error: Exception | None = None

    async def connect(self) -> None:
        """Start a stdio server, or prepare the remote session."""
        self._reader_error = None
        if self.url:
            # Remote servers have no process to supervise; the session header is
            # established by the initialize exchange.
            self._http = HttpTransport(
                self.url,
                token=resolve_token(self.auth),
                headers=self.headers,
            )
            return

        # 子进程环境变量 = 当前进程环境变量 + 配置里声明的额外变量。
        merged_env = {**os.environ, **self.env}
        command, args = _resolve_command(self.command or "", self.args)
        # 使用 stdio 模式启动 MCP Server：
        # - stdin：客户端向 Server 写 JSON-RPC 请求。
        # - stdout：Server 向客户端返回 JSON-RPC 响应。
        # - stderr：保留错误输出管道，避免 Server 继承当前终端输出。
        self._process = await asyncio.create_subprocess_exec(
            command, *args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged_env,
            start_new_session=os.name != "nt",
            limit=MCP_MAX_MESSAGE_BYTES,
        )
        # 后台持续读取 stdout。这里不阻塞 connect()，否则后续无法继续初始化。
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _read_loop(self) -> None:
        """持续读取 MCP Server stdout 中按行分隔的 JSON-RPC 响应。"""
        assert self._process and self._process.stdout
        try:
            while True:
                # MCP stdio 通常是一行一个 JSON-RPC 消息。
                line = await self._process.stdout.readline()
                if not line:
                    # 读不到内容说明子进程 stdout 已关闭，通常表示进程退出。
                    break
                if len(line) > MCP_MAX_MESSAGE_BYTES:
                    raise ValueError(f"MCP message exceeds {MCP_MAX_MESSAGE_BYTES} bytes")
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # 非法 JSON 直接忽略，避免单条脏输出打断整个连接。
                    continue

                # JSON-RPC 响应会带 id；通知类消息通常没有 id。
                if not isinstance(msg, dict):
                    continue
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if "error" in msg:
                        # Server 返回 JSON-RPC error 时，把等待中的 Future 标记为异常。
                        e = msg["error"]
                        if not fut.done():
                            fut.set_exception(
                                RuntimeError(f"MCP error {e.get('code')}: {e.get('message')}")
                            )
                    elif not fut.done():
                        # 正常响应只把 result 部分交给调用方。
                        fut.set_result(msg.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._reader_error = RuntimeError(f"MCP reader failed (message limit {MCP_MAX_MESSAGE_BYTES} bytes): {exc}")
            self._fail_pending(self._reader_error)
        else:
            self._reader_error = RuntimeError(f"MCP server '{self.server_name}' closed")
            self._fail_pending(self._reader_error)

    async def _drain_stderr(self) -> None:
        """持续消费 stderr，避免子进程输出过多后阻塞。"""
        assert self._process and self._process.stderr
        try:
            while True:
                chunk = await self._process.stderr.read(65536)
                if not chunk:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _fail_pending(self, error: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(error)
        self._pending.clear()

    async def _send_request(self, method: str, params: dict | None = None) -> Any:
        """发送一条 JSON-RPC request，并等待对应 id 的 response。"""
        if self._reader_error is not None:
            raise RuntimeError(str(self._reader_error))
        # 为本次请求分配唯一 id。读循环会用这个 id 找到对应 Future。
        req_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}

        if self._http is not None:
            try:
                response = await self._http.post(payload)
            except asyncio.CancelledError:
                # The server may still be working on it; tell it to stop.
                self._notify_cancel(req_id)
                raise
            if response is None:
                return None
            if "error" in response:
                error = response["error"]
                raise RuntimeError(f"MCP error {error.get('code')}: {error.get('message')}")
            return response.get("result")

        assert self._process and self._process.stdin
        msg = json.dumps(payload)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        # stdio 传输使用换行作为消息边界。
        try:
            self._process.stdin.write((msg + "\n").encode())
            await self._process.stdin.drain()
            return await fut
        except asyncio.CancelledError:
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()
            self._notify_cancel(req_id)
            raise
        except Exception:
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()
            raise

    def _notify_cancel(self, request_id: int) -> None:
        """Best-effort cancellation; never raises or blocks the caller."""

        message = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}}
        if self._http is not None:
            try:
                asyncio.get_running_loop().create_task(self._http.notify(message))
            except RuntimeError:
                pass
            return
        try:
            self._send_notification("notifications/cancelled", {"requestId": request_id})
        except Exception:  # noqa: BLE001
            pass

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        """发送一条 JSON-RPC notification。notification 没有 id，也不等待响应。"""
        if self._http is not None:
            try:
                asyncio.get_running_loop().create_task(
                    self._http.notify({"jsonrpc": "2.0", "method": method, "params": params or {}})
                )
            except RuntimeError:
                pass
            return
        if not self._process or not self._process.stdin:
            return
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
        self._process.stdin.write((msg + "\n").encode())

    async def initialize(self) -> None:
        """执行 MCP 初始化握手。"""
        # initialize 是 MCP 连接建立后的第一步，用于协商协议版本和客户端信息。
        result = await self._send_request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mawoagent", "version": "1.0.0"},
        })
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        if version and version not in SUPPORTED_PROTOCOL_VERSIONS:
            # Half-understanding a newer protocol is worse than refusing it.
            raise RuntimeError(f"unsupported MCP protocol version: {version}")
        self.negotiated_version = version or MCP_PROTOCOL_VERSION
        if self._http is not None:
            self._http.protocol_version = self.negotiated_version
        # 初始化请求成功后，按 MCP 协议发送 initialized 通知。
        self._send_notification("notifications/initialized")

    async def list_tools(self) -> list[dict]:
        """从当前 MCP Server 查询可用工具列表，跟随 nextCursor 分页。"""
        collected: list[dict] = []
        cursor: str | None = None
        for _ in range(20):
            result = await self._send_request("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(result, dict):
                break
            tools = result.get("tools")
            if isinstance(tools, list):
                collected.extend(tools)
            cursor = result.get("nextCursor")
            if not cursor:
                break
        # 这里保留 MCP 原始 inputSchema，同时附加 serverName，方便上层做工具名加前缀和路由。
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema"),
                "serverName": self.server_name,
            }
            for t in collected
            if isinstance(t, dict) and "name" in t
        ]

    async def call_tool(self, name: str, args: dict) -> str:
        """调用当前 MCP Server 上的某个工具，并把结果转换成字符串返回。"""
        result = await self._send_request("tools/call", {"name": name, "arguments": args})
        if isinstance(result, dict) and result.get("isError") is True:
            raise McpToolError(result)
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            # Text blocks are joined exactly as before. Images, audio and
            # resource links become attachments: the payload is written to disk
            # and only a description enters the conversation.
            content = from_mcp_content(result["content"], name)
            body = content.text
            if result.get("structuredContent") is not None:
                structured = json.dumps(result["structuredContent"], ensure_ascii=False)
                body = f"{body}\n{structured}" if body else structured
            described = "\n".join(item.describe() for item in content.attachments)
            combined = "\n".join(part for part in (body, described) if part)
            report = capability_report(content)
            if report["unsupported"]:
                kinds = ", ".join(report["unsupported"])
                combined = f"{combined}\n[not displayable in this runtime: {kinds}]" if combined \
                    else f"[not displayable in this runtime: {kinds}]"
            if combined:
                return combined
            count = len(result["content"])
            return f"[MCP returned {count} content block(s) this runtime cannot display]"
        # 如果不是标准 content list，就退回为 JSON 字符串，避免丢失信息。
        return json.dumps(result)

    def close(self) -> None:
        """关闭 MCP Server 子进程，并让所有等待中的请求失败。"""
        if self._reader_task:
            # 停止 stdout 后台读取任务。
            self._reader_task.cancel()
            self._reader_task = None
        if self._stderr_task:
            self._stderr_task.cancel()
            self._stderr_task = None
        if self._process:
            try:
                # 直接杀掉子进程，确保外部 MCP Server 不继续残留。
                if getattr(self._process, "stdin", None):
                    try:
                        self._process.stdin.close()
                    except (RuntimeError, ValueError):
                        # The owning loop is gone; the kill below still applies.
                        pass
                if hasattr(self._process, "kill"):
                    if os.name != "nt":
                        os.killpg(self._process.pid, signal.SIGKILL)
                    else:
                        # kill() only stops cmd.exe; node and the browser it
                        # spawned would keep running.
                        subprocess.run(
                            ["taskkill", "/PID", str(self._process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=10,
                            check=False,
                        )
            except (ProcessLookupError, subprocess.TimeoutExpired):
                # 进程已经退出时 kill 可能抛出该异常，忽略即可。
                pass
            self._process = None
        # 连接关闭后，所有还没收到响应的请求都不可能再完成，需要显式置为异常。
        self._fail_pending(RuntimeError(f"MCP server '{self.server_name}' closed"))
        self._http = None

    async def aclose(self) -> None:
        process = self._process
        tasks = [task for task in (self._reader_task, self._stderr_task) if task]
        self.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process:
            await asyncio.wait_for(process.wait(), timeout=5)


# ─── MCP 管理器：统一管理多个 MCP Server 连接和工具路由 ─────────────────────────────


class McpManager:
    """
    管理所有 MCP Server 连接。

    使用方式：
    1. 调用 load_and_connect(authorize_start=...) 授权启动、连接 Server、发现工具。
    2. 调用 get_tool_definitions() 把 MCP 工具暴露给模型。
    3. 当模型调用 mcp__server__tool 形式的工具时，用 call_tool() 路由到对应 Server。
    """

    def __init__(self):
        # 已连接的 MCP Server。key 是 server name，value 是对应连接对象。
        self._connections: dict[str, McpConnection] = {}
        # 所有 MCP Server 发现出来的工具定义，保持接近 MCP 原始格式。
        self._tools: list[dict] = []
        # 防止重复连接。load_and_connect() 只应真正执行一次。
        self._connected = False

    async def load_and_connect(
        self, *, authorize_start: Callable[[str, dict], Awaitable[bool]] | None = None
    ) -> None:
        """Authorize each configured process before connecting and discovering tools."""
        if self._connected:
            return
        self._connected = True

        # 合并全局、项目和 .mcp.json 配置。后读取的配置会覆盖同名 Server。
        configs = self._load_configs()
        if not configs:
            return

        # 每个 Server 的初始化和工具发现最多等待 15 秒，避免坏配置长期卡住启动。
        timeout = 15.0

        for name, cfg in configs.items():
            conn = None
            try:
                # Configuration discovery must never implicitly execute project
                # commands or contact a remote endpoint before approval.
                remote = bool(cfg.get("url"))
                startup: dict[str, str] = {"server_name": name, "cwd": str(Path.cwd())}
                if remote:
                    startup.update({"transport": "http", "url": cfg["url"]})
                else:
                    startup.update({
                        "transport": "stdio",
                        "command": shlex.join([cfg["command"], *(cfg.get("args") or [])]),
                    })
                if authorize_start is None or not await authorize_start("mcp_start", startup):
                    print_info(f"MCP startup denied: {name}")
                    continue
                conn = McpConnection(
                    name,
                    cfg.get("command"),
                    cfg.get("args"),
                    cfg.get("env"),
                    url=cfg.get("url"),
                    headers=cfg.get("headers"),
                    auth=cfg.get("auth"),
                )
                # 连接子进程 -> MCP 初始化握手 -> 查询工具列表。
                await conn.connect()
                await asyncio.wait_for(conn.initialize(), timeout=timeout)
                server_tools = await asyncio.wait_for(conn.list_tools(), timeout=timeout)
                # 只有初始化和工具发现都成功，才登记为可用连接。
                self._connections[name] = conn
                self._tools.extend(server_tools)
                print_info(f"MCP connected: {name} ({len(server_tools)} tools)")
            except asyncio.CancelledError:
                if conn is not None:
                    await conn.aclose()
                raise
            except Exception as e:
                # 单个 Server 失败不影响其他 Server。失败连接需要关闭以清理子进程。
                print_error(f"MCP failed to connect: {name}: {e}")
                if conn is not None:
                    await conn.aclose()

    def get_tool_definitions(self) -> list[dict]:
        """返回 Agent/Anthropic 可直接使用的工具定义，并给 MCP 工具名加前缀。"""
        return [
            {
                # 前缀格式：mcp__服务名__工具名。
                # 这样可以避免 MCP 工具和内置工具重名，也方便 call_tool() 反向解析路由。
                "name": f"mcp__{t['serverName']}__{t['name']}",
                "description": t.get("description") or f"MCP tool {t['name']} from {t['serverName']}",
                # Anthropic 工具字段叫 input_schema；MCP 原始工具字段一般叫 inputSchema。
                "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
            }
            for t in self._tools
        ]

    def is_mcp_tool(self, name: str) -> bool:
        """判断一个工具名是否是 MCP 工具名。"""
        return name.startswith("mcp__")

    async def call_tool(self, prefixed_name: str, args: dict) -> str:
        """把带前缀的 MCP 工具调用路由到正确的 MCP Server。"""
        # 工具名格式为 mcp__serverName__toolName。
        parts = prefixed_name.split("__")
        if len(parts) < 3:
            raise ValueError(f"Invalid MCP tool name: {prefixed_name}")
        server_name = parts[1]
        # 工具名本身也可能包含 "__"，所以第 3 段之后要重新拼回去。
        tool_name = "__".join(parts[2:])  # tool name might contain __
        conn = self._connections.get(server_name)
        if not conn:
            raise RuntimeError(f"MCP server '{server_name}' not connected")
        try:
            return await asyncio.wait_for(
                conn.call_tool(tool_name, args), timeout=MCP_TOOL_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"MCP tool '{prefixed_name}' timed out after {MCP_TOOL_TIMEOUT_SECONDS:g} seconds"
            ) from exc

    async def disconnect_all(self) -> None:
        """断开所有 MCP Server 连接，并清空工具缓存。"""
        await asyncio.gather(*(conn.aclose() for conn in self._connections.values()))
        self._connections.clear()
        self._tools.clear()
        self._connected = False

    # ─── 配置加载 ──────────────────────────────────────

    def _load_configs(self) -> dict[str, dict]:
        """按优先级加载并合并 MCP Server 配置。"""
        from .project_trust import is_project_trusted
        from .workspace import project_root

        merged: dict[str, dict] = {}
        project = project_root()

        # 1. 全局配置：~/.mawoagent/settings.json
        global_path = Path.home() / ".mawoagent" / "settings.json"
        self._merge_config_file(global_path, merged)

        # A project configuration names the command to run, so loading it is
        # already executing it. Untrusted checkouts must not get that far —
        # otherwise cloning a repository is enough to start its server with the
        # runtime's own environment, including the API key.
        project_files = [project / ".mawoagent" / "settings.json", project / ".mcp.json"]
        if is_project_trusted(project):
            for path in project_files:
                self._merge_config_file(path, merged)
        elif any(path.exists() for path in project_files):
            _logger.warning(
                "Ignoring MCP configuration in %s: the project is not trusted. "
                "Run /trust-project to allow it.", project
            )

        return merged

    def _merge_config_file(self, path: Path, target: dict[str, dict]) -> None:
        """把单个配置文件里的 mcpServers 合并进 target。"""
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            # 支持两种格式：
            # 1. {"mcpServers": {"name": {...}}}
            # 2. {"name": {...}}
            servers = raw.get("mcpServers", raw)
            for name, config in servers.items():
                # 接收 stdio（command）或 remote（url）配置；其余忽略。
                if isinstance(config, dict) and ("command" in config or "url" in config):
                    target[name] = config
        except Exception:
            # 配置文件格式错误时跳过，避免一个坏配置导致整个 Agent 启动失败。
            pass
