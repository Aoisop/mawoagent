"""Instance state isolation: two workspaces must not share state (I7 / T20)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents import memory, session, skills
from agents.workspace import WorkspaceContext, project_root, workspace_scope


class RootTests(unittest.TestCase):
    def test_process_directory_is_the_default(self) -> None:
        self.assertEqual(project_root(), Path.cwd())

    def test_a_bound_workspace_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = WorkspaceContext(root=Path(tmp))
            with workspace_scope(context):
                self.assertEqual(project_root(), context.root)
        self.assertEqual(project_root(), Path.cwd())

    def test_concurrent_scopes_do_not_see_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first, second = WorkspaceContext(root=Path(one)), WorkspaceContext(root=Path(two))
            seen: dict[str, Path] = {}

            async def probe(name: str, context: WorkspaceContext) -> None:
                with workspace_scope(context):
                    await asyncio.sleep(0.01)
                    seen[name] = project_root()

            async def run() -> None:
                await asyncio.gather(probe("a", first), probe("b", second))

            asyncio.run(run())
            self.assertEqual(seen["a"], first.root)
            self.assertEqual(seen["b"], second.root)


class MemoryIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.one = WorkspaceContext(root=root / "one")
        self.two = WorkspaceContext(root=root / "two")
        self.one.root.mkdir()
        self.two.root.mkdir()

    def test_memories_of_two_workspaces_are_separate(self) -> None:
        with workspace_scope(self.one):
            memory.save_memory("Only here", "d", "project", "x", source="user")
            first = memory.get_memory_dir()
        with workspace_scope(self.two):
            second = memory.get_memory_dir()
            self.assertEqual(memory.list_memories(), [])

        self.assertNotEqual(first, second)
        with workspace_scope(self.one):
            self.assertEqual(len(memory.list_memories()), 1)


class SessionIsolationTests(unittest.TestCase):
    def test_project_session_directories_follow_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            one, two = Path(tmp) / "a", Path(tmp) / "b"
            one.mkdir()
            two.mkdir()
            with workspace_scope(WorkspaceContext(root=one)):
                first = session.get_project_session_dir()
            with workspace_scope(WorkspaceContext(root=two)):
                second = session.get_project_session_dir()

            self.assertEqual(first, one / ".mawoagent" / "sessions")
            self.assertEqual(second, two / ".mawoagent" / "sessions")


class SkillIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

        self.one = WorkspaceContext(root=root / "one")
        self.two = WorkspaceContext(root=root / "two")
        skill = self.one.root / ".mawoagent" / "skills" / "demo" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: demo\ndescription: only in workspace one\n---\nDo the thing.\n",
            encoding="utf-8",
        )
        self.two.root.mkdir(parents=True, exist_ok=True)
        skills.reset_skill_cache()
        self.addCleanup(skills.reset_skill_cache)

    def test_a_project_skill_is_only_visible_in_its_workspace(self) -> None:
        with workspace_scope(self.one):
            names = [skill.name for skill in skills.discover_skills()]
        self.assertIn("demo", names)

        with workspace_scope(self.two):
            names = [skill.name for skill in skills.discover_skills()]
        self.assertNotIn("demo", names)


class PermissionRuleIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

        self.one = WorkspaceContext(root=root / "one")
        self.two = WorkspaceContext(root=root / "two")
        settings = self.one.root / ".mawoagent" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            '{"permissions": {"deny": ["run_shell"]}}', encoding="utf-8"
        )
        self.two.root.mkdir(parents=True, exist_ok=True)

    def test_rules_come_from_the_workspace_not_the_process(self) -> None:
        from agents.tools import load_permission_rules

        with workspace_scope(self.one):
            denied = load_permission_rules()["deny"]
        with workspace_scope(self.two):
            allowed = load_permission_rules()["deny"]

        self.assertEqual([rule["tool"] for rule in denied], ["run_shell"])
        self.assertEqual(allowed, [])

    def test_switching_back_reloads_the_first_workspace(self) -> None:
        from agents.tools import load_permission_rules

        with workspace_scope(self.one):
            load_permission_rules()
        with workspace_scope(self.two):
            load_permission_rules()
        with workspace_scope(self.one):
            self.assertEqual(len(load_permission_rules()["deny"]), 1)


if __name__ == "__main__":
    unittest.main()
