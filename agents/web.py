"""Local-only web adapter for the existing Agent runtime (no extra dependencies)."""
from __future__ import annotations

import argparse
from collections import deque
from copy import deepcopy
import asyncio
from contextvars import ContextVar
from dataclasses import asdict
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import os
import re
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, urlsplit

from .agent import Agent
from .checkpoints import CURRENT_CALL
from .config import (
    load_env_file as _load_env_file,
    resolve_api_config as _resolve_api_config,
    resolve_agent_options,
)
from .policy import load_security_policy
from .session import list_sessions, load_session

ASSETS = Path(__file__).resolve().parent.parent / "web"
MODES = {"default", "plan", "acceptEdits", "dontAsk"}
CURRENT_TOOL = ContextVar("web_tool", default=None)
STREAM_HEARTBEAT_SECONDS = 12
STREAM_EVENT_LIMIT = 128
STREAM_BYTE_LIMIT = 2 * 1024 * 1024
WEB_TEXT_LIMIT = 128000
WEB_TEXT_TRUNCATION = "\n\n[Web 预览已达 128,000 字符上限；后续内容不再展示。]"


def _sse_frame(kind, payload):
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {kind}\ndata: {data}\nid: {payload['id']}:{payload['revision']}\n\n".encode("utf-8", errors="replace")


def _entry_update(previous, entry):
    """Send new text once; replacements also cover model retry rollback."""
    update = {"id": entry["id"], "set": {}, "append": {}}
    for key, value in entry.items():
        if key in previous and previous[key] == value:
            continue
        old = previous.get(key)
        if key in {"text", "result"} and isinstance(old, str) and isinstance(value, str) and value.startswith(old):
            update["append"][key] = value[len(old):]
        else:
            update["set"][key] = value
    removed = previous.keys() - entry.keys()
    if removed:
        update["remove"] = sorted(removed)
    return update


class WebError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def text_content(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(part.get("text", "")) for part in value if isinstance(part, dict) and part.get("type") == "text")
    return ""


def history_entries(data: dict) -> list[dict]:
    """Render stored protocol messages without exposing system prompts or injections."""
    if isinstance(data.get("webEntries"), list):
        entries = data["webEntries"]
        legacy_thinking = any(isinstance(e, dict) and e.get("role") == "assistant" and
                              re.match(r"^\s*\[thinking\]\s", str(e.get("text", ""))) for e in entries)
        if all(isinstance(e, dict) and isinstance(e.get("id"), str) and
               ((e.get("kind") == "message" and e.get("role") in {"user", "assistant"} and isinstance(e.get("text"), str)) or
                (e.get("kind") == "thinking" and isinstance(e.get("text"), str)) or
                (e.get("kind") == "tool" and isinstance(e.get("name"), str) and isinstance(e.get("result"), str))) for e in entries):
            if not legacy_thinking:
                entries = deepcopy(entries[-500:])
                journal = (data.get("runtimeState") or {}).get("toolCheckpoints") or {}
                for entry in entries:
                    if entry.get("kind") != "tool" or entry.get("status") != "running":
                        continue
                    record = journal.get(entry.get("callId"), {})
                    if record.get("status") == "completed" and isinstance(record.get("result"), str):
                        result = record["result"]
                        entry.update(result=result[:16000], status="error" if result.startswith("Error") else "done")
                    else:
                        entry.update(status="error", result="执行中断，结果未知。请先检查实际状态，不要直接重复执行。")
                return entries
            # Old mixed text has no reliable delimiter. Recover the actual answer
            # from protocol messages rather than guessing where reasoning ends.
    entries, tools = [], {}
    for msg in (data.get("openaiMessages") or data.get("anthropicMessages") or [])[-500:]:
        role = msg.get("role")
        if role not in {"user", "assistant", "tool"}:
            continue
        content = msg.get("content")
        text = text_content(content)
        if text and role != "tool":
            entries.append({"kind": "message", "role": role, "text": Agent._strip_runtime_injections(None, text), "id": secrets.token_hex(8)})
        calls = list(msg.get("tool_calls") or [])
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    calls.append({"id": block.get("id"), "function": {"name": block.get("name"), "arguments": block.get("input")}})
                if block.get("type") == "tool_result" and block.get("tool_use_id") in tools:
                    tools[block["tool_use_id"]].update(result=text_content(block.get("content"))[:16000], status="done")
        for call in calls:
            fn = call.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"raw": args}
            entry = {"kind": "tool", "id": str(call.get("id") or secrets.token_hex(8)), "name": fn.get("name", "tool"), "input": args, "status": "done", "result": ""}
            entries.append(entry)
            tools[entry["id"]] = entry
        if role == "tool" and msg.get("tool_call_id") in tools:
            tools[msg["tool_call_id"]]["result"] = text[:16000]
    return entries


class WebAgent(Agent):
    """Instance-scoped UI hooks; checked tool execution remains the runtime's own."""
    def __init__(self, view, **kwargs):
        self.view = view
        super().__init__(**kwargs)
        self._output_buffer = []  # Keep terminal output out of the browser stream.
        self.set_confirm_fn(view.confirm)
        self.set_plan_approval_fn(view.approve_plan)

    def _emit_text(self, text: str) -> None:
        super()._emit_text(text)
        self._output_buffer.clear()
        self.view.emit_text(text)

    def _emit_thinking(self, text: str) -> None:
        self.view.emit_thinking(text)

    def _capture_stream_output(self):
        state = super()._capture_stream_output()
        state["entries"] = deepcopy(self.view.entries)
        return state

    def _discard_stream_output(self, state):
        super()._discard_stream_output(state)
        self.view.entries = state["entries"]
        self.view.changed()

    def _auto_save(self, *, strict=False):
        super()._auto_save(strict=strict)
        data = load_session(self.session_id)
        if data:
            from .session import save_session
            data["webEntries"] = self.view.entries[-500:]
            save_session(self.session_id, data)

    async def _run_checked_tool(self, name: str, inp) -> str:
        self.view.finish_thinking()
        entry = {"kind": "tool", "id": secrets.token_hex(8), "callId": CURRENT_CALL.get(), "name": name, "input": inp, "status": "running", "result": ""}
        self.view.entries.append(entry)
        token = CURRENT_TOOL.set(entry["id"])
        self.view.changed()
        try:
            result = await super()._run_checked_tool(name, inp)
            failure = result.startswith(("Error", "Action denied", "User denied", "Tool not available"))
            entry.update(result=result[:16000], status="error" if failure else "done")
            if name in {"edit_file", "write_file"} and entry["status"] == "done" and isinstance(inp, dict):
                path = inp.get("file_path")
                if path and path not in self.view.files:
                    self.view.files.append(path)
            return result
        except asyncio.CancelledError:
            entry.update(status="cancelled", result="执行已停止")
            raise
        finally:
            CURRENT_TOOL.reset(token)
            self.view.changed()


class Conversation:
    def __init__(self, app, sid=None, saved=None):
        self.app = app
        self.id = sid or secrets.token_hex(4)
        self.saved = saved
        self.entries = history_entries(saved) if saved else []
        self.title = next((item["text"][:28] for item in self.entries if item.get("role") == "user"), "新会话")
        self.mode = "plan" if saved and saved.get("runtimeState", {}).get("permissionMode") == "plan" else "default"
        self.agent = None
        self.task = None
        self.busy = False
        self.files = []
        self.requests = {}
        self.error = None
        self.revision = 0
        self.stream_id = secrets.token_hex(12)
        self.condition = threading.Condition()
        self.snapshot = {}
        self._events = deque()
        self._event_bytes = 0
        self._publish_handle = None
        self.changed()

    def state(self):
        usage = self.agent.get_token_usage() if self.agent else {"input": 0, "output": 0}
        return {"id": self.id, "streamId": self.stream_id, "revision": self.revision, "title": self.title, "mode": self.agent.permission_mode if self.agent else self.mode,
                "busy": self.busy, "entries": self.entries, "files": self.files, "error": self.error,
                "requests": [{k: v for k, v in req.items() if k != "future"} for req in self.requests.values()],
                "usage": usage, "model": self.agent.model if self.agent else self.app.config["model"]}

    def changed(self):
        if self._publish_handle:
            self._publish_handle.cancel()
            self._publish_handle = None
        if len(self.entries) > 500:
            self.entries = self.entries[-500:]
        # Reuse immutable entries/metadata that did not change. Token publication
        # must not serialize and duplicate the entire accumulated conversation.
        previous = self.snapshot
        before = {entry["id"]: entry for entry in previous.get("entries", [])}
        entries, updates = [], []
        for entry in self.entries:
            old = before.get(entry["id"], {})
            if old == entry:
                entries.append(old)
            else:
                frozen = deepcopy(entry)
                entries.append(frozen)
                updates.append(_entry_update(old, frozen))
        changes = {}
        snapshot = {}
        for key, value in self.state().items():
            if key in {"entries", "revision"}:
                continue
            if key in previous and previous[key] == value:
                snapshot[key] = previous[key]
            else:
                snapshot[key] = deepcopy(value)
                changes[key] = snapshot[key]
        revision = self.revision + 1
        snapshot.update(entries=entries, revision=revision)
        delta = {"id": self.id, "streamId": self.stream_id, "baseRevision": self.revision, "revision": revision,
                 "changes": changes, "entries": updates}
        order = [entry["id"] for entry in entries]
        if order != [entry["id"] for entry in previous.get("entries", [])]:
            delta["order"] = order
        frame = _sse_frame("delta", delta)
        with self.condition:
            self.revision, self.snapshot = revision, snapshot
            self._events.append((delta["baseRevision"], revision, frame))
            self._event_bytes += len(frame)
            while self._events and (len(self._events) > STREAM_EVENT_LIMIT or self._event_bytes > STREAM_BYTE_LIMIT):
                self._event_bytes -= len(self._events.popleft()[2])
            self.condition.notify_all()

    def stream_updates(self, revision):
        """Fresh/reconnected or lagging clients receive one authoritative snapshot."""
        with self.condition:
            current, snapshot = self.revision, self.snapshot
            if revision == current:
                return current, []
            if revision >= 0 and self._events and self._events[0][0] <= revision < current:
                return current, [frame for _, end, frame in self._events if end > revision]
        return current, [_sse_frame("state", snapshot)]

    @staticmethod
    def _append_preview(entry, text):
        existing = entry["text"]
        if len(existing) > WEB_TEXT_LIMIT:
            return
        remaining = WEB_TEXT_LIMIT - len(existing)
        entry["text"] = existing + text[:remaining]
        if len(text) > remaining:
            entry["text"] += WEB_TEXT_TRUNCATION

    def emit_text(self, text):
        self.finish_thinking()
        if not self.entries or self.entries[-1].get("role") != "assistant":
            self.entries.append({"kind": "message", "id": secrets.token_hex(8), "role": "assistant", "text": ""})
        self._append_preview(self.entries[-1], text)
        # Coalesce token bursts; tools, permissions and final state publish immediately.
        if self._publish_handle is None:
            self._publish_handle = asyncio.get_running_loop().call_later(0.05, self.changed)

    def emit_thinking(self, text):
        if not text:
            return
        if not self.entries or self.entries[-1].get("kind") != "thinking" or self.entries[-1].get("status") != "running":
            self.entries.append({"kind": "thinking", "id": secrets.token_hex(8), "text": "", "status": "running"})
        self._append_preview(self.entries[-1], text)
        if self._publish_handle is None:
            self._publish_handle = asyncio.get_running_loop().call_later(0.05, self.changed)

    def finish_thinking(self):
        for entry in self.entries:
            if entry.get("kind") == "thinking" and entry.get("status") == "running":
                entry["status"] = "done"

    async def request(self, message, kind="tool"):
        rid = secrets.token_hex(12)
        future = asyncio.get_running_loop().create_future()
        self.requests[rid] = {"id": rid, "kind": kind, "message": message, "toolId": CURRENT_TOOL.get(), "future": future}
        self.changed()
        try:
            # A disconnected browser never silently grants a permission.
            return await asyncio.wait_for(future, timeout=600)
        except asyncio.TimeoutError:
            return {"choice": "cancel"} if kind == "plan" else False
        finally:
            self.requests.pop(rid, None)
            self.changed()

    async def confirm(self, message):
        return await self.request(message)

    async def approve_plan(self, message):
        return await self.request(message, "plan")

    def ensure_agent(self):
        if self.agent:
            return
        if not self.app.options.get("api_key"):
            raise WebError("尚未配置模型 API Key。请在项目 .env 中配置后重启 Web 服务。")
        agent = WebAgent(self, permission_mode=self.mode, workspace=self.app.workspace, **self.app.options)
        if self.saved:
            try:
                agent.restore_session(self.saved)
            except Exception:
                # Clients have not made any requests at this point.
                asyncio.create_task(agent.aclose())
                raise
        agent.session_id = self.id
        if not self.saved:
            agent._conversation_id = self.id
        self.agent = agent

    async def run_command(self, message):
        """Reuse CLI operations and preserve their authorization/promotion guards."""
        command, _, args = message.partition(" ")
        if command == "/skill-eval":
            if self.agent.permission_mode in {"plan", "dontAsk"}:
                raise WebError("当前权限模式不允许写入评测产物，请先切换为默认权限。")
            if not await self.confirm("运行 Skills 评测：将调用模型并在当前项目保存评测产物。"):
                self.emit_text("已取消评测。")
                return True
            from .online_skill_eval import format_online_skill_eval_async
            await self.agent.drain_background_skill_tasks()
            query = self.agent._build_side_query(max_tokens=2400, require_complete=True)
            judge_model = os.getenv("MAWOAGENT_SKILL_JUDGE_MODEL")
            judge = self.agent._build_side_query(max_tokens=2400, require_complete=True, model_override=judge_model) if judge_model else None
            self.emit_text(await format_online_skill_eval_async(side_query=query, judge_side_query=judge))
            return True
        if command == "/skill-apply":
            from .skill_candidates import read_candidate, apply_candidate
            parts = args.split()
            if not parts or len(parts) > 2 or (len(parts) == 2 and parts[1] != "--manual-review"):
                raise WebError("用法：/skill-apply <id> [--manual-review]")
            proposal = read_candidate(parts[0])
            snapshot_hash = hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()
            operation = "skill_evolve" if proposal["action"] == "merge" else "skill_create"
            if await self.agent.authorize_operation(operation, proposal["write_input"]):
                result = apply_candidate(parts[0], manual_review=len(parts) == 2, expected_hash=snapshot_hash)
                self.emit_text(json.dumps(result, ensure_ascii=False, indent=2))
                self.agent._refresh_runtime_system_prompt()
            else:
                self.emit_text("已拒绝发布候选。")
            return True
        if command == "/skill-rollback":
            from .skill_candidates import rollback_skill
            if await self.agent.authorize_operation("skill_evolve", {"skill_name": args.strip()}):
                self.emit_text(json.dumps(rollback_skill(args.strip()), ensure_ascii=False, indent=2))
                self.agent._refresh_runtime_system_prompt()
            else:
                self.emit_text("已拒绝回滚。")
            return True
        return False

    async def run(self, message):
        self.agent._aborted = False
        try:
            if await self.run_command(message):
                self.agent._auto_save()
            else:
                from .skills import get_skill_by_name, execute_skill
                if message.startswith("/"):
                    name, _, args = message[1:].partition(" ")
                    skill = get_skill_by_name(name)
                    if skill and skill.user_invocable:
                        message = (f'Use the skill tool to invoke "{skill.name}" with args: {args or "(none)"}'
                                   if skill.context == "fork" else execute_skill(name, args)["prompt"])
                    else:
                        raise WebError("未知命令或不可手动调用的 Skill。支持 /plan、/skills、/memory、/skill-eval 与 /<skill-name>。")
                await self.agent.chat(message)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.error = self.app.safe_error(exc)
        finally:
            self.finish_thinking()
            self.busy = False
            self.mode = self.agent.permission_mode
            self.app.active = None
            self.changed()


class WebApp:
    def __init__(self, *, options=None):
        api_config = _resolve_api_config(None)
        self.options = options if options is not None else resolve_agent_options(api_config=api_config)
        self.project = Path.cwd().resolve()
        self.policy = load_security_policy(self.project)
        self.workspace = self.policy.workspace_context(self.project)
        self.config = {"project": self.project.name, "path": str(self.project), "model": self.options.get("model", "deepseek-chat"),
                       "protocol": self.options.get("api_protocol", "openai"), "configured": bool(self.options.get("api_key")),
                       "security": self.policy.describe(),
                       "csrf": secrets.token_urlsafe(32)}
        self.auth = secrets.token_urlsafe(32)
        self.conversations = {}
        self.active = None
        self.stopping = False
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def call(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=20)

    def safe_error(self, error):
        value = str(error)
        key = self.options.get("api_key")
        if key:
            value = value.replace(key, "[redacted]")
        return value[:1500]

    def get(self, sid):
        if sid in self.conversations:
            return self.conversations[sid]
        saved = load_session(sid)
        if not saved or Path(saved.get("metadata", {}).get("cwd", "")).resolve() != self.project:
            raise WebError("找不到当前项目中的会话。", 404)
        if len(self.conversations) >= 40:
            raise WebError("已打开过多会话，请重启 Web 服务后继续。", 409)
        view = Conversation(self, sid, saved)
        self.conversations[sid] = view
        return view

    async def dispatch(self, method, path, body=None):
        body = body or {}
        if path == "/api/config" and method == "GET":
            return self.config
        if path == "/api/sessions":
            if method == "GET":
                rows = {sid: {"id": sid, "title": view.title, "busy": view.busy} for sid, view in self.conversations.items()}
                saved_rows = sorted(list_sessions(), key=lambda s: (s.get("updatedAt", s.get("startTime", "")), s.get("updatedAtNs", 0)), reverse=True)
                for item in saved_rows:
                    if Path(item["cwd"]).resolve() == self.project and item.get("id") not in rows:
                        rows[item["id"]] = {"id": item["id"], "title": "历史会话 · " + str(item.get("updatedAt", item.get("startTime", "")))[:16], "busy": False}
                return list(rows.values())[:80]
            if method == "POST":
                if len(self.conversations) >= 40:
                    raise WebError("已打开过多会话，请重启 Web 服务后继续。", 409)
                view = Conversation(self)
                self.conversations[view.id] = view
                return view.snapshot
        if path == "/api/library" and method == "GET":
            from .skills import discover_skills
            from .memory import list_memories
            from .skill_candidates import list_candidates
            from .skill_evolution import get_evolution_dir
            errors = []
            def read_items(label, function):
                try:
                    return function()
                except Exception as exc:
                    errors.append(label + ": " + self.safe_error(exc))
                    return []
            skills = read_items("Skills", lambda: [asdict(skill) for skill in discover_skills()])
            memories = read_items("记忆", lambda: [{"name": m.name, "description": m.description, "type": m.type, "content": m.content, "filename": m.filename} for m in list_memories()])
            candidates = read_items("候选", list_candidates)
            report_path = get_evolution_dir() / "online_eval_report.json"
            report = None
            if report_path.is_file():
                try:
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    errors.append("评测: " + self.safe_error(exc))
            return {"skills": skills, "memories": memories, "candidates": candidates, "report": report, "errors": errors}
        parts = path.strip("/").split("/")
        if len(parts) not in {3, 4} or parts[:2] != ["api", "sessions"]:
            raise WebError("接口不存在。", 404)
        view = self.get(parts[2])
        action = parts[3] if len(parts) == 4 else ""
        if method == "GET" and not action:
            return view.snapshot
        if method != "POST":
            raise WebError("不支持的请求。", 405)
        if action == "chat":
            message = body.get("message")
            if not isinstance(message, str) or not message.strip() or len(message) > 100000:
                raise WebError("请输入任务，长度不能超过 100,000 字符。")
            if self.active:
                raise WebError("已有任务正在执行，请先停止或等待完成。", 409)
            view.ensure_agent()
            if view.requests:
                raise WebError("请先处理仍在等待的授权请求。", 409)
            if view.title == "新会话":
                view.title = message.strip()[:28]
            view.entries.append({"kind": "message", "id": secrets.token_hex(8), "role": "user", "text": message})
            view.error = None
            view.busy = True
            view.files = []
            self.active = view.id
            view.task = asyncio.create_task(view.run(message))
            view.changed()
        elif action == "stop":
            if view.busy:
                view.agent.abort()
                view.task.cancel()
        elif action == "mode":
            mode = body.get("mode")
            if mode not in MODES:
                raise WebError("无效的权限模式。")
            if view.busy or view.requests:
                raise WebError("请等待当前任务完成后切换权限。", 409)
            if view.agent:
                if view.agent.permission_mode == "plan" and mode != "plan":
                    view.agent.toggle_plan_mode()
                if mode == "plan" and view.agent.permission_mode != "plan":
                    view.agent.toggle_plan_mode()
                else:
                    view.agent.permission_mode = mode
                view.agent._refresh_runtime_system_prompt()
            view.mode = mode
            view.changed()
        elif action == "confirm":
            rid = body.get("id")
            if not isinstance(rid, str):
                raise WebError("无效的授权请求 ID。")
            req = view.requests.get(rid)
            if not req or req["future"].done():
                raise WebError("该授权请求已结束。", 409)
            if req["kind"] == "plan":
                choice = body.get("choice")
                if choice not in {"manual-execute", "keep-planning", "cancel"}:
                    raise WebError("无效的计划审核选项。")
                feedback = body.get("feedback", "")
                if not isinstance(feedback, str) or len(feedback) > 4000:
                    raise WebError("修改意见不能超过 4,000 字符。")
                req["future"].set_result({"choice": choice, "feedback": feedback})
            else:
                if type(body.get("allow")) is not bool:
                    raise WebError("必须明确允许或拒绝。")
                req["future"].set_result(body["allow"])
        else:
            raise WebError("接口不存在。", 404)
        return view.snapshot

    async def shutdown(self):
        self.stopping = True
        for view in self.conversations.values():
            if view.task and not view.task.done():
                view.agent.abort()
                view.task.cancel()
        tasks = [view.task for view in self.conversations.values() if view.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(view.agent.aclose() for view in self.conversations.values() if view.agent), return_exceptions=True)
        for view in self.conversations.values():
            view.changed()

    def close(self):
        self.call(self.shutdown())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, WebHandler)


class WebHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def headers_for(self, code, kind="application/json; charset=utf-8", length=None, cookie=False):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if self.close_connection:
            self.send_header("Connection", "close")
        if cookie:
            self.send_header("Set-Cookie", f"mawoagent_web={self.server.app.auth}; HttpOnly; SameSite=Strict; Path=/")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def send_json(self, code, value):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8", errors="replace")
        self.headers_for(code, length=len(data))
        self.wfile.write(data)

    def guard(self, mutation=False, bootstrap=False):
        port = self.server.server_port
        host = self.headers.get("Host", "")
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            raise WebError("只允许本地浏览器访问。", 403)
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + host:
            raise WebError("拒绝跨站请求。", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise WebError("拒绝跨站请求。", 403)
        if not bootstrap:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            auth = cookies.get("mawoagent_web")
            if not auth or not secrets.compare_digest(auth.value, self.server.app.auth):
                raise WebError("请刷新页面重新连接本地服务。", 403)
        if mutation and not secrets.compare_digest(self.headers.get("X-Mawoagent-CSRF", ""), self.server.app.config["csrf"]):
            raise WebError("请求校验失败，请刷新页面。", 403)

    def do_GET(self):
        try:
            parsed = urlsplit(self.path)
            allowed = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                       "/chat-scroll.mjs": ("chat-scroll.mjs", "text/javascript; charset=utf-8"), "/chat-view.mjs": ("chat-view.mjs", "text/javascript; charset=utf-8"),
                       "/conversation-state.mjs": ("conversation-state.mjs", "text/javascript; charset=utf-8")}
            self.guard(bootstrap=parsed.path in allowed)
            if parsed.path in allowed:
                file, kind = allowed[parsed.path]
                data = (ASSETS / file).read_bytes()
                self.headers_for(200, kind, len(data), cookie=parsed.path == "/")
                self.wfile.write(data)
                return
            if parsed.path == "/api/events":
                sid = parse_qs(parsed.query).get("session", [""])[0]
                self.server.app.call(self.server.app.dispatch("GET", "/api/sessions/" + sid))
                view = self.server.app.conversations[sid]
                self.headers_for(200, "text/event-stream; charset=utf-8")
                self.connection.settimeout(20)
                revision = -1
                while not self.server.app.stopping:
                    with view.condition:
                        if revision == view.revision:
                            view.condition.wait(timeout=STREAM_HEARTBEAT_SECONDS)
                    revision, frames = view.stream_updates(revision)
                    if not frames:
                        self.wfile.write(b": heartbeat\n\n")
                    for frame in frames:
                        self.wfile.write(frame)
                    self.wfile.flush()
                self.close_connection = True
                return
            self.send_json(200, self.server.app.call(self.server.app.dispatch("GET", parsed.path)))
        except WebError as exc:
            self.send_json(exc.status, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            self.send_json(500, {"error": self.server.app.safe_error(exc)})

    def _drain_request_body(self):
        """Consume an unread POST body before closing so Windows clients do not get an RST."""
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return
        if 0 < size <= 512000:
            try:
                self.rfile.read(size)
            except (OSError, ValueError):
                pass

    def do_POST(self):
        try:
            self.guard(mutation=True)
            self.connection.settimeout(15)
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 512000:
                raise WebError("请求内容过大或为空。", 413)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise WebError("只接受 JSON 请求。", 415)
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise WebError("无效的请求内容。")
            self.send_json(200, self.server.app.call(self.server.app.dispatch("POST", urlsplit(self.path).path, body)))
        except WebError as exc:
            self._drain_request_body()
            self.close_connection = True
            self.send_json(exc.status, {"error": str(exc)})
        except (ValueError, UnicodeError):
            self.close_connection = True
            self.send_json(400, {"error": "无效的 JSON 请求。"})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:
            self.send_json(500, {"error": self.server.app.safe_error(exc)})


def main():
    parser = argparse.ArgumentParser(description="mawoagent 本地深色 Web 工作台")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    _load_env_file()
    app = WebApp()
    try:
        server = WebServer(("127.0.0.1", args.port), app)
    except OSError:
        app.close()
        raise
    print(f"mawoagent Web → http://127.0.0.1:{args.port}\n按 Ctrl+C 停止；模型配置沿用项目 .env。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        server.server_close()


if __name__ == "__main__":
    main()
