"""Memory tools: source, scope, archive and the resident index cap (I6 / T17)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents import memory


class MemoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.work = root / "work"
        self.home.mkdir()
        self.work.mkdir()

        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

        previous = Path.cwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, previous)


class SaveTests(MemoryTestCase):
    def test_source_and_scope_are_stored_and_read_back(self) -> None:
        filename = memory.save_memory("Prefers tabs", "formatting", "user", "Uses tabs.", source="user", scope="global")
        entry = memory.list_memories()[0]

        self.assertEqual(entry.filename, filename)
        self.assertEqual(entry.source, "user")
        self.assertEqual(entry.scope, "global")
        self.assertEqual(entry.content.strip(), "Uses tabs.")

    def test_legacy_memories_default_to_user_and_project(self) -> None:
        directory = memory.get_memory_dir()
        (directory / "user_old.md").write_text(
            "---\nname: Old\ndescription: legacy\ntype: user\n---\nbody\n", encoding="utf-8"
        )
        entry = memory.list_memories()[0]
        self.assertEqual((entry.source, entry.scope), ("user", "project"))

    def test_invalid_source_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            memory.save_memory("x", "d", "user", "c", source="guessed")

    def test_invalid_scope_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            memory.save_memory("x", "d", "user", "c", scope="everywhere")


class IndexTests(MemoryTestCase):
    def test_index_makes_provenance_visible(self) -> None:
        memory.save_memory("Deploy day", "release", "project", "Fridays.", source="inference")
        index = memory.load_memory_index()

        self.assertIn("project/inference", index)
        self.assertIn("Deploy day", index)

    def test_index_is_capped(self) -> None:
        for number in range(memory.MAX_INDEX_ENTRIES + 5):
            memory.save_memory(f"note {number}", "bulk", "project", "x", source="user")
        index = memory.load_memory_index()

        listed = [line for line in index.splitlines() if line.startswith("- **[")]
        self.assertEqual(len(listed), memory.MAX_INDEX_ENTRIES)
        self.assertIn("more not listed", index)

    def test_archived_memories_leave_the_index(self) -> None:
        filename = memory.save_memory("Temporary", "d", "project", "x", source="user")
        memory.archive_memory(filename)

        self.assertNotIn("Temporary", memory.load_memory_index())


class ArchiveTests(MemoryTestCase):
    def test_archiving_keeps_the_file_and_hides_it(self) -> None:
        filename = memory.save_memory("Old fact", "d", "project", "x", source="user")
        self.assertTrue(memory.archive_memory(filename))

        self.assertEqual(memory.list_memories(), [])
        self.assertTrue((memory.archive_dir() / filename).exists())
        archived = memory.list_memories(include_archived=True)
        self.assertEqual(len(archived), 1)
        self.assertTrue(archived[0].archived)

    def test_restoring_brings_it_back(self) -> None:
        filename = memory.save_memory("Old fact", "d", "project", "x", source="user")
        memory.archive_memory(filename)
        self.assertTrue(memory.restore_memory(filename))

        self.assertEqual(len(memory.list_memories()), 1)
        self.assertFalse((memory.archive_dir() / filename).exists())

    def test_archiving_twice_is_an_error(self) -> None:
        filename = memory.save_memory("Old fact", "d", "project", "x", source="user")
        memory.archive_memory(filename)
        with self.assertRaises(ValueError):
            memory.archive_memory(filename)

    def test_unknown_filename_reports_false(self) -> None:
        self.assertFalse(memory.archive_memory("nope.md"))
        self.assertFalse(memory.restore_memory("nope.md"))

    def test_path_traversal_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            memory.archive_memory("../MEMORY.md")


class SearchTests(MemoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        memory.save_memory("Use pytest", "testing", "project", "The team runs pytest -q.", source="repo")
        memory.save_memory("Report format", "docs", "reference", "Markdown tables.", source="user")

    def test_matches_body_text_case_insensitively(self) -> None:
        hits = memory.search_memories("PYTEST")
        self.assertEqual([entry.name for entry in hits], ["Use pytest"])

    def test_matches_description(self) -> None:
        self.assertEqual(len(memory.search_memories("docs")), 1)

    def test_limit_is_respected(self) -> None:
        self.assertEqual(len(memory.search_memories("e", limit=1)), 1)

    def test_archived_entries_are_excluded_by_default(self) -> None:
        filename = memory.save_memory("Pytest legacy", "old", "project", "pytest old", source="user")
        memory.archive_memory(filename)

        self.assertEqual(len(memory.search_memories("legacy")), 0)
        self.assertEqual(len(memory.search_memories("legacy", include_archived=True)), 1)

    def test_empty_query_returns_nothing(self) -> None:
        self.assertEqual(memory.search_memories("   "), [])


class ToolSurfaceTests(MemoryTestCase):
    def call(self, name: str, payload: dict) -> str:
        from agents.tools import execute_tool

        return asyncio.run(execute_tool(name, payload))

    def test_save_tool_reports_the_filename_and_source(self) -> None:
        result = self.call("memory_save", {
            "name": "Deploy freeze", "description": "release", "type": "project",
            "content": "No deploys in December.", "source": "user",
        })
        self.assertIn("Saved memory", result)
        self.assertIn("source=user", result)

    def test_search_tool_shows_type_source_and_scope(self) -> None:
        self.call("memory_save", {
            "name": "Deploy freeze", "description": "release", "type": "project",
            "content": "No deploys in December.", "source": "inference",
        })
        result = self.call("memory_search", {"query": "december"})
        self.assertIn("project/inference, project", result)
        self.assertIn("Deploy freeze", result)

    def test_archive_and_restore_round_trip_through_the_tools(self) -> None:
        self.call("memory_save", {
            "name": "Temp", "description": "d", "type": "project", "content": "x",
        })
        filename = memory.list_memories()[0].filename

        self.assertIn("Archived", self.call("memory_archive", {"filename": filename}))
        self.assertEqual(memory.list_memories(), [])
        self.assertIn("Restored", self.call("memory_restore", {"filename": filename}))
        self.assertEqual(len(memory.list_memories()), 1)

    def test_invalid_source_fails_validation(self) -> None:
        result = self.call("memory_save", {
            "name": "x", "description": "d", "type": "user", "content": "c", "source": "vibes",
        })
        self.assertIn("invalid arguments", result)

    def test_missing_required_field_fails_validation(self) -> None:
        result = self.call("memory_save", {"name": "x", "description": "d", "type": "user"})
        self.assertIn("invalid arguments", result)

    def test_search_without_hits_says_so(self) -> None:
        self.assertEqual(self.call("memory_search", {"query": "nothing-here"}), "No matching memories.")


class MemoryProvenanceTests(MemoryTestCase):
    """Recall must say where a memory came from, and text must stay on one line."""

    def test_recall_carries_the_source_into_the_injected_header(self) -> None:
        filename = memory.save_memory("guess", "d", "project", "the build uses make",
                                      source="inference")

        async def side_query(_prompt: str, _text: str) -> str:
            return '{"selected_memories": ["%s"]}' % filename

        recalled = asyncio.run(memory.select_relevant_memories("how do builds work", side_query, set()))
        self.assertEqual(len(recalled), 1)
        self.assertEqual(recalled[0].source, "inference")
        self.assertIn("source: inference", recalled[0].header)

    def test_a_newline_in_the_description_cannot_forge_an_index_row(self) -> None:
        memory.save_memory("real", "line one\n# INJECTED", "project", "x", source="user")
        index = memory.load_memory_index()
        self.assertNotIn("\n# INJECTED", index)
        self.assertIn("line one # INJECTED", index)

    def test_a_newline_in_the_description_cannot_forge_a_manifest_entry(self) -> None:
        memory.save_memory("real", "line one\n# INJECTED", "project", "x", source="user")
        manifest = memory.format_memory_manifest(memory.scan_memory_headers())
        self.assertNotIn("\n# INJECTED", manifest)

    def test_plan_mode_does_not_write_memory(self) -> None:
        from agents.tools import check_permission

        for tool in ("memory_save", "memory_archive", "memory_restore"):
            with self.subTest(tool=tool):
                verdict = check_permission(tool, {"name": "x", "description": "d",
                                                  "type": "user", "content": "c"}, "plan")
                self.assertEqual(verdict["action"], "deny")

    def test_write_file_into_the_memory_directory_keeps_the_index_format(self) -> None:
        """There is one index implementation; a direct write must not replace it."""

        filename = memory.save_memory("guess", "d", "project", "x", source="inference")
        path = memory.get_memory_dir() / filename
        text = path.read_text(encoding="utf-8").replace("x", "edited", 1)
        path.write_text(text, encoding="utf-8")

        from agents.tools import _refresh_memory_index

        _refresh_memory_index(str(path))

        index = memory.load_memory_index()
        self.assertIn("project/inference", index)
        self.assertIn(", project)", index)
