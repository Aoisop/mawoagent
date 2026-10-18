from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from agents import tools


class ToolDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_deferred_activation_strips_metadata_without_mutating_schemas(self):
        definitions = [
            {"name": "ready", "description": "Ready tool", "input_schema": {"type": "object"}},
            {"name": "later", "description": "Deferred tool", "input_schema": {"type": "object"}, "deferred": True},
        ]
        original = deepcopy(definitions)
        with patch.object(tools, "tool_definitions", definitions), patch.object(tools, "_activated_tools", set()):
            self.assertEqual([tool["name"] for tool in tools.get_active_tool_definitions()], ["ready"])
            self.assertEqual(tools.get_deferred_tool_names(), ["later"])
            result = await tools.execute_tool("tool_search", {"query": "DEFERRED"})
            self.assertEqual(json.loads(result)[0]["name"], "later")
            active = tools.get_active_tool_definitions()
            self.assertEqual([tool["name"] for tool in active], ["ready", "later"])
            self.assertTrue(all("deferred" not in tool for tool in active))
            self.assertEqual(tools.get_deferred_tool_names(), [])
            self.assertEqual(definitions, original)
            tools.reset_activated_tools()
            self.assertEqual(tools.get_deferred_tool_names(), ["later"])

    async def test_reads_use_workers_and_mutations_remain_on_event_loop_thread(self):
        event_loop_thread = threading.get_ident()
        threads = {}

        def read(_):
            threads["read"] = threading.get_ident()
            return "read result"

        def write(_):
            threads["write"] = threading.get_ident()
            return "write result"

        with patch.object(tools, "_read_file", side_effect=read), patch.object(tools, "_write_file", side_effect=write):
            self.assertEqual(await tools.execute_tool("read_file", {"file_path": "unused"}), "read result")
            self.assertEqual(await tools.execute_tool("write_file", {"file_path": "unused", "content": "text"}), "write result")
        self.assertNotEqual(threads["read"], event_loop_thread)
        self.assertEqual(threads["write"], event_loop_thread)

    async def test_unread_or_externally_modified_file_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file.txt"
            path.write_text("original")
            state = {}
            write_input = {"file_path": str(path), "content": "replacement"}
            unread = await tools.execute_tool("write_file", write_input, state)
            self.assertIn("must read this file", unread)
            await tools.execute_tool("read_file", {"file_path": str(path)}, state)
            recorded = state[str(path.resolve())]
            path.write_text("external change")
            os.utime(path, ns=(recorded.modified_ns + 5_000_000_000, recorded.modified_ns + 5_000_000_000))
            stale = await tools.execute_tool("write_file", write_input, state)
            self.assertIn("modified externally", stale)
            self.assertEqual(path.read_text(), "external change")
            self.assertEqual(state[str(path.resolve())], recorded)

    async def test_unreadable_file_metadata_returns_tool_error_before_mutation(self):
        with patch.object(tools, "_file_version", side_effect=PermissionError("access denied")), \
                patch.object(tools, "_write_file") as write:
            result = await tools.execute_tool("write_file", {"file_path": "example", "content": "text"}, {})
        self.assertTrue(result.startswith("Error checking file"), result)
        write.assert_not_called()


class PythonSearchTests(unittest.TestCase):
    def test_searching_a_single_file_respects_include_pattern(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "example.py"
            path.write_text("first\nneedle\nlast")
            result = asyncio.run(tools.execute_tool("grep_search", {"path": str(path), "pattern": "needle"}))
            self.assertEqual(result, f"{path}:2:needle")
            self.assertEqual(tools._grep_python("needle", str(path), "*.txt"), "No matches found.")

    def test_invalid_regex_is_returned_as_a_tool_error(self):
        result = asyncio.run(tools.execute_tool("grep_search", {"pattern": "["}))
        self.assertTrue(result.startswith("Error searching files: invalid regular expression"), result)

    @unittest.skipIf(os.name == "nt", "Directory symlinks require additional Windows privileges")
    def test_directory_symlink_cycles_are_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "file.txt").write_text("needle")
            (root / "loop").symlink_to(root, target_is_directory=True)
            (root / ".hidden").mkdir()
            (root / ".hidden" / "skip.txt").write_text("needle")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "skip.txt").write_text("needle")
            self.assertEqual(tools._grep_python("needle", tmp, None), f"{root / 'file.txt'}:1:needle")

    def test_match_limit_is_global_across_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("first", "second"):
                folder = root / name
                folder.mkdir()
                (folder / "hits.txt").write_text("\n".join(["needle"] * 150))
            result = tools._grep_python("needle", tmp, None)
            self.assertEqual(len(result.splitlines()), 101)
            self.assertEqual(result.splitlines()[-1], "... and 100 more matches")


if __name__ == "__main__":
    unittest.main()
