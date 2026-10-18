"""Durable tool boundaries; recovery never replays an uncertain side effect."""
from __future__ import annotations

from contextvars import ContextVar

from .result_storage import persist_tool_result

CURRENT_CALL = ContextVar("checkpoint_call", default=None)


class ToolCheckpoints:
    def _save_tool_checkpoint(self) -> None:
        if not self.is_sub_agent:
            # A failed write must prevent starting an unjournaled operation.
            self._auto_save(strict=True)

    def _begin_tool_round(self, calls) -> None:
        self._tool_checkpoints = {
            call_id: {"name": name, "status": "not_started"}
            for call_id, name in calls
        }
        self._save_tool_checkpoint()

    async def _run_tracked_tool(self, call_id: str, name: str, inp) -> str:
        record = self._tool_checkpoints[call_id]
        record["status"] = "running"
        self._save_tool_checkpoint()
        token = CURRENT_CALL.set(call_id)
        try:
            result = await self._run_checked_tool(name, inp)
        except BaseException:
            record["status"] = "unknown"
            # The persisted running record already means unknown after a crash.
            raise
        finally:
            CURRENT_CALL.reset(token)
        failed = self._looks_like_tool_failure(name, result, "")
        stored = persist_tool_result(name, result)
        record.update(status="completed", failed=failed, **stored)
        self._save_tool_checkpoint()
        return stored["result"]

    def _tracked_tool_failed(self, call_id: str, name: str, result: str) -> bool:
        record = self._tool_checkpoints.get(call_id, {})
        if isinstance(record.get("failed"), bool):
            return record["failed"]
        return self._looks_like_tool_failure(name, result, result)

    def _recover_tool_checkpoints(self) -> None:
        messages = self._openai_messages if self.use_openai else self._anthropic_messages
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") != "assistant":
                continue
            ids = ([call["id"] for call in message.get("tool_calls") or []]
                   if self.use_openai else list(self._anthropic_tool_use_ids(message)))
            if not ids:
                break
            if self.use_openai:
                present = {item.get("tool_call_id") for item in messages[index + 1:] if item.get("role") == "tool"}
            else:
                if index + 1 >= len(messages):
                    messages.append({"role": "user", "content": []})
                following = messages[index + 1]
                present = self._anthropic_tool_result_ids(following)
                if not isinstance(following.get("content"), list):
                    messages.insert(index + 1, {"role": "user", "content": []})
                following = messages[index + 1]
            for call_id in ids:
                if call_id in present:
                    continue
                record = self._tool_checkpoints.get(call_id, {})
                if record.get("status") == "completed" and isinstance(record.get("result"), str):
                    # Older sessions stored raw output. Migrate only when it is
                    # recovered, without injecting the entire artifact again.
                    record.update(persist_tool_result(record.get("name", "tool"), record["result"], reuse_existing=True))
                    result = record["result"]
                elif record.get("status") == "not_started":
                    result = "Tool execution skipped: interrupted before execution; this tool was not started."
                else:
                    result = "Tool execution status unknown after interruption. Inspect actual state before retrying; do not automatically repeat side effects."
                    record["status"] = "unknown"
                if self.use_openai:
                    messages.insert(index + 1, {"role": "tool", "tool_call_id": call_id, "content": result})
                else:
                    following["content"].append({"type": "tool_result", "tool_use_id": call_id, "content": result})
            break
