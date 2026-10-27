"""Shared tool validation and permission checks for both model protocols."""
from __future__ import annotations

import asyncio
from pathlib import Path

from .audit import AuditLog, CallRecord, audit_path
from .tools import build_edit_proposal, check_permission
from .ui import print_tool_call
from .workspace import workspace_scope


class ToolExecution:
    def _audit_record(self, name: str, inp) -> CallRecord:
        """One audit pair per call, on the workspace's log."""

        log = getattr(self, "_audit_log", None)
        if log is None:
            workspace = getattr(self, "workspace", None)
            root = getattr(workspace, "root", None) or Path.cwd()
            log = AuditLog(audit_path(root))
            self._audit_log = log
        count = getattr(self, "_audit_calls", 0) + 1
        self._audit_calls = count
        return CallRecord(
            log,
            session_id=str(getattr(self, "session_id", "")),
            tool=name,
            arguments=inp,
            tool_call_number=count,
        )

    async def _run_checked_tool(self, name: str, inp) -> str:
        """Validate and authorize at execution time for both model protocols."""
        record = self._audit_record(name, inp)
        record.start()
        try:
            return await self._authorize_and_execute(name, inp, record)
        except asyncio.CancelledError:
            record.fail("cancelled")
            raise
        finally:
            # The returned text is unchanged by auditing; only the record moves.
            record.finish()

    async def _authorize_and_execute(self, name: str, inp, record: CallRecord) -> str:
        from .validation import validate_json
        definition = next((tool for tool in self.tools if tool["name"] == name), None)
        if definition is None:
            record.fail("unavailable")
            return f"Tool not available in this run: {name}"
        error = validate_json(inp, definition["input_schema"])
        if error:
            record.fail("invalid", str(error))
            return f"Error: invalid arguments for {name}: {error}"

        # Bind the session workspace for the whole call so path resolution and
        # the access policy agree on the same root, even for concurrent tools.
        with workspace_scope(getattr(self, "workspace", None)):
            print_tool_call(name, inp)
            permission = check_permission(name, inp, self.permission_mode, self._plan_file_path)
            record.permission = str(permission["action"])
            if permission["action"] == "deny":
                record.fail("denied", str(permission.get("message", "")))
                return f"Action denied: {permission.get('message', '')}"

            # Prepare the change before asking, so the approval covers the exact
            # patch and the exact file version it was built from.
            proposal = None
            if permission["action"] == "confirm" and name in {"write_file", "edit_file"}:
                try:
                    proposal = build_edit_proposal(name, inp)
                except ValueError as exc:
                    record.fail("invalid", str(exc))
                    return f"Error: {exc}"
                permission = {**permission, "message": proposal.render()}

            if permission["action"] == "confirm" and not await self._confirm_tool_call(name, permission):
                record.fail("denied", "user denied")
                return "User denied this action."
            permission = check_permission(name, inp, self.permission_mode, self._plan_file_path)
            record.permission = str(permission["action"])
            if permission["action"] == "deny":
                record.fail("denied", str(permission.get("message", "")))
                return f"Action denied: {permission.get('message', '')}"
            if proposal is not None and not proposal.is_current():
                # The approval was for another version of the file; regenerate
                # rather than applying a patch the user never saw.
                record.fail("stale", "file changed while awaiting approval")
                return (
                    "The file changed while the edit was waiting for approval, so nothing "
                    "was written. Read it again and repeat the change."
                )
            try:
                result = await self._execute_tool_call(name, inp)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                record.fail("error", f"{type(exc).__name__}: {exc}")
                return f"Error executing tool: {exc}"
            record.outcome = "ok"
            return result
