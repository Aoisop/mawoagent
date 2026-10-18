from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from agents import tools


class ToolBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.cwd)
        self.rules = {"allow": [], "deny": []}
        mock = patch.object(tools, "load_permission_rules", return_value=self.rules)
        mock.start()
        self.addCleanup(mock.stop)

    def deny(self, name: str, pattern: str | None):
        self.rules["deny"].append({"tool": name, "pattern": pattern})

    async def test_file_rules_match_relative_absolute_and_symlink_aliases(self):
        blocked = self.root / "blocked.txt"
        blocked.write_text("private")
        (self.root / "nested").mkdir()
        aliases = ["blocked.txt", "./blocked.txt", "nested/../blocked.txt", str(blocked)]
        if os.name != "nt":
            (self.root / "alias.txt").symlink_to(blocked)
            aliases.append("alias.txt")
        self.deny("read_file", "blocked.txt")
        for value in aliases:
            with self.subTest(path=value):
                self.assertEqual(tools.check_permission("read_file", {"file_path": value})["action"], "deny")
        self.assertEqual(tools.check_permission("read_file", {"file_path": "other.txt"})["action"], "allow")
        self.assertEqual(tools.check_permission("read_file", {"file_path": "alias.txt"}, "bypassPermissions")["action"], "allow")

    async def test_discovery_path_rules_and_directory_prefix_keep_boundaries(self):
        for name in ("grep_search", "list_files"):
            self.deny(name, "./private/*")
            self.assertEqual(tools.check_permission(name, {"path": str(self.root / "private" / "a.txt")})["action"], "deny")
            self.assertEqual(tools.check_permission(name, {"path": str(self.root / "private-other" / "a.txt")})["action"], "allow")
        self.rules["allow"] = [{"tool": "write_file", "pattern": "allowed/*"}]
        self.assertEqual(tools.check_permission("write_file", {"file_path": str(self.root / "allowed" / "new.txt")})["action"], "allow")
        self.assertEqual(tools.check_permission("write_file", {"file_path": str(self.root / "elsewhere" / "new.txt")})["action"], "confirm")

    @unittest.skipIf(os.name == "nt", "Symlinks require additional Windows privileges")
    async def test_symlink_parent_traversal_is_resolved_before_dotdot_normalization(self):
        outside = self.root / "outside"
        (outside / "child").mkdir(parents=True)
        (outside / "secret.txt").write_text("private")
        (self.root / "alias").symlink_to(outside / "child", target_is_directory=True)
        self.deny("read_file", str(outside / "secret.txt"))
        for name in ("alias/../secret.txt", str(self.root / "alias" / ".." / "secret.txt")):
            self.assertEqual(tools.check_permission("read_file", {"file_path": name})["action"], "deny")
        listing = await tools.execute_tool("list_files", {"path": "alias/..", "pattern": "*.txt"})
        self.assertNotIn("secret", listing)

    @unittest.skipIf(os.name == "nt", "Symlinks require additional Windows privileges")
    async def test_discovery_does_not_open_or_list_denied_files_or_aliases(self):
        blocked = self.root / "blocked.txt"
        blocked.write_text("needle SECRET")
        (self.root / "visible.txt").write_text("needle visible")
        (self.root / "alias.txt").symlink_to(blocked)
        self.deny("read_file", "blocked.txt")
        original = Path.open
        opened = []

        def tracked(path, *args, **kwargs):
            opened.append(path.resolve())
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", tracked):
            listing = await tools.execute_tool("list_files", {"pattern": "**/*.txt"})
            search = await tools.execute_tool("grep_search", {"pattern": "needle"})
        self.assertEqual(listing, "visible.txt")
        self.assertNotIn("SECRET", search)
        self.assertIn("visible", search)
        self.assertNotIn(blocked, opened)
        bypass = await tools.execute_tool("grep_search", {"pattern": "needle"}, permission_mode="bypassPermissions")
        self.assertIn("SECRET", bypass)

    @unittest.skipIf(os.name == "nt", "Symlinks require additional Windows privileges")
    async def test_denied_directory_cannot_be_entered_through_glob_or_symlink(self):
        private = self.root / "private"
        private.mkdir()
        (private / "hidden.txt").write_text("needle SECRET")
        outside = self.root / "outside.txt"
        outside.write_text("needle outside")
        (private / "escaped.txt").symlink_to(outside)
        (self.root / "directory-alias").symlink_to(private, target_is_directory=True)
        self.deny("grep_search", "private")
        self.deny("list_files", "private")
        for base in (".", "directory-alias"):
            listing = await tools.execute_tool("list_files", {"path": base, "pattern": "**/*.txt"})
            search = await tools.execute_tool("grep_search", {"path": base, "pattern": "needle"})
            self.assertNotIn("hidden", listing)
            self.assertNotIn("escaped", listing)
            self.assertNotIn("SECRET", search)
        self.deny("read_file", "private/*")
        self.assertEqual(tools.check_permission("read_file", {"file_path": "private/escaped.txt"})["action"], "deny")

    async def test_modification_during_read_discards_content_and_previous_version(self):
        path = self.root / "file.txt"
        path.write_text("before")
        state = {}
        await tools.execute_tool("read_file", {"file_path": str(path)}, state)
        original = tools._read_page

        def racing_read(*args):
            content = original(*args)
            path.write_text("after")
            return content

        with patch.object(tools, "_read_page", side_effect=racing_read):
            result = await tools.execute_tool("read_file", {"file_path": str(path)}, state)
        self.assertIn("file changed during the read", result)
        self.assertEqual(state, {})
        rejected = await tools.execute_tool("write_file", {"file_path": str(path), "content": "overwrite"}, state)
        self.assertIn("must read this file", rejected)
        self.assertEqual(path.read_text(), "after")

    async def test_post_read_modification_never_gets_recorded_as_observed(self):
        path = self.root / "file.txt"
        path.write_text("before")
        state = {}
        original = tools._read_file

        def racing_read(inp):
            result = original(inp)
            path.write_text("external edit")
            return result

        with patch.object(tools, "_read_file", side_effect=racing_read):
            result = await tools.execute_tool("read_file", {"file_path": str(path)}, state)
        self.assertIn("before", result)
        self.assertNotEqual(state[str(path)], tools._file_version(path))
        rejected = await tools.execute_tool("write_file", {"file_path": str(path), "content": "overwrite"}, state)
        self.assertIn("modified externally", rejected)
        self.assertEqual(path.read_text(), "external edit")

    async def test_replaced_file_with_identical_size_and_mtime_requires_reread(self):
        path = self.root / "file.txt"
        path.write_text("before")
        state = {}
        await tools.execute_tool("read_file", {"file_path": str(path)}, state)
        version = state[str(path)]
        replacement = self.root / "replacement.txt"
        replacement.write_text("change")
        os.utime(replacement, ns=(version.modified_ns, version.modified_ns))
        replacement.replace(path)
        rejected = await tools.execute_tool("edit_file", {"file_path": str(path), "old_string": "change", "new_string": "lost"}, state)
        self.assertIn("modified externally", rejected)
        self.assertEqual(path.read_text(), "change")

    async def test_external_edit_while_staging_prevents_commit_and_cleans_temp(self):
        path = self.root / "file.txt"
        path.write_text("before")
        state = {}
        await tools.execute_tool("read_file", {"file_path": str(path)}, state)
        original = tools._file_version
        calls = 0

        def racing_version(candidate):
            nonlocal calls
            calls += 1
            if calls == 3:  # precheck, staging start, immediately before replace
                path.write_text("external edit")
            return original(candidate)

        with patch.object(tools, "_file_version", side_effect=racing_version):
            result = await tools.execute_tool("write_file", {"file_path": str(path), "content": "overwrite"}, state)
        self.assertIn("modified externally", result)
        self.assertEqual(path.read_text(), "external edit")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["file.txt"])

    @unittest.skipIf(os.name == "nt", "POSIX permissions and symlinks are platform-specific")
    async def test_atomic_write_preserves_target_permissions_symlink_and_fresh_version(self):
        path = self.root / "file.txt"
        path.write_text("before")
        path.chmod(0o640)
        alias = self.root / "alias.txt"
        alias.symlink_to(path)
        state = {}
        await tools.execute_tool("read_file", {"file_path": str(alias)}, state)
        result = await tools.execute_tool("write_file", {"file_path": str(alias), "content": "after"}, state)
        self.assertTrue(result.startswith("Successfully"), result)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertEqual(state[str(path)], tools._file_version(path))
        result = await tools.execute_tool("edit_file", {"file_path": str(path), "old_string": "after", "new_string": "final"}, state)
        self.assertTrue(result.startswith("Successfully"), result)
        self.assertEqual(path.read_text(), "final")
        new_path = self.root / "new.txt"
        await tools.execute_tool("write_file", {"file_path": str(new_path), "content": "new"}, state)
        self.assertEqual(state[str(new_path)], tools._file_version(new_path))

    async def test_new_file_write_records_its_version_and_never_clobbers_a_racing_create(self):
        path = self.root / "new.txt"
        state = {}
        result = await tools.execute_tool("write_file", {"file_path": str(path), "content": "created"}, state)
        self.assertTrue(result.startswith("Successfully"), result)
        self.assertEqual(state[str(path)], tools._file_version(path))
        result = await tools.execute_tool("edit_file", {"file_path": str(path), "old_string": "created", "new_string": "edited"}, state)
        self.assertTrue(result.startswith("Successfully"), result)
        racing_path = self.root / "racing.txt"
        original = os.link

        def racing_link(source, destination):
            racing_path.write_text("external create")
            return original(source, destination)

        with patch.object(tools.os, "link", side_effect=racing_link):
            result = await tools.execute_tool("write_file", {"file_path": str(racing_path), "content": "overwrite"}, state)
        self.assertTrue(result.startswith("Error"), result)
        self.assertEqual(racing_path.read_text(), "external create")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["new.txt", "racing.txt"])

    async def test_small_reads_keep_numbering_newlines_and_empty_file_behavior(self):
        path = self.root / "file.txt"
        for content in ("", "one", "one\n", "one\n\ntwo\n", "one\r\ntwo\r\n"):
            path.write_bytes(content.encode())
            result = await tools.execute_tool("read_file", {"file_path": str(path)})
            normalized = content.replace("\r\n", "\n")
            expected = "\n".join(f"{number:4d} | {line}" for number, line in enumerate(normalized.split("\n"), 1))
            self.assertEqual(result, expected)

    async def test_line_pagination_has_original_numbers_and_explicit_continuation(self):
        path = self.root / "lines.txt"
        path.write_text("\n".join(f"line {number}" for number in range(1, 1006)))
        page = await tools.execute_tool("read_file", {"file_path": str(path)})
        self.assertIn("1000 | line 1000", page)
        self.assertNotIn("1001 |", page)
        self.assertIn("offset=1001", page)
        last = await tools.execute_tool("read_file", {"file_path": str(path), "offset": 1001, "limit": 10})
        self.assertTrue(last.startswith("\n".join(f"{number:4d} | line {number}" for number in range(1001, 1006))))
        self.assertIn("Partial file", last)
        missing = await tools.execute_tool("read_file", {"file_path": str(path), "offset": 1010})
        self.assertIn("No lines found", missing)

    async def test_byte_limit_preserves_whole_lines_and_unicode(self):
        path = self.root / "wide.txt"
        path.write_text("\n".join(["好" * 5000, "好" * 5000, "尾" * 5000]))
        result = await tools.execute_tool("read_file", {"file_path": str(path)})
        self.assertIn("offset=2", result)
        self.assertNotIn("尾", result)
        last = await tools.execute_tool("read_file", {"file_path": str(path), "offset": 3})
        self.assertTrue(last.startswith("   3 | " + "尾" * 5000))
        self.assertIn("Partial file", last)

    async def test_giant_line_is_bounded_without_full_file_allocation(self):
        path = self.root / "giant.txt"
        path.write_text("大" * 2_000_000 + "\nnext")
        tracemalloc.start()
        try:
            result = tools._read_file({"file_path": str(path)})
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024)
        self.assertLess(len(result.encode("utf-8")), tools.MAX_READ_BYTES + 1000)
        self.assertIn("Line 1 exceeds", result)
        self.assertIn("offset=2", result)
        self.assertNotIn("�", result)
        next_page = await tools.execute_tool("read_file", {"file_path": str(path), "offset": 2})
        self.assertTrue(next_page.startswith("   2 | next"))
        self.assertIn("Partial file", next_page)

    async def test_numbered_pages_remain_below_result_artifact_threshold(self):
        path = self.root / "page.txt"
        for line in ("好" * 100, "x" * 40, "x" * 100_000):
            path.write_text((line + "\n") * (1 if len(line) > 1000 else 1001))
            result = await tools.execute_tool("read_file", {"file_path": str(path)}, truncate_result=False)
            self.assertLess(len(result.encode("utf-8")), 30 * 1024)
            self.assertIn("Partial file", result)

    async def test_invalid_paging_arguments_are_rejected(self):
        for extra in ({"offset": 0}, {"offset": 1.5}, {"limit": 0}, {"limit": 1001}):
            result = await tools.execute_tool("read_file", {"file_path": "unused", **extra})
            self.assertIn("invalid arguments", result)

    async def test_search_uses_python_regex_consistently(self):
        (self.root / "data.txt").write_text("a1\na22\na+\n")
        result = await tools.execute_tool("grep_search", {"pattern": r"a\d+"})
        self.assertIn(":1:a1", result)
        self.assertIn(":2:a22", result)
        self.assertNotIn("a+", result)


if __name__ == "__main__":
    unittest.main()
