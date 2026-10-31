"""Symbol index, repository map and /symbols (I3 / T08)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import repo_index
from agents.repo_index import (
    build_repo_map_section,
    format_search,
    format_stats,
    index_path,
    load_index,
    refresh_index,
    repo_map_enabled,
)

PY_SOURCE = '''"""Module docstring."""


def helper(value, *args, **kwargs):
    return value


class Widget:
    def render(self):
        return "x"

    async def load(self, path):
        return path


if True:
    def conditional():
        pass
'''

JS_SOURCE = """export function startServer(port) {
  return port;
}

export class Router {
  handle(request) {
    return request;
  }
}

const buildUrl = (base, path) => base + path;
"""


class IndexTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class PythonSymbolTests(IndexTestCase):
    def test_functions_classes_and_methods_are_indexed(self) -> None:
        self.write("app.py", PY_SOURCE)
        index = refresh_index(self.root)
        entry = index.files["app.py"]
        by_name = {symbol.name: symbol for symbol in entry.symbols}

        self.assertEqual(by_name["helper"].kind, "function")
        self.assertIn("*args", by_name["helper"].signature)
        self.assertIn("**kwargs", by_name["helper"].signature)
        self.assertEqual(by_name["Widget"].kind, "class")
        self.assertEqual(by_name["Widget.render"].kind, "method")
        self.assertEqual(by_name["Widget.load"].signature.split("(")[0], "async def Widget.load")

    def test_line_numbers_point_at_the_definition(self) -> None:
        self.write("app.py", PY_SOURCE)
        entry = refresh_index(self.root).files["app.py"]
        helper = next(symbol for symbol in entry.symbols if symbol.name == "helper")
        self.assertEqual(PY_SOURCE.splitlines()[helper.line - 1].strip(), "def helper(value, *args, **kwargs):")

    def test_syntax_errors_are_recorded_per_file(self) -> None:
        self.write("broken.py", "def broken(:\n")
        entry = refresh_index(self.root).files["broken.py"]
        self.assertEqual(entry.symbols, [])
        self.assertIn("syntax error", entry.error)


class ScriptSymbolTests(IndexTestCase):
    def test_javascript_patterns_are_matched(self) -> None:
        self.write("web/app.js", JS_SOURCE)
        index = refresh_index(self.root)
        names = {symbol.name: symbol.kind for symbol in index.files["web/app.js"].symbols}

        self.assertEqual(names["startServer"], "function")
        self.assertEqual(names["Router"], "class")
        self.assertEqual(names["handle"], "method")
        self.assertEqual(names["buildUrl"], "function")
        self.assertTrue(index.approximate)


class IncrementalTests(IndexTestCase):
    def test_unchanged_files_are_not_reparsed(self) -> None:
        self.write("a.py", "def one():\n    pass\n")
        self.write("b.py", "def two():\n    pass\n")
        refresh_index(self.root)

        calls: list[str] = []
        original = repo_index._parse_file

        def counting(path, language):
            calls.append(path.name)
            return original(path, language)

        with patch.object(repo_index, "_parse_file", counting):
            refresh_index(self.root)
        self.assertEqual(calls, [])

    def test_edited_file_is_reparsed_and_symbols_replace_the_old_ones(self) -> None:
        path = self.write("a.py", "def before():\n    pass\n")
        refresh_index(self.root)

        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        path.write_text("def after():\n    pass\n", encoding="utf-8")

        index = refresh_index(self.root)
        names = [symbol.name for symbol in index.files["a.py"].symbols]
        self.assertEqual(names, ["after"])

    def test_deleted_file_disappears(self) -> None:
        path = self.write("gone.py", "def old():\n    pass\n")
        refresh_index(self.root)
        path.unlink()

        self.assertNotIn("gone.py", refresh_index(self.root).files)

    def test_renamed_file_moves(self) -> None:
        path = self.write("old.py", "def moved():\n    pass\n")
        refresh_index(self.root)
        path.rename(self.root / "new.py")

        index = refresh_index(self.root)
        self.assertNotIn("old.py", index.files)
        self.assertIn("new.py", index.files)
        self.assertEqual([symbol.name for symbol in index.files["new.py"].symbols], ["moved"])

    def test_index_is_written_and_reloaded(self) -> None:
        self.write("a.py", "def one():\n    pass\n")
        refresh_index(self.root)
        self.assertTrue(index_path(self.root).exists())

        loaded = load_index(self.root)
        self.assertIsNotNone(loaded)
        self.assertEqual(list(loaded.files), ["a.py"])

    def test_index_with_a_foreign_version_is_ignored(self) -> None:
        self.write("a.py", "def one():\n    pass\n")
        refresh_index(self.root)
        data = json.loads(index_path(self.root).read_text(encoding="utf-8"))
        data["version"] = 999
        index_path(self.root).write_text(json.dumps(data), encoding="utf-8")

        self.assertIsNone(load_index(self.root))


class IgnoreRuleTests(IndexTestCase):
    def test_dependency_and_state_directories_are_skipped(self) -> None:
        self.write("app.py", "def real():\n    pass\n")
        for directory in ("node_modules/pkg", ".venv/lib", ".mawoagent/cache", "__pycache__"):
            self.write(f"{directory}/ignored.py", "def hidden():\n    pass\n")

        index = refresh_index(self.root)
        self.assertEqual(list(index.files), ["app.py"])

    def test_oversized_files_are_skipped(self) -> None:
        big = self.root / "huge.py"
        big.write_text("x = 1\n" + "# padding\n" * 60_000, encoding="utf-8")
        self.assertGreater(big.stat().st_size, repo_index.MAX_FILE_BYTES)

        self.assertEqual(refresh_index(self.root).files, {})

    def test_only_known_languages_are_indexed(self) -> None:
        self.write("notes.md", "# heading\n")
        self.write("data.json", "{}\n")
        self.assertEqual(refresh_index(self.root).files, {})


class SearchTests(IndexTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("app.py", "class Agent:\n    pass\n\ndef run_agent():\n    pass\n")
        self.write("other.py", "def unrelated():\n    pass\n")
        self.index = refresh_index(self.root)

    def test_exact_matches_come_before_partial_ones(self) -> None:
        results = self.index.search("Agent")
        self.assertEqual(results[0][1].name, "Agent")
        self.assertIn("run_agent", [symbol.name for _, symbol in results])

    def test_search_is_case_insensitive(self) -> None:
        self.assertTrue(self.index.search("agent"))

    def test_empty_query_returns_nothing(self) -> None:
        self.assertEqual(self.index.search("   "), [])

    def test_limit_is_respected(self) -> None:
        self.assertEqual(len(self.index.search("a", limit=1)), 1)

    def test_formatting_includes_location_and_kind(self) -> None:
        text = format_search(self.index.search("Agent"))
        self.assertIn("app.py:", text)
        self.assertIn("class Agent", text)

    def test_stats_report_files_and_languages(self) -> None:
        text = format_stats(self.index)
        self.assertIn("2 file(s)", text)
        self.assertIn("python", text)


class MapTests(IndexTestCase):
    def test_map_lists_public_symbols_before_private_ones(self) -> None:
        self.write("app.py", "def _hidden():\n    pass\n\ndef visible():\n    pass\n")
        text = refresh_index(self.root).map_text(500)
        line = next(line for line in text.splitlines() if line.startswith("- app.py"))
        self.assertLess(line.index("visible"), line.index("_hidden"))

    def test_methods_are_left_out_of_the_map(self) -> None:
        self.write("app.py", "class Widget:\n    def render(self):\n        pass\n")
        text = refresh_index(self.root).map_text(500)
        self.assertNotIn("render", text)

    def test_budget_is_respected(self) -> None:
        for number in range(12):
            self.write(f"module{number}.py", "def exported():\n    pass\n")
        text = refresh_index(self.root).map_text(120)
        self.assertLess(len(text), 260)
        self.assertIn("omitted by the size budget", text)


class PromptSectionTests(IndexTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("app.py", "def visible():\n    pass\n")

    def test_section_includes_a_scope_header(self) -> None:
        with patch.dict(os.environ, {"MAWOAGENT_REPO_MAP": "on"}):
            text = build_repo_map_section(self.root, budget=500)
        self.assertTrue(text.startswith("## Repository map (1 file"))
        self.assertIn("app.py", text)

    def test_environment_switch_disables_the_section(self) -> None:
        with patch.dict(os.environ, {"MAWOAGENT_REPO_MAP": "off"}):
            self.assertEqual(build_repo_map_section(self.root), "")
            self.assertFalse(repo_map_enabled(self.root))

    def test_settings_switch_disables_the_section(self) -> None:
        with patch.dict(os.environ, {"MAWOAGENT_REPO_MAP": ""}):
            with patch.object(repo_index, "_setting", return_value=False):
                self.assertFalse(repo_map_enabled(self.root))
                self.assertEqual(build_repo_map_section(self.root), "")

    def test_budget_setting_is_honoured(self) -> None:
        for number in range(10):
            self.write(f"mod{number}.py", "def exported():\n    pass\n")
        with patch.dict(os.environ, {"MAWOAGENT_REPO_MAP": "on"}):
            with patch.object(repo_index, "_setting", side_effect=lambda root, key, default: 120 if key == "repo_map_budget" else default):
                text = build_repo_map_section(self.root)
        self.assertIn("omitted by the size budget", text)


if __name__ == "__main__":
    unittest.main()
