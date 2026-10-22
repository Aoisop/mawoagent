"""Shared tool validation and permission checks for both model protocols."""
from __future__ import annotations

import asyncio
from .tools import build_edit_proposal, check_permission
from .ui import print_tool_call
from .workspace import workspace_scope


class ToolExecution:
    async def _run_checked_tool(self, name: str, inp) -> str:
        """Validate and authorize at execution time for both model protocols."""
        from .validation import validate_json
        definition = next((tool for tool in self.tools if tool["name"] == name), None)
        if definition is None:
            return f"Tool not available in this run: {name}"
        error = validate_json(inp, definition["input_schema"])
        if error:
            return f"Error: invalid arguments for {name}: {error}"

        # Bind the session workspace for the whole call so path resolution and
        # the access policy agree on the same root, even for concurrent tools.
        with workspace_scope(getattr(self, "workspace", None)):
            print_tool_call(name, inp)
            permission = check_permission(name, inp, self.permission_mode, self._plan_file_path)
            if permission["action"] == "deny":
                return f"Action denied: {permission.get('message', '')}"

            # Prepare the change before asking, so the approval covers the exact
            # patch and the exact file version it was built from.
            proposal = None
            if permission["action"] == "confirm" and name in {"write_file", "edit_file"}:
                try:
                    proposal = build_edit_proposal(name, inp)
                except ValueError as exc:
                    return f"Error: {exc}"
                permission = {**permission, "message": proposal.render()}

            if permission["action"] == "confirm" and not await self._confirm_tool_call(name, permission):
                return "User denied this action."
            permission = check_permission(name, inp, self.permission_mode, self._plan_file_path)
            if permission["action"] == "deny":
                return f"Action denied: {permission.get('message', '')}"
            if proposal is not None and not proposal.is_current():
                # The approval was for another version of the file; regenerate
                # rather than applying a patch the user never saw.
                return (
                    "The file changed while the edit was waiting for approval, so nothing "
                    "was written. Read it again and repeat the change."
                )
            try:
                return await self._execute_tool_call(name, inp)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return f"Error executing tool: {exc}"
