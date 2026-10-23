"""YAML frontmatter: the subset, its fallback, and round-tripping (I3 / T10)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agents.frontmatter import (
    as_bool,
    as_list,
    as_text,
    format_frontmatter,
    parse_frontmatter,
)
from agents.skills import _parse_skill_file


class LegacyCompatibilityTests(unittest.TestCase):
    def test_simple_pairs_still_parse(self) -> None:
        result = parse_frontmatter("---\nname: demo\ndescription: does things\n---\nbody text\n")
        self.assertEqual(result.meta, {"name": "demo", "description": "does things"})
        self.assertEqual(result.body, "body text")

    def test_content_without_frontmatter_is_all_body(self) -> None:
        result = parse_frontmatter("# Title\n\ntext\n")
        self.assertEqual(result.meta, {})
        self.assertEqual(result.body, "# Title\n\ntext")

    def test_unterminated_block_is_treated_as_body(self) -> None:
        result = parse_frontmatter("---\nname: demo\nbody\n")
        self.assertEqual(result.meta, {})
        self.assertIn("name: demo", result.body)

    def test_malformed_indentation_falls_back_instead_of_raising(self) -> None:
        result = parse_frontmatter("---\nname: demo\n  stray: value\n---\nbody\n")
        self.assertEqual(result.meta.get("name"), "demo")
        self.assertEqual(result.meta.get("stray"), "value")

    def test_crlf_is_accepted(self) -> None:
        result = parse_frontmatter("---\r\nname: demo\r\n---\r\nbody\r\n")
        self.assertEqual(result.meta, {"name": "demo"})
        self.assertEqual(result.body, "body")


class ScalarTests(unittest.TestCase):
    def test_booleans_null_and_numbers(self) -> None:
        result = parse_frontmatter(
            "---\nflag: false\nother: yes\nempty: null\ncount: 3\nratio: 0.5\n---\n"
        )
        self.assertIs(result.meta["flag"], False)
        self.assertIs(result.meta["other"], True)
        self.assertIsNone(result.meta["empty"])
        self.assertEqual(result.meta["count"], 3)
        self.assertEqual(result.meta["ratio"], 0.5)

    def test_quoted_values_keep_colons_and_hashes(self) -> None:
        result = parse_frontmatter(
            '---\nurl: "https://example.test/a:b"\nnote: \'literal # not a comment\'\n---\n'
        )
        self.assertEqual(result.meta["url"], "https://example.test/a:b")
        self.assertEqual(result.meta["note"], "literal # not a comment")

    def test_comments_are_stripped_outside_quotes(self) -> None:
        result = parse_frontmatter("---\nname: demo  # the name\n---\n")
        self.assertEqual(result.meta["name"], "demo")

    def test_list_and_map_flow_forms(self) -> None:
        result = parse_frontmatter("---\ntools: [Read, Write]\nmeta: {owner: team, level: 2}\n---\n")
        self.assertEqual(result.meta["tools"], ["Read", "Write"])
        self.assertEqual(result.meta["meta"], {"owner": "team", "level": 2})


class StructureTests(unittest.TestCase):
    def test_block_list(self) -> None:
        result = parse_frontmatter("---\ntools:\n  - Read\n  - Write\nname: demo\n---\n")
        self.assertEqual(result.meta["tools"], ["Read", "Write"])
        self.assertEqual(result.meta["name"], "demo")

    def test_nested_mapping(self) -> None:
        result = parse_frontmatter(
            "---\nowner:\n  team: runtime\n  contact: dev@example.test\nname: demo\n---\n"
        )
        self.assertEqual(result.meta["owner"], {"team": "runtime", "contact": "dev@example.test"})
        self.assertEqual(result.meta["name"], "demo")

    def test_list_of_mappings(self) -> None:
        result = parse_frontmatter("---\nsteps:\n  - id: one\n    run: echo 1\n  - id: two\n---\n")
        self.assertEqual(result.meta["steps"], [{"id": "one", "run": "echo 1"}, {"id": "two"}])

    def test_literal_block_scalar(self) -> None:
        result = parse_frontmatter("---\nnotes: |\n  line one\n  line two\nname: demo\n---\n")
        self.assertEqual(result.meta["notes"], "line one\nline two\n")

    def test_literal_block_without_trailing_newline(self) -> None:
        result = parse_frontmatter("---\nnotes: |-\n  line one\n  line two\n---\n")
        self.assertEqual(result.meta["notes"], "line one\nline two")

    def test_folded_block_joins_lines(self) -> None:
        result = parse_frontmatter("---\nsummary: >\n  first part\n  second part\n---\n")
        self.assertEqual(result.meta["summary"], "first part second part\n")


class RoundTripTests(unittest.TestCase):
    def assert_round_trip(self, meta: dict) -> None:
        text = format_frontmatter(meta, "body")
        parsed = parse_frontmatter(text)
        self.assertEqual(parsed.meta, meta)
        self.assertEqual(parsed.body, "body")

    def test_rich_mapping_survives_a_round_trip(self) -> None:
        self.assert_round_trip({
            "name": "demo",
            "description": "has: a colon",
            "user-invocable": False,
            "context": "fork",
            "version": 3,
            "ratio": 0.5,
            "empty": None,
            "tools": ["Read", "Write"],
            "nested": {"owner": "team", "tags": ["a", "b"]},
            "notes": "line one\nline two\n",
        })

    def test_values_that_look_like_yaml_are_quoted(self) -> None:
        for value in ("true", "false", "null", "123", "0.5", "- dash", "# hash", ": colon"):
            with self.subTest(value=value):
                self.assert_round_trip({"key": value})

    def test_unicode_is_written_as_is(self) -> None:
        text = format_frontmatter({"description": "中文说明"}, "body")
        self.assertIn("中文说明", text)
        self.assertEqual(parse_frontmatter(text).meta["description"], "中文说明")

    def test_multiline_without_trailing_newline(self) -> None:
        self.assert_round_trip({"notes": "first\nsecond"})


class CoercionTests(unittest.TestCase):
    def test_as_bool_accepts_native_and_textual_values(self) -> None:
        self.assertTrue(as_bool(True))
        self.assertFalse(as_bool(False))
        self.assertFalse(as_bool("false"))
        self.assertTrue(as_bool("yes"))
        self.assertTrue(as_bool(None, default=True))
        self.assertFalse(as_bool("unclear", default=False))

    def test_as_list_accepts_lists_and_comma_strings(self) -> None:
        self.assertEqual(as_list(["a", "b"]), ["a", "b"])
        self.assertEqual(as_list("a, b"), ["a", "b"])
        self.assertEqual(as_list("[a, b]"), ["a", "b"])
        self.assertEqual(as_list(None), [])

    def test_as_text_flattens_structures(self) -> None:
        self.assertEqual(as_text(None), "")
        self.assertEqual(as_text(False), "false")
        self.assertEqual(as_text(["a", "b"]), "a, b")


class SkillIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "SKILL.md"

    def _write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def test_boolean_and_block_list_reach_the_skill_definition(self) -> None:
        self._write(
            "---\n"
            "name: demo-skill\n"
            'description: "Uses: colons"\n'
            "user-invocable: false\n"
            "context: fork\n"
            "allowed-tools:\n"
            "  - Read\n"
            "  - Write\n"
            "---\n"
            "body\n"
        )
        skill = _parse_skill_file(self.path, "project", self.tmp.name)

        self.assertEqual(skill.name, "demo-skill")
        self.assertEqual(skill.description, "Uses: colons")
        self.assertFalse(skill.user_invocable)
        self.assertEqual(skill.context, "fork")
        self.assertEqual(skill.allowed_tools, ["Read", "Write"])

    def test_legacy_comma_tools_still_work(self) -> None:
        self._write("---\nname: legacy\nallowed-tools: Read, Write\n---\nbody\n")
        skill = _parse_skill_file(self.path, "project", self.tmp.name)
        self.assertEqual(skill.allowed_tools, ["Read", "Write"])

    def test_defaults_are_unchanged(self) -> None:
        self._write("---\nname: minimal\n---\nbody\n")
        skill = _parse_skill_file(self.path, "project", self.tmp.name)
        self.assertTrue(skill.user_invocable)
        self.assertEqual(skill.context, "inline")
        self.assertIsNone(skill.allowed_tools)


if __name__ == "__main__":
    unittest.main()
