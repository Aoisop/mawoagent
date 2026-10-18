from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agents import memory


class CountingReader(io.BytesIO):
    def __init__(self, content: bytes):
        super().__init__(content)
        self.bytes_read = 0

    def read(self, size=-1):
        result = super().read(size)
        self.bytes_read += len(result)
        return result

    def readline(self, size=-1):
        result = super().readline(size)
        self.bytes_read += len(result)
        return result


class MemoryIOTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        directory = patch.object(memory, "get_memory_dir", return_value=self.root)
        directory.start()
        self.addCleanup(directory.stop)

    def write_memory(self, name: str, body: bytes = b"preference") -> Path:
        path = self.root / name
        path.write_bytes(b"---\nname: demo\ntype: user\ndescription: preference\n---\n" + body)
        return path

    def test_header_scan_does_not_read_large_or_invalid_body(self):
        path = self.write_memory("large.md", b"x" * (2 * 1024 * 1024) + b"\xff")
        reader = CountingReader(path.read_bytes())
        with patch.object(Path, "open", return_value=reader):
            headers = memory.scan_memory_headers()
        self.assertEqual([header.filename for header in headers], ["large.md"])
        self.assertEqual(headers[0].description, "preference")
        self.assertLess(reader.bytes_read, 100)

    def test_unterminated_header_has_byte_limit(self):
        path = self.write_memory("long.md")
        reader = CountingReader(b"---\ndescription: " + b"x" * (2 * 1024 * 1024))
        with patch.object(Path, "open", return_value=reader):
            headers = memory.scan_memory_headers()
        self.assertEqual([header.filename for header in headers], [path.name])
        self.assertLessEqual(reader.bytes_read, memory.MAX_MEMORY_HEADER_BYTES)

    def test_only_newest_200_readable_headers_are_opened(self):
        for index in range(205):
            path = self.write_memory(f"memory-{index}.md")
            os.utime(path, (1000 + index, 1000 + index))
        # An unreadable newest header must not reduce the number of valid results.
        bad = self.root / "bad.md"
        bad.write_bytes(b"---\nname: \xff\n---\n")
        os.utime(bad, (2000, 2000))
        original_open = Path.open
        opened = []

        def tracked_open(path, *args, **kwargs):
            opened.append(path.name)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", tracked_open), self.assertLogs("agents.memory", level="WARNING"):
            headers = memory.scan_memory_headers()
        expected = [f"memory-{index}.md" for index in range(204, 4, -1)]
        self.assertEqual([header.filename for header in headers], expected)
        self.assertEqual(opened, ["bad.md", *expected])

    def test_listing_survives_file_disappearing_after_read(self):
        path = self.write_memory("removed.md")
        original_read = Path.read_text

        def read_then_remove(current, *args, **kwargs):
            text = original_read(current, *args, **kwargs)
            current.unlink()
            return text

        with patch.object(Path, "read_text", read_then_remove):
            entries = memory.list_memories()
        self.assertEqual([entry.filename for entry in entries], [path.name])

    async def test_recall_skips_deleted_and_invalid_files_but_keeps_valid_results(self):
        first = self.write_memory("first.md", b"first preference")
        removed = self.write_memory("removed.md")
        invalid = self.write_memory("invalid.md", b"\xff")
        last = self.write_memory("last.md", b"last preference")
        paths = [first, removed, invalid, last]
        headers = [memory.MemoryHeader(path.name, str(path), 0, "preference", "user") for path in paths]

        async def select(*_):
            removed.unlink()
            return json.dumps({"selected_memories": [path.name for path in paths]})

        with patch.object(memory, "scan_memory_headers", return_value=headers), \
                self.assertLogs("agents.memory", level="WARNING"):
            recalled = await memory.select_relevant_memories("query", select, set())
        self.assertEqual([item.path for item in recalled], [str(first), str(last)])

    async def test_recall_reads_only_byte_budget_and_preserves_utf8(self):
        path = self.write_memory("large.md")
        content = "中" * (1024 * 1024)
        reader = CountingReader(content.encode("utf-8"))
        header = memory.MemoryHeader(path.name, str(path), 0, "preference", "user")
        query = AsyncMock(return_value=json.dumps({"selected_memories": [path.name]}))
        with patch.object(memory, "scan_memory_headers", return_value=[header]), \
                patch.object(Path, "open", return_value=reader):
            recalled = await memory.select_relevant_memories("query", query, set())
        self.assertEqual(recalled[0].content, "中" * (memory.MAX_MEMORY_BYTES_PER_FILE // 3))
        self.assertLessEqual(reader.bytes_read, memory.MAX_MEMORY_BYTES_PER_FILE + 1)


if __name__ == "__main__":
    unittest.main()
