#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Awaitable, Any, TypedDict

import anthropic
import openai

from agents.mcp_client import BorrowedMcpManager, McpManager
from agents.policy import load_security_policy
from agents.snapshots import begin_turn, finish_turn
from agents.workspace import WorkspaceContext, bind_workspace, reset_workspace
from .background_jobs import BackgroundSkillTasks
from .tool_runtime import ToolExecution
from .context_runtime import ContextRuntime
from .streaming import StreamAttempts
from .checkpoints import ToolCheckpoints
from agents.memory import MemoryPrefetch
from agents.prompt import build_system_prompt
from agents.session_memory import (
    FOLD_COMPONENT_SYSTEMS,
    FOLD_SESSION_MEMORY_SYSTEM,
    build_anthropic_transcript,
    build_folding_user_prompt,
    build_openai_transcript,
    format_folded_memory,
    parse_folded_memory,
    parse_fold_component,
)
from agents.session import save_folded_session_memory, save_session
from agents.subagent import get_sub_agent_config
from agents.tools import (
    FileVersion, ToolDef, tool_definitions, execute_tool, CONCURRENCY_SAFE_TOOLS,
    check_permission, get_active_tool_definitions,
)
from agents.ui import (
    print_info, print_divider, print_assistant_text, print_sub_agent_start, print_sub_agent_end,
    start_spinner, stop_spinner, print_cost, print_tool_result, print_confirmation, print_retry,
    print_error,
)


# 指数退避重试


def _is_retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status in (429, 503, 529):
        return True
    msg = str(error)
    if "overloaded" in msg or "ECONNRESET" in msg or "ETIMEDOUT" in msg:
        return True
    return False


def _safe_utf8_text(value: object) -> str:
    return str(value).encode("utf-8", errors="replace").decode("utf-8")


def _sanitize_for_utf8(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_utf8_text(value)
    if isinstance(value, list):
        return [_sanitize_for_utf8(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_for_utf8(item) for item in value)
    if isinstance(value, dict):
        return {
            _sanitize_for_utf8(key): _sanitize_for_utf8(item)
            for key, item in value.items()
        }
    return value


async def _with_retry(fn, max_retries: int = 3):
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as error:
            if attempt >= max_retries or not _is_retryable(error):
                raise
            delay = min(1000 * (2 ** attempt), 30000) / 1000 + (hash(str(time.time())) % 1000) / 1000
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            reason = f"HTTP {status}" if status else (getattr(error, "code", None) or "network error")
            print_retry(attempt + 1, max_retries, reason)
            await asyncio.sleep(delay)

MODEL_CONTEXT = {
    "claude-opus-4-6": 200000,
    "claude-sonnet-4-6": 200000,
    "claude-sonnet-4-20250514": 200000,
    "claude-haiku-4-5-20251001": 200000,
    "claude-opus-4-20250514": 200000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "deepseek-chat":200000
}

def _get_context_windows(model:str)->int:
    return MODEL_CONTEXT.get(model, 32000)


AUTO_COMPACT_THRESHOLD = 0.70


def _get_max_output_tokens(model: str) -> int:
    m = model.lower()
    if "opus-4-6" in m:
        return 64000
    if "sonnet-4-6" in m:
        return 32000
    if any(x in m for x in ("opus-4", "sonnet-4", "haiku-4")):
        return 32000
    return 16384

#转换tool的形式到openai
def _to_openai_tools(tools: list[ToolDef]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


class TokenUsage(TypedDict):
    input: int
    output: int


class RunResult(TypedDict):
    text: str
    tokens: TokenUsage


class Agent(BackgroundSkillTasks, ToolExecution, ContextRuntime, StreamAttempts, ToolCheckpoints):
    def __init__(self,
                 *,
                 permission_mode:str="default",
                 model:str="deepseek-chat",
                 api_base: str | None=None,
                 anthropic_base_url: str | None=None,
                 api_key: str | None=None,
                 api_protocol: str | None=None,
                 thinking: bool=False,
                 max_cost_usd: float | None=None,
                 max_turns: int | None=None,
                 max_output_tokens: int | None=None,
                 input_price_per_million: float=3.0,
                 output_price_per_million: float=15.0,
                 shutdown_timeout: float=5.0,
                 context_window: int | None=None,
                 background_skill_concurrency: int=2,
                 background_skill_queue_limit: int=16,
                 confirm_fn:Callable[[str], Awaitable[bool]] | None=None,
                 custom_system_prompt: str | None=None,
                 custom_tools: list[ToolDef] | None=None,
                 is_sub_agent: bool=False,
                 enable_folding: bool=True,
                 max_fold_limit: int | None=None,
                 fold_memory_mode: str="single_json",
                 auto_compact_threshold: float=AUTO_COMPACT_THRESHOLD,
                 enable_memory_prefetch: bool=True,
                 memory_recall_timeout: float=2.0,
                 enable_mcp: bool=True,
                 enable_online_evolution: bool=True,
                 fold_observer: Callable[[dict[str, Any]], None] | None=None,
                 workspace: WorkspaceContext | None=None,):
        if not 0 < auto_compact_threshold < 1:
            raise ValueError("auto_compact_threshold must be between 0 and 1")
        if max_fold_limit is not None and max_fold_limit < 0:
            raise ValueError("max_fold_limit must be non-negative")
        if fold_memory_mode not in {"single_json", "three_parallel"}:
            raise ValueError("fold_memory_mode must be single_json or three_parallel")
        if max_output_tokens is not None and max_output_tokens < 2:
            raise ValueError("max_output_tokens must be at least 2")
        if api_protocol not in {None, "openai", "anthropic"}:
            raise ValueError("api_protocol must be openai or anthropic")
        if any(not math.isfinite(price) or price < 0 for price in (input_price_per_million, output_price_per_million)):
            raise ValueError("token prices must be finite and non-negative")
        if not math.isfinite(shutdown_timeout) or shutdown_timeout <= 0:
            raise ValueError("shutdown_timeout must be finite and positive")
        if max_cost_usd is not None and (not math.isfinite(max_cost_usd) or max_cost_usd < 0):
            raise ValueError("max_cost_usd must be finite and non-negative")
        if context_window is not None and (type(context_window) is not int or context_window < 1024):
            raise ValueError("context_window must be an integer of at least 1024 tokens")
        if max_turns is not None and (type(max_turns) is not int or max_turns < 0):
            raise ValueError("max_turns must be a non-negative integer")
        if min(background_skill_concurrency, background_skill_queue_limit) < 1:
            raise ValueError("background Skill limits must be positive")
        self.permission_mode = permission_mode
        self.thinking = thinking
        self.model = model
        self.use_openai = api_protocol == "openai" if api_protocol else bool(api_base)
        self.is_sub_agent = is_sub_agent
        self._api_base = api_base
        self._anthropic_base_url = anthropic_base_url
        self._api_key = api_key
        self.enable_folding = enable_folding
        self.max_fold_limit = max_fold_limit
        self.fold_memory_mode = fold_memory_mode
        self.auto_compact_threshold = auto_compact_threshold
        if not math.isfinite(memory_recall_timeout) or memory_recall_timeout <= 0:
            raise ValueError("memory_recall_timeout must be finite and positive")
        self.memory_recall_timeout = memory_recall_timeout
        self.enable_memory_prefetch = enable_memory_prefetch
        self.enable_mcp = enable_mcp
        self.enable_online_evolution = enable_online_evolution
        self.fold_observer = fold_observer
        available_tools = tool_definitions if custom_tools is None else custom_tools
        self.tools = [tool for tool in available_tools if enable_folding or tool["name"] != "compact_context"]
        self.workspace = workspace if workspace is not None else load_security_policy().workspace_context()
        self.max_cost_usd = max_cost_usd
        self.max_turns = max_turns
        self.max_output_tokens = max_output_tokens
        self.input_price_per_million = input_price_per_million
        self.output_price_per_million = output_price_per_million
        self.shutdown_timeout = shutdown_timeout
        self.context_window = context_window or _get_context_windows(model)
        self.background_skill_concurrency = background_skill_concurrency
        self.background_skill_queue_limit = background_skill_queue_limit
        self._background_semaphore = None
        self._background_job_ids: set[str] = set()
        self._budget_parent: Agent | None = None
        # Deferred tools are per instance: one session searching must not widen
        # the schema list another session is running with.
        self._activated_tools: set[str] = set()
        self._active_children: list[Agent] = []
        self._memory_tasks: set[asyncio.Task] = set()
        self.confirm_fn = confirm_fn
        self._custom_system_prompt = custom_system_prompt
        self.effective_window=self.context_window - min(20000, max(512, self.context_window // 4))
        self.session_id = uuid.uuid4().hex[:8]
        self._conversation_id = self.session_id
        self.session_start_time= time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())

        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.side_input_tokens = 0
        self.side_output_tokens = 0
        self.last_input_token_count = 0
        self._last_request_bytes = None
        self.last_finish_reason: str | None = None
        self.current_turns = 0
        self.last_api_call_time = 0


        self._aborted = False
        #存储异步任务
        self._current_task:asyncio.Task | None = None
        #权限白名单
        self._confirmed_paths: set[str] = set()


        # 计划模式”（Plan Mode）状态的变量
        self._pre_plan_mode: str | None=None
        self._plan_file_path: str | None=None
        self._plan_approval_fn : Callable[[str], Awaitable[dict[str, Any]]] | None=None
        self._context_cleared : bool=False

        #思考模式
        self._thinking_mode = self._resolve_thinking_mode()

        #子agent的输出缓存
        self._output_buffer: list[str] | None=None
        self._turn_output_buffer: list[str] | None = None

        # 编辑前读取
        self._read_file_state: dict[str, FileVersion] = {}

        #MCP集成
        self._mcp_manager = McpManager()
        self._mcp_initialized = False

        #记忆回溯
        #记忆agent已经回答过的信息
        self._already_surfaced_memories: set[str] = set()
        #当前会话占用的字节数
        self._session_memory_bytes = 0

        #区分message的历史消息
        self._anthropic_messages: list[dict[str, Any]] = []
        self._openai_messages: list[dict[str, Any]] = []
        self._last_retrieved_skill_reference: dict[str, Any] | None = None
        self._last_retrieved_skill_hits: list[dict[str, Any]] = []
        self._pending_skill_extraction_window: dict[str, Any] | None = None
        self._skill_eval_dialog: list[dict[str, str]] = []
        self._background_skill_tasks: set[asyncio.Task] = set()
        self._skill_feedback_outbox: list[dict] = []
        self._tool_checkpoints: dict[str, dict] = {}
        self._folded_session_memories: list[dict[str, Any]] = []
        self._fold_last_time: float = 0.0
        self._fold_count: int = 0
        self._tool_error_streak: int = 0
        self._same_tool_repeat_count: int = 0
        self._last_tool_name: str = ""

        #构建系统提示词
        self._base_system_prompt = custom_system_prompt or build_system_prompt()

        if self.permission_mode == "plan":
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt

        #初始化大模型客户端
        if self.use_openai:
            self._openai_client = openai.AsyncOpenAI(base_url=api_base, api_key=api_key)
            self._anthropic_client = None
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        else:
            kwargs : dict[str,Any] = {}
            if api_key:
                kwargs["api_key"] = api_key
            if anthropic_base_url:
                kwargs["base_url"] = anthropic_base_url
            self._anthropic_client = anthropic.AsyncAnthropic(**kwargs)
            self._openai_client = None

        self._refresh_runtime_system_prompt()

    #判断返回模型的思考模式
    def _resolve_thinking_mode(self) -> str:
        if not self.thinking:
            return "disabled"
        if not self._model_supports_thinking():
            return "disabled"

        if self._model_supports_adaptive_thinking():
            return "adaptive"
        return "enabled"

    def _model_supports_thinking(self) -> bool:
        m = self.model.lower()
        if "claude-3-" in m or "3-5-" in m or "3-7-" in m:
            return False
        if "claude" in m and any(x in m for x in ("opus", "sonnet", "haiku")):
            return True
        return False
    def _model_supports_adaptive_thinking(self) -> bool:
        m = self.model.lower()
        return "opus-4-6" in m or "sonnet-4-6" in m

    #生成一个用于保存 AI 计划（Plan）的 Markdown 文件的绝对路径。
    def _generate_plan_file_path(self) -> str:
        d = Path.home() / ".mawoagent" / "plans"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"plan-{self.session_id}.md")

    def _build_plan_mode_prompt(self) -> str:
        return f"""

    # Plan Mode Active

    Plan mode is active. You MUST NOT make any edits (except the plan file below), run non-readonly tools, or make any changes to the system.

    ## Plan File: {self._plan_file_path}
    Write your plan incrementally to this file using write_file or edit_file. This is the ONLY file you are allowed to edit.

    ## Workflow
    1. **Explore**: Read code to understand the task. Use read_file, list_files, grep_search.
    2. **Design**: Design your implementation approach. Use the agent tool with type="plan" if the task is complex.
    3. **Write Plan**: Write a structured plan to the plan file including:
       - **Context**: Why this change is needed
       - **Steps**: Implementation steps with critical file paths
       - **Verification**: How to test the changes
    4. **Exit**: Call exit_plan_mode when your plan is ready for user review.

    IMPORTANT: When your plan is complete, you MUST call exit_plan_mode. Do NOT ask the user to approve — exit_plan_mode handles that."""

    #判断当前的任务所有的任务是否完成
    @property
    def is_processing(self)->bool:
        return self._current_task is not None and not self._current_task.done()

    #大模型调用的工厂方法,构建一个用于记忆召回（memory recall）的 sideQuery 可调用对象，兼容anthropic, openai。
    def _build_side_query(self, *, max_tokens: int = 256, require_complete: bool = False, model_override: str | None = None):
        if self._anthropic_client:
            client = self._anthropic_client
            model = model_override or self.model
            async def _sq(system:str, user_message:str)->str:
                self._require_model_budget()
                resp = await client.messages.create(
                    model=model, max_tokens=max(1, int(max_tokens)), system=system,
                messages=[{"role": "user", "content": user_message}],
                )
                usage = getattr(resp, "usage", None)
                self.side_input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
                self.side_output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
                if require_complete and getattr(resp, "stop_reason", None) != "end_turn":
                    raise ValueError("incomplete_evaluation_response")
                text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                if not text.strip():
                    block_types = [str(getattr(b, "type", "")) for b in getattr(resp, "content", [])]
                    logging.warning(
                        "side_query returned empty Anthropic-compatible response: model=%s stop_reason=%s content_block_types=%s",
                        getattr(resp, "model", model),
                        getattr(resp, "stop_reason", ""),
                        block_types,
                    )
                return text
            return _sq
        if self._openai_client:
            client = self._openai_client
            model = model_override or self.model
            async def _sq_openai(system:str, user_message:str)->str:
                self._require_model_budget()
                resp = await client.chat.completions.create(
                    model=model,
                    max_tokens=max(1, int(max_tokens)),
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_message},
                    ],

                )
                usage = getattr(resp, "usage", None)
                self.side_input_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
                self.side_output_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
                if not resp.choices:
                    logging.warning("side_query returned no OpenAI-compatible choices: model=%s", model)
                    return ""
                choice = resp.choices[0]
                if require_complete and getattr(choice, "finish_reason", None) != "stop":
                    raise ValueError("incomplete_evaluation_response")
                content = choice.message.content or ""
                if not content.strip():
                    logging.warning(
                        "side_query returned empty OpenAI-compatible response: model=%s finish_reason=%s message=%s",
                        model,
                        getattr(choice, "finish_reason", ""),
                        choice.message,
                    )
                return content
            return _sq_openai
        return None
    #异步任务取消（Abort）
    def abort(self) -> None:
        self._aborted = True
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    def set_confirm_fn(self, fn:Callable[[str], Awaitable[bool]]) -> None:
        self.confirm_fn = fn

    def set_plan_approval_fn(self, fn:Callable[[str], Awaitable[dict[str, Any]]]) -> None:
        self._plan_approval_fn = fn


    #计划模式开关（“状态切换与现场保护”机制）
    def toggle_plan_mode(self) -> str:
        """Toggle plan mode while preserving the prior mode and current prompt."""
        if self.permission_mode == "plan":
            self.permission_mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] =self._system_prompt
            print_info(f"Exited plan mode -> {self.permission_mode} mode")
            return self.permission_mode
        else:
            self._pre_plan_mode = self.permission_mode
            self.permission_mode = "plan"
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            self._refresh_runtime_system_prompt()
            print_info(f"Entered plan mode. Plan file: {self._plan_file_path}")
            return "plan"

    def get_token_usage(self) -> TokenUsage:
        return {"input":self.total_input_tokens, "output":self.total_output_tokens}

    #主入口

    async def chat(self, user_message: str) -> None:
        self._aborted = False
        self.last_finish_reason = None
        self._turn_output_buffer = []
        # Bound before the task is created so every tool call inherits it.
        recorder, snapshot_token = begin_turn(
            self.session_id, self.workspace.root, enabled=not self.is_sub_agent
        )
        # State such as memory and sessions is keyed by project root; binding it
        # here makes the whole turn, not just each tool call, resolve correctly.
        workspace_token = bind_workspace(self.workspace)
        try:
            self._current_task = asyncio.create_task(self._chat_turn(user_message))
            await self._current_task
        except asyncio.CancelledError:
            user_abort = self._aborted
            self._aborted = True
            # User abort cancels only the inner task; caller cancellation (e.g.
            # an evaluation timeout or parent abort) must continue propagating.
            if not user_abort:
                raise
        finally:
            reset_workspace(workspace_token)
            finish_turn(
                recorder,
                snapshot_token,
                status="interrupted" if self._aborted else "complete",
            )
            self._repair_interrupted_tool_calls()
            self._current_task = None
            self._turn_output_buffer = None
            if not self.is_sub_agent:
                stop_spinner()
                self._auto_save()

    async def _chat_turn(self, user_message: str) -> None:
        self._resume_background_skill_jobs()
        #懒加载MCP服务在第一次chat的时候
        if self.enable_mcp and not self._mcp_initialized and not self.is_sub_agent and self.permission_mode != "plan":
            try:
                await self._mcp_manager.load_and_connect(authorize_start=self.authorize_operation)
                mcp_defs = self._mcp_manager.get_tool_definitions()
                if mcp_defs:
                    self.tools = self.tools + mcp_defs
                self._mcp_initialized = True
            except asyncio.CancelledError:
                await self._mcp_manager.disconnect_all()
                raise
            except Exception as e:
                print_error(f"MCP init failed: {e}")

        original_user_message = _safe_utf8_text(user_message)
        self._last_retrieved_skill_reference = None
        self._last_retrieved_skill_hits = []
        if not self.is_sub_agent:
            self._capture_skill_feedback(original_user_message)
            self._flush_skill_feedback()
            user_message, self._last_retrieved_skill_reference = self._augment_user_message_with_skill_context(
                original_user_message
            )

        coro = self._chat_openai(user_message) if self.use_openai else self._chat_anthropic(user_message)
        await coro
        assistant_text = "".join(self._turn_output_buffer or []).strip()
        if not self.is_sub_agent and not self._aborted and self.last_finish_reason in {"stop", "end_turn"}:
            usage_hits = [dict(hit) for hit in self._last_retrieved_skill_hits]
            self._schedule_background_skill_task(self._run_skill_usage_tracking(
                original_user_message, assistant_text,
                hits=usage_hits,
            ), job={"kind": "usage", "user": original_user_message, "assistant": assistant_text, "hits": usage_hits})
            self._set_pending_skill_extraction_window(
                original_user_message=original_user_message,
                assistant_text=assistant_text,
                retrieved_reference=self._last_retrieved_skill_reference,
            )
        if not self.is_sub_agent:
            print_divider()

    def _repair_interrupted_tool_calls(self) -> None:
        self._recover_tool_checkpoints()


   #执行一次对话，收集本轮模型输出文本，并返回本轮消耗的 token 数
    async def run_once(self, prompt: str) -> RunResult:
        self._output_buffer = []
        prev_in = self.total_input_tokens
        prev_out = self.total_output_tokens
        try:
            await self.chat(prompt)
            text = "".join(self._output_buffer)
        finally:
            self._output_buffer = None
        return {
            "text": text,
            "tokens":{
                "input":self.total_input_tokens-prev_in,
                "output":self.total_output_tokens-prev_out
            },
        }

    #输出工具：统一处理模型输出文本。根据当前是否处于“收集输出”的模式
    # 决定是把文本存进缓冲区，还是直接打印到终端。
    def _emit_text(self, text:str)->None:
        text = _safe_utf8_text(text)
        if self._turn_output_buffer is not None:
            self._turn_output_buffer.append(text)
        if self._output_buffer is not None:
            self._output_buffer.append(text)
        else:
            print_assistant_text(text)

    def _emit_thinking(self, text: str) -> None:
        """Optional adapter hook; reasoning never enters answer/learning buffers."""
        pass

    def _build_fold_guidance_section(self) -> str:
        if self._custom_system_prompt is not None or not self.enable_folding:
            return ""
        if self._fold_limit_reached():
            return "\n\n# Runtime Fold Guidance\n- Context folding limit reached. Continue without calling `compact_context`.\n"
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0.0
        last_fold = "never" if not self._fold_last_time else f"{int((time.time() - self._fold_last_time) / 60)}m ago"
        return (
            "\n\n# Runtime Fold Guidance\n"
            f"- Current context utilization: {utilization:.0%}\n"
            f"- Recent tool error streak: {self._tool_error_streak}\n"
            f"- Same tool repeat count: {self._same_tool_repeat_count}\n"
            f"- Last fold: {last_fold}\n"
            "- If the context is getting long, the same tool is being retried without progress, or tool failures are accumulating, call `compact_context` before trying more tools.\n"
            "- If you folded very recently and the next step is clear, prefer continuing rather than folding again.\n"
        )

    def _refresh_runtime_system_prompt(self) -> None:
        if self._custom_system_prompt is not None:
            return
        self._base_system_prompt = build_system_prompt()
        if self.permission_mode == "plan":
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt
        self._system_prompt += self._build_fold_guidance_section()
        if self.use_openai and self._openai_messages:
            self._openai_messages[0]["content"] = self._system_prompt

    def _record_tool_outcome(self, tool_name: str, success: bool) -> None:
        if tool_name == self._last_tool_name:
            self._same_tool_repeat_count += 1
        else:
            self._same_tool_repeat_count = 1
        self._last_tool_name = tool_name
        if success:
            self._tool_error_streak = 0
        else:
            self._tool_error_streak += 1

    def _record_fold_event(self) -> None:
        self._fold_last_time = time.time()
        self._fold_count += 1
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""

    def _looks_like_tool_failure(self, tool_name: str, raw: str, result: str) -> bool:
        text = f"{raw}\n{result}".lower()
        if any(marker in text for marker in ("error", "denied", "timed out", "timeout")):
            return True
        if tool_name == "compact_context" and "no context compaction" in text:
            return True
        return False

    def _augment_user_message_with_skill_context(self, user_message: str) -> tuple[str, dict[str, Any] | None]:
        try:
            from .skills import format_retrieved_skill_context

            context, top_ref = format_retrieved_skill_context(user_message, limit=3)
        except Exception:
            return user_message, None
        if top_ref and isinstance(top_ref.get("all_hits"), list):
            self._last_retrieved_skill_hits = list(top_ref.get("all_hits") or [])
        if not context.strip():
            return user_message, top_ref
        return f"{user_message}\n\n{context}", top_ref

    def _strip_runtime_injections(self, text: str) -> str:
        return re.sub(r"\n*<retrieved_skills>.*?</retrieved_skills>\s*", "", str(text or ""), flags=re.DOTALL).strip()

    def _message_text(self, msg: dict[str, Any]) -> str:
        content = msg.get("content")
        if isinstance(content, str):
            return self._strip_runtime_injections(content)
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(str(block.get("text") or ""))
                    elif "content" in block and block.get("type") not in {"tool_result", "tool_use"}:
                        parts.append(str(block.get("content") or ""))
            return self._strip_runtime_injections("\n".join(parts))
        return ""

    def _recent_dialog_messages(self, *, max_messages: int = 8) -> list[dict[str, str]]:
        raw_messages = self._openai_messages if self.use_openai else self._anthropic_messages
        out: list[dict[str, str]] = []
        for msg in raw_messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            text = self._message_text(msg)
            if text:
                out.append({"role": role, "content": text})
        return out[-max(2, int(max_messages)) :]

    async def authorize_operation(self, tool_name: str, inp: dict, *, interactive: bool = True) -> bool:
        """Apply the same permission policy to operations outside the model tool loop."""
        permission = check_permission(tool_name, inp, self.permission_mode, self._plan_file_path)
        if permission["action"] == "allow":
            return True
        if permission["action"] == "confirm" and interactive and self.confirm_fn is not None:
            return await self._confirm_tool_call(tool_name, permission)
        return False

    async def _confirm_online_skill_write(self, tool_name: str, inp: dict) -> bool:
        return await self.authorize_operation(tool_name, inp)

    async def _confirm_background_online_skill_write(self, tool_name: str, inp: dict) -> bool:
        return await self.authorize_operation(tool_name, inp, interactive=False)

    def _online_evolution_enabled(self) -> bool:
        if not self.enable_online_evolution:
            return False
        raw = os.environ.get("MAWOAGENT_AUTO_SKILL_EVOLUTION", "1").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    async def aclose(self) -> None:
        """Finish bounded background work and release every owned resource."""
        try:
            await self.drain_background_skill_tasks(cancel_on_timeout=True)
        finally:
            from .storage import write_json
            for path in self._background_jobs_dir().glob("*.json"):
                try:
                    job = json.loads(path.read_text(encoding="utf-8"))
                    if job.get("owner_session") == self.session_id and job.get("status") != "running":
                        job["owner_pid"] = None
                        write_json(path, job)
                except (ValueError, OSError, AttributeError, TypeError):
                    logging.exception("Unable to release persisted Skill job %s", path)
            for task in self._memory_tasks:
                task.cancel()
            await asyncio.gather(*self._memory_tasks, return_exceptions=True)
            self._memory_tasks.clear()
            try:
                await self._mcp_manager.disconnect_all()
            finally:
                if self._openai_client:
                    await self._openai_client.close()
                if self._anthropic_client:
                    await self._anthropic_client.close()

    def _track_memory_prefetch(self, prefetch: MemoryPrefetch | None) -> None:
        if prefetch:
            self._memory_tasks.add(prefetch.task)
            def done(task):
                self._memory_tasks.discard(task)
                if not task.cancelled() and task.exception():
                    logging.warning("Memory prefetch failed: %s", task.exception())
            prefetch.task.add_done_callback(done)

    def _pop_pending_skill_extraction_window(self, next_user_feedback: str) -> dict[str, Any] | None:
        pending = self._pending_skill_extraction_window
        self._pending_skill_extraction_window = None
        if not pending:
            return None
        messages = list(pending.get("messages") or [])
        feedback = _safe_utf8_text(next_user_feedback).strip()
        if feedback:
            messages.append({"role": "user", "content": feedback})
        pending["messages"] = messages[-10:]
        pending["next_user_feedback"] = feedback
        return pending

    def _set_pending_skill_extraction_window(
        self,
        *,
        original_user_message: str,
        assistant_text: str,
        retrieved_reference: dict[str, Any] | None,
    ) -> None:
        if not original_user_message.strip() or not assistant_text.strip():
            return
        self._pending_skill_extraction_window = {
            "messages": self._recent_dialog_messages(max_messages=8),
            "latest_user": original_user_message,
            "latest_assistant": assistant_text,
            "retrieved_reference": self._compact_retrieved_reference(retrieved_reference),
            "session_id": self.session_id,
            "replay_context": {
                "schema_version": 2,
                "event_id": str(uuid.uuid4()),
                "session_id": getattr(self, "_conversation_id", self.session_id),
                "task_user": original_user_message,
                "context_messages": list(self._skill_eval_dialog),
                "original_response": assistant_text,
            },
        }
        # Keep original turns, without retrieved Skill text or tool messages.
        self._skill_eval_dialog = (self._skill_eval_dialog + [
            {"role": "user", "content": original_user_message},
            {"role": "assistant", "content": assistant_text},
        ])[-8:]

    def _compact_retrieved_reference(self, ref: dict[str, Any] | None) -> dict[str, Any] | None:
        if not ref:
            return None
        return {k: v for k, v in ref.items() if k != "all_hits"}

    async def _run_online_skill_evolution(self, window: dict[str, Any], *, interactive_confirm: bool = False) -> dict:
        if not self._online_evolution_enabled() or self.permission_mode == "plan":
            return {"ok": False, "error": "online skill extraction is disabled or blocked in plan mode"}
        messages = list(window.get("messages") or [])
        if not messages:
            return {"ok": False, "error": "no extraction messages"}

        side_query = self._build_side_query(max_tokens=2200, require_complete=True)
        if side_query is None:
            return {"ok": False, "error": "no model client available"}

        try:
            from .online_skill_evolution import online_ingest
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        result = await online_ingest(
            messages=messages,
            replay_context={**window.get("replay_context", {}),
                            "feedback": str(window.get("next_user_feedback") or "")},
            side_query=side_query,
            retrieved_reference=window.get("retrieved_reference") or None,
            hint=str(window.get("hint") or ""),
            confirm_write=self._confirm_online_skill_write if interactive_confirm else self._confirm_background_online_skill_write,
            target=os.environ.get("MAWOAGENT_AUTO_SKILL_TARGET", "project"),
            stage_only=True,
        )
        if result.get("ok"):
            if result.get("action") in {"add", "merge"}:
                self._refresh_runtime_system_prompt()
                print_info(f"Online skill {result.get('action')}: {result.get('skill')}")
            elif result.get("action") in {"add_staged", "merge_staged"}:
                print_info(f"Skill candidate saved: {result.get('candidate_id')} ({result.get('skill')}). Review with /skill-candidates before /skill-apply.")
        elif result.get("action") not in {"add_denied", "merge_denied"}:
            print_error(f"Online skill evolution failed: {result.get('error') or result}")
        return result

    async def _run_skill_usage_tracking(self, original_user_message: str, assistant_text: str,
                                        *, hits: list[dict[str, Any]] | None = None) -> None:
        if not self._online_evolution_enabled() or self.permission_mode == "plan":
            return {"ok": False, "error": "Skill usage tracking disabled"}
        hits = list(self._last_retrieved_skill_hits if hits is None else hits)
        if not hits or not assistant_text.strip():
            return
        side_query = self._build_side_query(max_tokens=700)
        try:
            from .online_skill_evolution import judge_retrieved_skill_usage
            from .skills import record_usage_judgments

            judgments = await judge_retrieved_skill_usage(
                hits=hits,
                user_message=original_user_message,
                assistant_text=assistant_text,
                side_query=side_query,
            )
            permission = check_permission("skill_prune", {}, self.permission_mode, self._plan_file_path)
            result = record_usage_judgments(judgments, allow_prune=permission["action"] == "allow")
            if result.get("pruned"):
                self._refresh_runtime_system_prompt()
            return result
        except Exception:
            logging.exception("Skill usage tracking failed")
            return {"ok": False, "error": "Skill usage tracking failed"}

    async def extract_now(self, hint: str = "") -> dict[str, Any]:
        pending = self._pending_skill_extraction_window
        if not pending:
            return {"ok": False, "error": "no pending online skill extraction window"}
        window = dict(pending)
        window["hint"] = hint
        result = await self._run_online_skill_evolution(window, interactive_confirm=True)
        if result.get("ok"):
            self._pending_skill_extraction_window = None
        self._auto_save()
        return result


    def clear_history(self) -> None:
        self._clear_history_keep_system()
        self._skill_eval_dialog = []
        self._skill_feedback_outbox = []
        self._tool_checkpoints = {}
        self._pending_skill_extraction_window = None
        self._last_retrieved_skill_reference = None
        self._last_retrieved_skill_hits = []
        self._already_surfaced_memories.clear()
        self._session_memory_bytes = 0
        self._folded_session_memories = []
        print_info("Conversation cleared.")

    def show_cost(self):
        total = self._get_current_cost_usd()
        budget_info = f" / ${self.max_cost_usd} budget" if self.max_cost_usd else ""
        turn_info = f" | Turns: {self.current_turns}/{self.max_turns}" if self.max_turns else ""
        print_info(
            f"Tokens: {self.total_input_tokens + self.side_input_tokens} in / {self.total_output_tokens + self.side_output_tokens} out\n  Estimated cost: ${total:.4f}{budget_info}{turn_info}")

    #获取当前的花费，
    def _get_current_cost_usd(self) -> float:
        own = ((self.total_input_tokens + self.side_input_tokens) * self.input_price_per_million +
               (self.total_output_tokens + self.side_output_tokens) * self.output_price_per_million) / 1_000_000
        return own + sum(child._get_current_cost_usd() for child in self._active_children)

    def _require_model_budget(self) -> None:
        budget = self._check_budget()
        if budget["exceeded"]:
            raise RuntimeError(f"Model request blocked: {budget['reason']}")

    #检查预算
    def _check_budget(self) -> dict:
        if self._budget_parent is not None:
            parent_budget = self._budget_parent._check_budget()
            if parent_budget["exceeded"]:
                return parent_budget
        if self.max_cost_usd is not None and self._get_current_cost_usd() >= self.max_cost_usd:
            return {"exceeded": True, "reason": f"Cost limit reached (${self._get_current_cost_usd():.4f} >= ${self.max_cost_usd})"}
        if self.max_turns is not None and self.current_turns >= self.max_turns:
            return {"exceeded": True, "reason": f"Turn limit reached ({self.current_turns} >= {self.max_turns})"}
        return {"exceeded": False}

    #压缩会话
    async def compact(self)->None:
        if not self.enable_folding:
            print_info("Context folding is disabled for this run.")
            return
        compacted = await self._compact_conversation(trigger="manual")
        if not compacted:
            print_info("Compaction skipped; conversation history was retained.")


    #恢复会话信息
    def restore_session(self, data:dict)->None:
        if not isinstance(data, dict) or not isinstance(data.get("metadata"), dict):
            raise ValueError("Invalid session metadata")
        if not isinstance(data.get("runtimeState", {}), dict):
            raise ValueError("Invalid session runtime state")
        for key in ("openaiMessages", "anthropicMessages"):
            messages = data.get(key)
            if messages is not None and (not isinstance(messages, list) or any(not isinstance(msg, dict) for msg in messages)):
                raise ValueError(f"Invalid session message list: {key}")
        metadata = data.get("metadata") or {}
        if not metadata.get("cwd") or Path(metadata["cwd"]).resolve() != Path.cwd().resolve():
            raise ValueError("Session belongs to another project or has no project metadata")
        saved_protocol = metadata.get("apiProtocol") or ("openai" if data.get("openaiMessages") else "anthropic")
        if saved_protocol != ("openai" if self.use_openai else "anthropic"):
            raise ValueError("Session API protocol differs from the current configuration")
        if data.get("anthropicMessages"):
            self._anthropic_messages = _sanitize_for_utf8(data["anthropicMessages"])
        if data.get("openaiMessages"):
            self._openai_messages = _sanitize_for_utf8(data["openaiMessages"])
        if isinstance(data.get("foldedSessionMemories"), list):
            self._folded_session_memories = _sanitize_for_utf8(data["foldedSessionMemories"])
        state = data.get("runtimeState") or {}
        outbox = state.get("skillFeedbackOutbox", [])
        if not isinstance(outbox, list) or any(not isinstance(item, dict) for item in outbox):
            raise ValueError("Invalid session feedback outbox")
        self._skill_feedback_outbox = _sanitize_for_utf8(outbox)
        journal = state.get("toolCheckpoints", {})
        self._tool_checkpoints = {str(key): value for key, value in journal.items() if isinstance(value, dict)} if isinstance(journal, dict) else {}
        self._recover_tool_checkpoints()
        if not self.use_openai:
            self._anthropic_messages = self._normalize_anthropic_messages(self._anthropic_messages)
        self._conversation_id = str(state.get("conversationId") or metadata.get("id") or self.session_id)
        dialog = state.get("skillEvalDialog")
        if isinstance(dialog, list):
            self._skill_eval_dialog = [dict(role=item["role"], content=_safe_utf8_text(item["content"]))
                for item in dialog if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
                and isinstance(item.get("content"), str)][-8:]
        else:
            self._skill_eval_dialog = self._recent_dialog_messages(max_messages=8)
        pending = state.get("pendingSkillExtractionWindow")
        self._pending_skill_extraction_window = _sanitize_for_utf8(pending) if isinstance(pending, dict) else None
        if state.get("permissionMode") == "plan" and self.permission_mode == "default":
            self.permission_mode = "plan"
            candidate = state.get("planFilePath")
            plans_dir = (Path.home() / ".mawoagent" / "plans").resolve()
            self._plan_file_path = (candidate if isinstance(candidate, str) and Path(candidate).is_absolute()
                                   and Path(candidate).resolve().parent == plans_dir else self._generate_plan_file_path())
            self._pre_plan_mode = "default"
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        # Retain the current project's prompt/configuration on resume.
        if self.use_openai and self._openai_messages and self._openai_messages[0].get("role") == "system":
            self._openai_messages[0]["content"] = self._system_prompt
        self._fold_count = len(self._folded_session_memories)
        print_info(f"Session restored ({self._get_message_count()} messages).")



#整理 Anthropic 的历史消息，修正部分角色错误，并丢弃不合法的工具调用消息。
    def _normalize_anthropic_messages(self, messages: list[dict]) -> list[dict]:
        role_normalized = []
        for msg in messages:
            copied = dict(msg)
            content = copied.get("content")
            if copied.get("role") == "user" and isinstance(content, list):
                if any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content):
                    copied["role"] = "assistant"
            role_normalized.append(copied)

        normalized = []
        i = 0
        while i < len(role_normalized):
            msg = role_normalized[i]
            tool_use_ids = self._anthropic_tool_use_ids(msg)
            if not tool_use_ids:
                normalized.append(msg)
                i += 1
                continue

            next_msg = role_normalized[i + 1] if i + 1 < len(role_normalized) else None
            result_ids = self._anthropic_tool_result_ids(next_msg) if next_msg else set()
            if tool_use_ids.issubset(result_ids):
                normalized.append(msg)
                normalized.append(next_msg)
                i += 2
                continue

            i += 1
        return normalized

    @staticmethod
    def _anthropic_tool_use_ids(msg: dict | None) -> set[str]:
        if not msg or msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
            return set()
        return {
            block.get("id")
            for block in msg["content"]
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
        }

    @staticmethod
    def _anthropic_tool_result_ids(msg: dict | None) -> set[str]:
        if not msg or msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            return set()
        return {
            block.get("tool_use_id")
            for block in msg["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id")
        }

    def _get_message_count(self) -> int:
        return len(self._openai_messages) if self.use_openai else len(self._anthropic_messages)

    def _auto_save(self, *, strict: bool = False) -> None:
        try:
            save_session(self.session_id, {
                "metadata": {
                    "id": self.session_id,
                    "model": self.model,
                    "apiProtocol": "openai" if self.use_openai else "anthropic",
                    "cwd": str(Path.cwd()),
                    "startTime": self.session_start_time,
                    "messageCount": self._get_message_count(),
                },
                "anthropicMessages": _sanitize_for_utf8(self._anthropic_messages) if not self.use_openai else None,
                "openaiMessages": _sanitize_for_utf8(self._openai_messages) if self.use_openai else None,
                "foldedSessionMemories": _sanitize_for_utf8(self._folded_session_memories),
                "runtimeState": _sanitize_for_utf8({
                    "conversationId": self._conversation_id,
                    "skillEvalDialog": self._skill_eval_dialog,
                    "pendingSkillExtractionWindow": self._pending_skill_extraction_window,
                    "skillFeedbackOutbox": self._skill_feedback_outbox,
                    "toolCheckpoints": self._tool_checkpoints,
                    "permissionMode": self.permission_mode,
                    "planFilePath": self._plan_file_path,
                }),
            })
        except Exception:
            logging.exception("Failed to save session %s", self.session_id)
            if strict:
                raise

    #自动压缩
    async def _check_and_compact(self)->None:
        if not self._aborted and self.enable_folding and not self._fold_limit_reached() and self._estimated_input_tokens() > self.effective_window * self.auto_compact_threshold:
            print_info("Context window filling up, compacting conversation...")
            await self._compact_conversation(trigger="auto")

    async def _compact_conversation(self, *, trigger: str = "manual")->bool:
        if not self.enable_folding or self._fold_limit_reached():
            return False
        if self.use_openai:
            compacted = await self._compact_openai(trigger=trigger)
        else:
            compacted = await self._compact_anthropic(trigger=trigger)
        if compacted:
            print_info("Conversation compacted.")
        return compacted

    def _fold_limit_reached(self) -> bool:
        return self.max_fold_limit is not None and self._fold_count >= self.max_fold_limit

    async def _compact_anthropic(self, *, trigger: str)->bool:
        if len (self._anthropic_messages)<4:
            return False

        transcript = build_anthropic_transcript(_sanitize_for_utf8(self._anthropic_messages))
        if not transcript.strip():
            return False
        memory = await self._generate_folded_session_memory(transcript)
        if memory is None:
            return False
        self._notify_fold_observer(trigger, transcript, memory)
        self._record_folded_session_memory(trigger, memory)
        self._record_fold_event()
        self._anthropic_messages = [{"role": "user", "content": format_folded_memory(memory)}]
        self.last_input_token_count = 0
        self._last_request_bytes = None
        self._refresh_runtime_system_prompt()
        return True

    async def _compact_openai(self, *, trigger: str)->bool:
        if len (self._openai_messages)<4:
            return False
        system_msg = self._openai_messages[0]
        transcript = build_openai_transcript(_sanitize_for_utf8(self._openai_messages))
        if not transcript.strip():
            return False
        memory = await self._generate_folded_session_memory(transcript)
        if memory is None:
            return False
        self._notify_fold_observer(trigger, transcript, memory)
        self._record_folded_session_memory(trigger, memory)
        self._record_fold_event()
        self._openai_messages=[
            system_msg,
            {"role": "user", "content": format_folded_memory(memory)},
        ]
        self.last_input_token_count=0
        self._last_request_bytes = None
        self._refresh_runtime_system_prompt()
        return True

    async def _generate_folded_session_memory(self, transcript: str) -> dict[str, Any] | None:
        try:
            parallel = self.fold_memory_mode == "three_parallel"
            side_query = self._build_side_query(max_tokens=2500 if parallel else 6000, require_complete=True)
            if side_query is None:
                raise ValueError("No model client available for compaction")
            if parallel:
                prompt = "Extract the requested memory from this conversation.\n\nConversation transcript:\n" + transcript
                names = tuple(FOLD_COMPONENT_SYSTEMS)
                results = await asyncio.gather(
                    *(side_query(FOLD_COMPONENT_SYSTEMS[name], prompt) for name in names),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
                memory = {name: parse_fold_component(name, raw) for name, raw in zip(names, results)}
                return parse_folded_memory(json.dumps(memory))
            raw = await side_query(FOLD_SESSION_MEMORY_SYSTEM, build_folding_user_prompt(transcript))
            return parse_folded_memory(raw)
        except Exception as exc:
            logging.warning("Compaction failed; retaining original conversation: %s", exc)
            return None

    def _notify_fold_observer(self, trigger: str, transcript: str, memory: dict[str, Any]) -> None:
        if self.fold_observer is None:
            return
        try:
            self.fold_observer({
                "trigger": trigger,
                "fold_memory_mode": self.fold_memory_mode,
                "transcript": transcript,
                "memory": memory,
                "pre_fold_input_tokens": self.last_input_token_count,
            })
        except Exception:
            logging.exception("fold observer failed")

    def _record_folded_session_memory(self, trigger: str, memory: dict[str, Any]) -> None:
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trigger": trigger,
            "fold_memory_mode": self.fold_memory_mode,
            "session_id": self.session_id,
            "pre_fold_input_tokens": self.last_input_token_count,
            "effective_window": self.effective_window,
            "folded_memory_characters": len(format_folded_memory(memory)),
            **memory,
        }
        self._folded_session_memories.append(record)
        try:
            save_folded_session_memory(self.session_id, _sanitize_for_utf8(record))
        except Exception:
            pass

    #大结果持久化
    #如果工具返回的结果太大（超过 30KB），不要硬塞进上下文里，而是把它存成一个临时文件。
    # 然后在对话里只留一个‘文件路径’和‘内容预览’。如果模型后面还需要看完整内容，它可以再次调用工具去读取这个文件

    def _persist_large_result(self, tool_name: str, result: str) -> str:
        from .result_storage import persist_tool_result
        return persist_tool_result(tool_name, result)["result"]

    #执行工具入口

    async def _execute_tool_call(self, name: str, inp: dict) -> str:
        if name not in {tool["name"] for tool in self.tools} and not self._mcp_manager.is_mcp_tool(name):
            return f"Tool not available in this run: {name}"
        if name == "compact_context":
            return await self._execute_compact_context_tool(inp)
        if name in ("enter_plan_mode", "exit_plan_mode"):
            return await self._execute_plan_mode_tool(name)
        if name == "agent":
            return await self._execute_agent_tool(inp)
        if name == "skill":
            return await self._execute_skill_tool(inp)
            # Route MCP tool calls to the MCP manager
        if self._mcp_manager.is_mcp_tool(name):
            return await self._mcp_manager.call_tool(name, inp)
        result = await execute_tool(name, inp, self._read_file_state, truncate_result=False,
                                    permission_mode=self.permission_mode,
                                    activated=self._activated_tools)
        if name in {"skill_create", "skill_evolve"}:
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and parsed.get("ok"):
                    self._refresh_runtime_system_prompt()
            except Exception:
                pass
        return result

    async def _execute_compact_context_tool(self, inp: dict) -> str:
        if not self.enable_folding:
            return "Context folding is disabled for this run. Continue without compacting."
        if self._fold_limit_reached():
            return f"Context folding limit reached ({self.max_fold_limit}). Continue without compacting."
        reason = str(inp.get("reason") or "").strip()
        compacted = await self._compact_conversation(trigger="tool")
        if not compacted:
            self._record_tool_outcome("compact_context", False)
            return "Compaction skipped: insufficient history or memory generation failed validation. Original conversation history was retained."
        self._record_tool_outcome("compact_context", True)
        self._context_cleared = True
        suffix = f"\nReason: {reason}" if reason else ""
        return (
            "Context compacted into structured session memory. "
            "Continue from the folded memory now present in the conversation context."
            f"{suffix}"
        )


    async def _execute_skill_tool(self, inp: dict) -> str:
        from .skills import execute_skill
        result = execute_skill(inp.get("skill_name", ""), inp.get("args", ""))

        if not result:
            return f"Unknown skill: {inp.get('skill_name', '')}"

        if result["context"] == "fork":
            allowed_tools = result.get("allowed_tools")
            tools = self.tools if allowed_tools is None else [
                tool for tool in self.tools if tool["name"] in allowed_tools
            ]
            return await self._run_child_agent(
                agent_type="skill-fork",
                description=inp.get("skill_name", ""),
                system_prompt=result["prompt"],
                tools=tools,
                prompt=inp.get("args") or "Execute this skill task.",
                label="Skill fork",
                empty_result="(Skill produced no output)",
            )

        return f'[Skill "{inp.get("skill_name", "")}" activated]\n\n{result["prompt"]}'

    async def _execute_plan_mode_tool(self, name: str) -> str:
        if name == "enter_plan_mode":
            if self.permission_mode == "plan":
                return "Already in plan mode."
            self._pre_plan_mode = self.permission_mode
            self.permission_mode = "plan"
            self._plan_file_path =  self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt
            print_info("Entered plan mode (read-only). Plan file: " + self._plan_file_path)
            return f"Entered plan mode. You are now in read-only mode.\n\nYour plan file: {self._plan_file_path}\nWrite your plan to this file. This is the only file you can edit.\n\nWhen your plan is complete, call exit_plan_mode."
        if name == "exit_plan_mode":
            if self.permission_mode != "plan":
                return "Not in plan mode."
            plan_content = "(No plan file found)"
            if self._plan_file_path and Path(self._plan_file_path).exists():
                try:
                    plan_content = Path(self._plan_file_path).read_text(encoding="utf-8")
                except Exception as exc:
                    plan_content = f"(Unable to read plan file: {exc})\n\nPlan file: {self._plan_file_path}"
            # 交互式审批流程（如果有审批函数）
            if self._plan_approval_fn:
                result = await self._plan_approval_fn(plan_content)
                choice = result.get("choice") if isinstance(result, dict) else None
                if not isinstance(choice, str):
                    choice = None

                if choice =="keep-planning":
                    feedback = result.get("feedback") or "Please revise the plan."
                    return (
                        f"User rejected the plan and wants to keep planning.\n\n"
                        f"User feedback: {feedback}\n\n"
                        f"Please revise your plan based on this feedback. When done, call exit_plan_mode again."
                    )

                if choice == "plan-only":
                    self._aborted = True
                    return (
                        f"Plan completed. One-shot plan mode does not execute changes.\n\n"
                        f"Plan file: {self._plan_file_path}\n\n"
                        f"## Plan:\n{plan_content}"
                    )

                if choice not in {"clear-and-execute", "execute", "manual-execute"}:
                    self._aborted = True
                    return "Plan approval cancelled or invalid. Remaining in plan mode; no implementation authorized."

                if choice == "clear-and-execute":
                    target_mode = "acceptEdits"
                elif choice == "execute":
                    target_mode = "acceptEdits"
                else:  # manual-execute
                    target_mode = self._pre_plan_mode or "default"

                #离开计划模式
                self.permission_mode = target_mode
                self._pre_plan_mode = None
                saved_plan_path = self._plan_file_path
                self._plan_file_path = None
                self._system_prompt = self._base_system_prompt
                self._refresh_runtime_system_prompt()
                if self.use_openai and self._openai_messages:
                    self._openai_messages[0]["content"] = self._system_prompt

                if choice == "clear-and-execute":
                    self._clear_history_keep_system()
                    self._context_cleared = True
                    print_info(f"Plan approved. Context cleared, executing in {target_mode} mode.")
                    return (
                        f"User approved the plan. Context was cleared. Permission mode: {target_mode}\n\n"
                        f"Plan file: {saved_plan_path}\n\n"
                        f"## Approved Plan:\n{plan_content}\n\n"
                        f"Proceed with implementation."
                    )
                print_info(f"Plan approved. Executing in {target_mode} mode.")
                return (
                    f"User approved the plan. Permission mode: {target_mode}\n\n"
                    f"## Approved Plan:\n{plan_content}\n\n"
                    f"Proceed with implementation."
                )
            self._aborted = True
            return "No plan approval handler available. Remaining in plan mode; no implementation authorized."

        return f"Unknown plan mode tool: {name}"

    def _clear_history_keep_system(self) -> None:
        """清空历史信息，但是保留系统prompt."""
        self._anthropic_messages = []
        self._openai_messages = []
        if self.use_openai:
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        self.last_input_token_count = 0
        self._last_request_bytes = None
        self._fold_last_time = 0.0
        self._fold_count = 0
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""

    async def _execute_agent_tool(self, inp: dict) -> str:
        try:
            config = get_sub_agent_config(inp.get("type", "general"))
        except ValueError as exc:
            # An agent type defined by an untrusted checkout is listed but not runnable.
            return f"Error: {exc}"
        return await self._run_child_agent(
            agent_type=inp.get("type", "general"),
            description=inp.get("description", "sub-agent task"),
            system_prompt=config["system_prompt"],
            tools=config["tools"],
            prompt=inp.get("prompt", ""),
            label="Sub-agent",
            empty_result="(Sub-agent produced no output)",
        )

    def _create_child_agent(self, *, system_prompt: str, tools: list[ToolDef]) -> Agent:
        """Inherit runtime limits and permissions consistently for every child."""
        forbidden = {"agent", "enter_plan_mode", "exit_plan_mode"}
        child_tools = [tool for tool in tools if tool["name"] not in forbidden]
        child = Agent(
            model=self.model,
            api_base=self._api_base,
            anthropic_base_url=self._anthropic_base_url,
            api_key=self._api_key,
            api_protocol="openai" if self.use_openai else "anthropic",
            input_price_per_million=self.input_price_per_million,
            output_price_per_million=self.output_price_per_million,
            shutdown_timeout=self.shutdown_timeout,
            context_window=self.context_window,
            max_output_tokens=self.max_output_tokens,
            memory_recall_timeout=self.memory_recall_timeout,
            background_skill_concurrency=self.background_skill_concurrency,
            background_skill_queue_limit=self.background_skill_queue_limit,
            custom_system_prompt=system_prompt,
            custom_tools=child_tools,
            is_sub_agent=True,
            confirm_fn=self.confirm_fn,
            permission_mode=self.permission_mode,
            max_cost_usd=(max(0.0, self.max_cost_usd - self._get_current_cost_usd())
                          if self.max_cost_usd is not None else None),
            max_turns=(max(0, self.max_turns - self.current_turns)
                       if self.max_turns is not None else None),
            enable_folding=self.enable_folding,
            max_fold_limit=self.max_fold_limit,
            fold_memory_mode=self.fold_memory_mode,
            auto_compact_threshold=self.auto_compact_threshold,
            enable_memory_prefetch=self.enable_memory_prefetch,
            enable_mcp=self.enable_mcp,
            enable_online_evolution=self.enable_online_evolution,
            workspace=self.workspace,
        )
        inherited_mcp_tools = {tool["name"] for tool in child_tools if tool["name"].startswith("mcp__")}
        if inherited_mcp_tools:
            child._mcp_manager = BorrowedMcpManager(self._mcp_manager, inherited_mcp_tools)
            child._mcp_initialized = True
        return child

    async def _run_child_agent(self, *, agent_type: str, description: str,
                               system_prompt: str, tools: list[ToolDef], prompt: str,
                               label: str, empty_result: str) -> str:
        budget = self._check_budget()
        if budget["exceeded"]:
            return f"{label} skipped: {budget['reason']}"
        print_sub_agent_start(agent_type, description)
        child = None
        try:
            child = self._create_child_agent(system_prompt=system_prompt, tools=tools)
            self._register_child(child)
            result = await child.run_once(prompt)
            return result["text"] or empty_result
        except Exception as exc:
            return f"{label} error: {exc}"
        finally:
            try:
                if child is not None:
                    try:
                        await child.aclose()
                    finally:
                        self._account_child_usage(child)
            finally:
                print_sub_agent_end(agent_type, description)

    def _register_child(self, child: "Agent") -> None:
        child._budget_parent = self
        self._active_children.append(child)

    def _account_child_usage(self, child: "Agent") -> None:
        # Each child is newly created; settle even if it failed or was cancelled.
        self._active_children = [active for active in self._active_children if active is not child]
        child._budget_parent = None
        self.total_input_tokens += child.total_input_tokens
        self.total_output_tokens += child.total_output_tokens
        self.side_input_tokens += child.side_input_tokens
        self.side_output_tokens += child.side_output_tokens
        self.current_turns += child.current_turns

#--------------Anthropic 后端---------------
    async def _chat_anthropic(self, user_message: str) -> None:
        self._anthropic_messages = self._normalize_anthropic_messages(_sanitize_for_utf8(self._anthropic_messages))
        budget = self._check_budget()
        if budget["exceeded"]:
            self.last_finish_reason = "budget_exceeded"
            self._emit_text(f"Budget exceeded: {budget['reason']}")
            return
        user_message = _safe_utf8_text(await self._recall_before_response(user_message))
        # 先把本轮用户输入放入 Anthropic 消息历史，后续每轮模型调用都会带上这段上下文。
        self._anthropic_messages.append({"role": "user", "content": user_message})

        while True:
            # 外部请求中止时，结束整个 agent loop。
            if self._aborted:
                break

            # 每轮调用模型前尝试压缩上下文，避免消息历史过长。
            await self._prepare_model_context()

            if not self.is_sub_agent:
                start_spinner()


            # 等完整响应成功后才执行工具。流式请求可能重试，提前执行会让同一工具产生重复副作用。
            budget = self._check_budget()
            if budget["exceeded"]:
                self.last_finish_reason = "budget_exceeded"
                if not self.is_sub_agent:
                    stop_spinner()
                self._emit_text(f"Budget exceeded: {budget['reason']}")
                break
            response = await self._call_anthropic_stream()
            self.last_finish_reason = response.stop_reason
            if not self.is_sub_agent:
                stop_spinner()

            # 记录本次模型调用的耗时点和 token 消耗，用于成本展示与预算控制。
            self.last_api_call_time = time.time()
            self.total_input_tokens += response.usage.input_tokens
            self.total_output_tokens += response.usage.output_tokens
            self.last_input_token_count = response.usage.input_tokens

            # Anthropic 的响应内容里可能混有 text block 和 tool_use block，这里只挑出工具调用。
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            # 把模型返回的所有 content block 写入消息历史，后续 tool_result 要与这些 tool_use 对应。
            self._anthropic_messages.append({
                "role": "assistant",
                "content": [self._block_to_dict(b) for b in response.content],
            })

            # 没有工具调用，说明模型已经给出最终回复，本轮对话结束。
            if not tool_uses:
                if not self.is_sub_agent:
                    print_cost(self.total_input_tokens + self.side_input_tokens, self.total_output_tokens + self.side_output_tokens,
                               self.input_price_per_million, self.output_price_per_million)
                break

            if self.last_finish_reason not in {"tool_use", "end_turn"}:
                self._anthropic_messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tu.id,
                     "content": "Tool execution skipped: incomplete model response."} for tu in tool_uses]})
                break

            # Reserve one tool round only after verifying that a round is available.
            budget = self._check_budget()
            if budget["exceeded"]:
                print_info(f"Budget exceeded: {budget['reason']}")
                self._anthropic_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": f"Tool execution skipped: {budget['reason']}",
                        }
                        for tu in tool_uses
                    ],
                })
                break
            self.current_turns += 1
            self._begin_tool_round((tu.id, tu.name) for tu in tool_uses)


            # 收集本轮所有工具结果，之后作为 tool_result 消息回传给模型。
            tool_results: list[dict] = []
            context_break = False

            for tu in tool_uses:
                # context_break 表示某个工具执行期间清理了上下文，需要停止继续处理本轮剩余工具。
                if context_break or self._aborted:
                    break

                # 将工具入参转为普通 dict，便于权限检查、打印和实际执行。
                inp = tu.input
                res = await self._run_tracked_tool(tu.id, tu.name, inp)
                print_tool_result(tu.name, res)
                self._record_tool_outcome(tu.name, not self._tracked_tool_failed(tu.id, tu.name, res))

                if self._context_cleared:
                    # 工具执行过程中如果清理了上下文，就把结果作为新的用户消息写入，
                    # 并停止继续处理本轮剩余工具，避免旧上下文和新上下文混在一起。
                    self._context_cleared = False
                    self._anthropic_messages.append({"role": "user", "content": res})
                    context_break = True
                    break

                # Anthropic 要求 tool_result 使用 tool_use_id 对应到前面的 tool_use block。
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res})

            if not context_break and tool_results:
                # Anthropic 要求 assistant/tool_use 后面紧跟一条 user/tool_result 消息，
                # 且这条消息必须包含本轮所有 tool_use 的对应结果。
                self._anthropic_messages.append({"role": "user", "content": tool_results})

            self._context_cleared = False

            # 工具结果可能很长，每轮工具执行后检查是否需要压缩上下文。
            self._refresh_runtime_system_prompt()

    @staticmethod
    def _block_to_dict(block) -> dict:
        if block.type == "text":
            return {"type": "text", "text": _safe_utf8_text(block.text)}
        if block.type == "tool_use":
            raw_input = dict(block.input) if hasattr(block.input, 'items') else block.input
            return {"type": "tool_use", "id": _safe_utf8_text(block.id), "name": _safe_utf8_text(block.name), "input": _sanitize_for_utf8(raw_input)}
        # Fallback
        return {"type": _safe_utf8_text(block.type)}

    async def _call_anthropic_stream(self, on_tool_block_complete=None):

        async def _do():
            self._require_model_budget()
            max_output = self.max_output_tokens or _get_max_output_tokens(self.model)

            create_params: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_output if self._thinking_mode != "disabled" else (self.max_output_tokens or 16384),
                "system": _safe_utf8_text(self._system_prompt),
                "tools": _sanitize_for_utf8(get_active_tool_definitions(self.tools, self._activated_tools)),
                "messages": _sanitize_for_utf8(self._anthropic_messages),
            }
            #如果开启了思考模式，就给 Anthropic 请求加上 thinking 参数。
            if self._thinking_mode  in ("adaptive", "enabled"):
                create_params["thinking"]={"type": "enabled", "budget_tokens": max_output - 1}

            first_text = True

            tool_blocks_by_index: dict[int, dict] = {}

            async with self._anthropic_client.messages.stream(**create_params)as stream:
                async for event in stream:
                    if not hasattr(event, 'type'):
                        continue
                    # 当事件是工具调用开始：
                    if event.type == "content_block_start":
                        cb = getattr(event, 'content_block', None)
                        #如果 block 类型是 tool_use，就记录这个工具调用：
                        if cb and getattr(cb, 'type', None) == "tool_use":
                            #因为工具参数 JSON 是流式分片返回的，所以先准备一个空字符串 input_json。
                            tool_blocks_by_index[event.index]= {
                                "id": cb.id, "name": cb.name, "input_json": "",
                            }
                    #当事件是内容增量，分三种情况。
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        # 第一种，普通文本：模型输出正文时，
                        # 调用 _emit_text()。如果是普通交互，就打印；
                        # 如果是 run_once()，就写入 _output_buffer。
                        if hasattr(delta, "text"):
                            if first_text:
                                stop_spinner()
                                self._emit_text("\n")
                                first_text = False
                            self._emit_text(delta.text)
                        # Reasoning has its own channel and is not answer text.
                        elif hasattr(delta, 'thinking'):
                            self._emit_thinking(_safe_utf8_text(delta.thinking))
                        #第三种，工具参数 JSON 片段：工具调用的参数不是一次性返回，
                        # 而是一段一段返回，所以这里不断拼接到 input_json。
                        elif hasattr(delta, 'partial_json'):
                            tb = tool_blocks_by_index.get(event.index)
                            if tb:
                                tb["input_json"] += _safe_utf8_text(delta.partial_json)
                    #当一个 content block 结束：
                    #如果结束的是之前记录的工具调用，就把拼好的 JSON 解析出来：
                    elif event.type == "content_block_stop":
                        tb = tool_blocks_by_index.pop(event.index, None)
                        if tb and on_tool_block_complete:
                            import json as _json
                            try:
                                parsed = _json.loads(tb["input_json"] or "{}")
                            except Exception:
                                parsed = {}
                            #然后调用回调：
                            #这个回调的作用通常是：工具调用一完整，
                            # 就可以提前开始执行工具，不必等整条 assistant 消息全部结束。
                            on_tool_block_complete({
                                "type": "tool_use", "id": _safe_utf8_text(tb["id"]),
                                "name": _safe_utf8_text(tb["name"]), "input": _sanitize_for_utf8(parsed),
                            })
                final_message = await stream.get_final_message()

            #过滤思考的message（因为 thinking 内容一般不应该进入历史消息，否则后续上下文会变大，也可能不符合 API 消息格式要求。）
            final_message.content = [b for b in final_message.content if b.type != "thinking"]
            return final_message
#调用 _do()，如果遇到可重试错误，就由 _with_retry() 负责重试。
        return await self._with_stream_retry(_do, retry=_with_retry)

    #openAI后端

    async def _chat_openai(self, user_message:str) -> None:
        budget = self._check_budget()
        if budget["exceeded"]:
            self.last_finish_reason = "budget_exceeded"
            self._emit_text(f"Budget exceeded: {budget['reason']}")
            return
        user_message = _safe_utf8_text(await self._recall_before_response(user_message))
        self._openai_messages.append({"role": "user", "content": user_message})

        while True:
            if self._aborted:
                break

            await self._prepare_model_context()

            if not self.is_sub_agent:
                start_spinner()

            budget = self._check_budget()
            if budget["exceeded"]:
                self.last_finish_reason = "budget_exceeded"
                if not self.is_sub_agent:
                    stop_spinner()
                self._emit_text(f"Budget exceeded: {budget['reason']}")
                break
            response = await self._call_openai_stream()

            if not self.is_sub_agent:
                stop_spinner()

            self.last_api_call_time = time.time()

            if response.get("usage"):
                self.total_input_tokens += response["usage"]["prompt_tokens"]
                self.total_output_tokens += response["usage"]["completion_tokens"]
                self.last_input_token_count = response["usage"]["prompt_tokens"]

            choice = response.get("choices", [{}])[0] if response.get("choices") else {}
            self.last_finish_reason = choice.get("finish_reason")
            message = choice.get("message", {})

            self._openai_messages.append(message)

            tool_calls = message.get("tool_calls")

            if not tool_calls:
                if not self.is_sub_agent:
                    print_cost(self.total_input_tokens + self.side_input_tokens, self.total_output_tokens + self.side_output_tokens,
                               self.input_price_per_million, self.output_price_per_million)
                break

            if self.last_finish_reason not in {"tool_calls", "stop"}:
                self._append_openai_skipped_tool_results(tool_calls, "Tool execution skipped: incomplete model response.")
                break
            budget = self._check_budget()
            if budget["exceeded"]:
                print_info(f"Budget exceeded: {budget['reason']}")
                self._append_openai_skipped_tool_results(tool_calls, f"Tool execution skipped: {budget['reason']}")
                break
            self.current_turns += 1

            oai_checked: list[dict] = []
            for tc in tool_calls:
                if tc.get("type") != "function":
                    continue
                fn_name = tc["function"]["name"]
                try:
                    inp = json.loads(tc["function"]["arguments"])
                except Exception:
                    inp = None
                oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp})

            self._begin_tool_round((ct["tc"]["id"], ct["fn"]) for ct in oai_checked)
            oai_batches: list[dict] = []
            for ct in oai_checked:
                safe = ct["fn"] in CONCURRENCY_SAFE_TOOLS
                if safe and oai_batches and oai_batches[-1]["concurrent"]:
                    oai_batches[-1]["items"].append(ct)
                else:
                    oai_batches.append({"concurrent": safe, "items": [ct]})

            oai_context_break = False
            try:
                for batch in oai_batches:
                    if oai_context_break or self._aborted:
                        break

                    if batch["concurrent"]:
                        async def _run_oai_safe(ct_item: dict) -> tuple[dict, str]:
                            res = await self._run_tracked_tool(ct_item["tc"]["id"], ct_item["fn"], ct_item["inp"])
                            print_tool_result(ct_item["fn"], res)
                            return ct_item, res

                        results = await asyncio.gather(*[_run_oai_safe(ct) for ct in batch["items"]])
                        for ct_item, res in results:
                            self._record_tool_outcome(
                                ct_item["fn"],
                                not self._tracked_tool_failed(ct_item["tc"]["id"], ct_item["fn"], res),
                            )
                            self._openai_messages.append(
                                {"role": "tool", "tool_call_id": ct_item["tc"]["id"], "content": res})
                    else:
                        for ct in batch["items"]:
                            if self._aborted:
                                break
                            res = await self._run_tracked_tool(ct["tc"]["id"], ct["fn"], ct["inp"])
                            print_tool_result(ct["fn"], res)
                            self._record_tool_outcome(
                                ct["fn"],
                                not self._tracked_tool_failed(ct["tc"]["id"], ct["fn"], res),
                            )

                            if self._context_cleared:
                                self._context_cleared = False
                                self._openai_messages.append({"role": "user", "content": res})
                                oai_context_break = True
                                break

                            self._openai_messages.append(
                                {"role": "tool", "tool_call_id": ct["tc"]["id"], "content": res})
            except asyncio.CancelledError:
                self._recover_tool_checkpoints()
                raise

            if self._aborted:
                self._recover_tool_checkpoints()
            elif oai_context_break:
                self._append_openai_skipped_tool_results(
                    tool_calls,
                    "Tool execution skipped because the conversation context was reset.",
                )

            self._context_cleared = False
            self._refresh_runtime_system_prompt()

    def _append_openai_skipped_tool_results(self, tool_calls: list[dict], reason: str) -> None:
        """Complete calls that still belong to the current conversation."""
        live_ids = {
            call.get("id")
            for msg in self._openai_messages if msg.get("role") == "assistant"
            for call in msg.get("tool_calls") or []
        }
        existing_ids = {
            msg.get("tool_call_id")
            for msg in self._openai_messages
            if msg.get("role") == "tool"
        }
        for tc in tool_calls:
            if tc.get("type") != "function":
                continue
            tool_call_id = tc.get("id")
            if tool_call_id and tool_call_id in live_ids and tool_call_id not in existing_ids:
                self._openai_messages.append(
                    {"role": "tool", "tool_call_id": tool_call_id, "content": reason}
                )
                existing_ids.add(tool_call_id)

    async def _call_openai_stream(self) -> dict:
        async def _do():
            self._require_model_budget()
            create_params = dict(
                model=self.model,
                tools=_sanitize_for_utf8(_to_openai_tools(get_active_tool_definitions(self.tools, self._activated_tools))),
                messages=_sanitize_for_utf8(self._openai_messages),
                stream=True,
                stream_options={"include_usage": True},
            )
            if self.max_output_tokens is not None:
                create_params["max_tokens"] = self.max_output_tokens
            stream = await self._openai_client.chat.completions.create(**create_params)

            content = ""
            first_text = True
            tool_calls: dict[int, dict] = {}
            finish_reason = ""
            usage = None

            try:
                async for chunk in stream:
                    if chunk.usage:
                        usage = {
                            "prompt_tokens": chunk.usage.prompt_tokens,
                            "completion_tokens": chunk.usage.completion_tokens,
                        }

                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    reasoning = (getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)) if delta else None
                    if isinstance(reasoning, str) and reasoning:
                        self._emit_thinking(_safe_utf8_text(reasoning))

                    if delta and delta.content:
                        if first_text:
                            stop_spinner()
                            self._emit_text("\n")
                            first_text = False
                        self._emit_text(delta.content)
                        content += _safe_utf8_text(delta.content)

                    if delta and delta.tool_calls:
                        for tc in delta.tool_calls:
                            existing = tool_calls.get(tc.index)
                            if existing:
                                if tc.function and tc.function.arguments:
                                    existing["arguments"] += _safe_utf8_text(tc.function.arguments)
                            else:
                                tool_calls[tc.index] = {
                                    "id": _safe_utf8_text(tc.id or ""),
                                    "name": _safe_utf8_text((tc.function.name if tc.function else "") or ""),
                                    "arguments": _safe_utf8_text((tc.function.arguments if tc.function else "") or ""),
                                }

                    if chunk.choices[0].finish_reason:
                        finish_reason = chunk.choices[0].finish_reason

            finally:
                await stream.close()

            assembled = None
            if tool_calls:
                assembled = [
                    {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for _, tc in sorted(tool_calls.items())
                ]

            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": assembled,
                    },
                    "finish_reason": finish_reason or "incomplete",
                }],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
            }

        return await self._with_stream_retry(_do, retry=_with_retry)

    async def _confirm_tool_call(self, tool_name: str, permission: dict) -> bool:
        message = permission.get("message", "")
        # Repeated shell and file changes need fresh approval, even with identical arguments.
        always_confirm = tool_name in {"run_shell", "write_file", "edit_file", "skill_create", "skill_evolve",
                                       "skill_prune", "skill_restore", "mcp_start"} or tool_name.startswith("mcp__")
        if not always_confirm and message in self._confirmed_paths:
            return True
        if not await self._confirm_dangerous(message):
            return False
        if not always_confirm:
            self._confirmed_paths.add(message)
        return True

    async def _confirm_dangerous(self, command: str) -> bool:
        print_confirmation(command)
        if self.confirm_fn:
            return await self.confirm_fn(command)
        # Without a confirmation channel (CI, headless runs, background jobs) a
        # blocking input() would wait forever. An unanswered request is a denial.
        if not sys.stdin or not sys.stdin.isatty():
            return False
        # Fallback: blocking input
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False
