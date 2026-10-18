"""Shared context preflight and bounded memory recall for both protocols."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from .memory import (MAX_SESSION_MEMORY_BYTES, start_memory_prefetch,
                     format_memories_for_injection)
from .tool_results import compact_tool_results


class ContextRuntime:
    def _run_compression_pipeline(self) -> None:
        messages = self._openai_messages if self.use_openai else self._anthropic_messages
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        idle_seconds = time.time() - self.last_api_call_time if self.last_api_call_time else 0
        compact_tool_results(messages, use_openai=self.use_openai,
                             utilization=utilization, idle_seconds=idle_seconds)

    async def _recall_before_response(self, user_message: str) -> str:
        if self.is_sub_agent or not self.enable_memory_prefetch:
            return user_message
        query = self._build_side_query()
        if query is None:
            return user_message
        prefetch = start_memory_prefetch(user_message, query,
                                        self._already_surfaced_memories, self._session_memory_bytes)
        if prefetch is None:
            return user_message
        self._track_memory_prefetch(prefetch)
        try:
            memories = await asyncio.wait_for(prefetch.task, timeout=self.memory_recall_timeout)
        except asyncio.TimeoutError:
            logging.info("Memory recall timed out; continuing without recalled memory")
            return user_message
        except Exception:
            logging.exception("Memory recall failed; continuing without recalled memory")
            return user_message
        selected = []
        for memory in memories:
            if (memory.path not in self._already_surfaced_memories
                    and self._session_memory_bytes + memory.size <= MAX_SESSION_MEMORY_BYTES):
                selected.append(memory)
                self._already_surfaced_memories.add(memory.path)
                self._session_memory_bytes += memory.size
        if not selected:
            return user_message
        return user_message + "\n\n" + format_memories_for_injection(selected)

    def _context_request_bytes(self) -> int:
        messages = self._openai_messages if self.use_openai else self._anthropic_messages
        # Include tools/system and non-ASCII text; this is a conservative heuristic,
        # not an exact tokenizer or a guarantee for every model's context limit.
        return len(json.dumps([self._system_prompt, self.tools, messages],
                              ensure_ascii=False, default=str).encode("utf-8", errors="replace"))

    def _estimated_input_tokens(self) -> int:
        size = self._context_request_bytes()
        anchor = self._last_request_bytes
        growth = max(0, size - anchor) if anchor is not None else 0
        return max((size + 2) // 3, self.last_input_token_count + (growth + 2) // 3)

    async def _prepare_model_context(self) -> None:
        self._run_compression_pipeline()
        await self._check_and_compact()
        self._last_request_bytes = self._context_request_bytes()
