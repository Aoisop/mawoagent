"""Workspace range and security policy (iteration I1 / T03 and T05)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.policy import (
    DEFAULTS,
    load_security_policy,
    redact_secrets,
)
from agents.tools import check_permission
from agents.workspace import (
    WorkspaceContext,
    current_workspace,
    resolve_tool_path,
    workspace_scope,
)

WINDOWS = os.name == "nt"


def _make_junction(link: Path, target: Path) -> bool:
    """Create an NTFS junction; it needs no administrator rights."""

    if not WINDOWS:
        return False
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
    )
    return result.returncode == 0 and link.exists()


def _make_symlink(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return link.exists()


class PathClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("print('x')\n", encoding="utf-8")
        self.workspace = WorkspaceContext(root=self.root)

    def test_relative_path_stays_inside(self) -> None:
        decision = self.workspace.classify("src/app.py")
        self.assertEqual(decision.scope, "workspace")
        self.assertEqual(decision.resolved.relative, "src/app.py")

    def test_parent_traversal_is_outside(self) -> None:
        decision = self.workspace.classify("../outside.txt")
        self.assertEqual(decision.scope, "outside")

    def test_traversal_hidden_behind_an_inner_segment_is_outside(self) -> None:
        decision = self.workspace.classify("src/../../outside.txt")
        self.assertEqual(decision.scope, "outside")

    def test_absolute_path_outside_the_root_is_outside(self) -> None:
        outside = Path(self.tmp.name) / "other" / "file.txt"
        decision = self.workspace.classify(str(outside))
        self.assertEqual(decision.scope, "outside")

    def test_absolute_path_inside_the_root_stays_inside(self) -> None:
        decision = self.workspace.classify(str(self.root / "src" / "app.py"))
        self.assertEqual(decision.scope, "workspace")

    def test_unc_path_is_outside_without_touching_the_network(self) -> None:
        decision = self.workspace.classify(r"\\server\share\payload.txt")
        self.assertEqual(decision.scope, "outside")
        self.assertTrue(decision.resolved.is_unc)

    def test_protected_policy_files_are_flagged(self) -> None:
        for name in (".mawoagent/settings.json", ".mcp.json", "./.mcp.json"):
            with self.subTest(name=name):
                decision = self.workspace.classify(name)
                self.assertTrue(decision.protected, name)
                self.assertEqual(decision.scope, "workspace")

    def test_runtime_state_directory_is_its_own_scope(self) -> None:
        decision = self.workspace.classify(".mawoagent/sessions/abc.json")
        self.assertEqual(decision.scope, "runtime_state")
        self.assertFalse(decision.protected)

    def test_dot_prefixed_names_are_not_mistaken_for_relative_prefixes(self) -> None:
        # ".mawoagent" must not be read as "./mawoagent".
        decision = self.workspace.classify(".mawoagent/settings.json")
        self.assertTrue(decision.protected)

    @unittest.skipIf(os.name != "nt", "the extended-length prefix is Windows syntax")
    def test_extended_length_prefix_does_not_drop_protection(self) -> None:
        """\\\\?\\C:\\... is the same file; it must not become an unchecked one."""

        target = self.root / ".mawoagent" / "settings.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")

        plain = self.workspace.classify(str(target))
        prefixed = self.workspace.classify("\\\\?\\" + str(target))
        self.assertTrue(plain.protected)
        self.assertTrue(prefixed.protected)
        self.assertEqual(prefixed.scope, "workspace")

    def test_user_level_policy_files_are_protected(self) -> None:
        """They govern every project, so they cannot be just "outside"."""

        home = Path(self.tmp.name) / "home"
        (home / ".mawoagent").mkdir(parents=True)
        settings = home / ".mawoagent" / "settings.json"
        settings.write_text("{}", encoding="utf-8")
        trust = home / ".mawoagent" / "trusted-projects.json"
        trust.write_text("{}", encoding="utf-8")

        with patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
            for path in (settings, trust):
                with self.subTest(path=path.name):
                    decision = self.workspace.classify(path)
                    self.assertEqual(decision.scope, "outside")
                    self.assertTrue(decision.protected)

    def test_relative_paths_resolve_against_the_root_not_the_process_cwd(self) -> None:
        previous = os.getcwd()
        try:
            os.chdir(self.tmp.name)
            resolved = self.workspace.resolve("src/app.py")
        finally:
            os.chdir(previous)
        self.assertEqual(Path(resolved.absolute), self.root / "src" / "app.py")

    @unittest.skipUnless(WINDOWS, "junctions are a Windows feature")
    def test_junction_cannot_be_used_to_escape(self) -> None:
        outside = Path(self.tmp.name) / "outside-dir"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
        link = self.root / "link"
        if not _make_junction(link, outside):
            self.skipTest("junction creation is unavailable on this filesystem")
        decision = self.workspace.classify("link/secret.txt")
        self.assertEqual(decision.scope, "outside")

    def test_symlink_cannot_be_used_to_escape(self) -> None:
        outside = Path(self.tmp.name) / "outside-symlink-target"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
        link = self.root / "simlink"
        if not _make_symlink(link, outside):
            self.skipTest("symlink creation requires privileges on this platform")
        decision = self.workspace.classify("simlink/secret.txt")
        self.assertEqual(decision.scope, "outside")

    def test_symlink_inside_the_root_stays_inside(self) -> None:
        link = self.root / "alias"
        if not _make_symlink(link, self.root / "src"):
            self.skipTest("symlink creation requires privileges on this platform")
        decision = self.workspace.classify("alias/app.py")
        self.assertEqual(decision.scope, "workspace")


class WorkspaceBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = WorkspaceContext(root=self.root)

    def test_no_workspace_is_bound_by_default(self) -> None:
        self.assertIsNone(current_workspace())

    def test_scope_binds_and_restores(self) -> None:
        with workspace_scope(self.workspace):
            self.assertIs(current_workspace(), self.workspace)
        self.assertIsNone(current_workspace())

    def test_bound_workspace_drives_tool_path_resolution(self) -> None:
        previous = os.getcwd()
        try:
            os.chdir(Path(self.tmp.name).parent)
            with workspace_scope(self.workspace):
                resolved = resolve_tool_path("nested/file.txt")
        finally:
            os.chdir(previous)
        self.assertEqual(resolved, self.root / "nested" / "file.txt")

    def test_unbound_resolution_keeps_the_requested_path(self) -> None:
        self.assertEqual(resolve_tool_path("nested/file.txt"), Path("nested/file.txt"))


class PolicyResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.project = tempfile.TemporaryDirectory()
        self.addCleanup(self.project.cleanup)
        self.home_path = Path(self.home.name)
        self.project_path = Path(self.project.name)
        self.home_patch = patch.object(Path, "home", return_value=self.home_path)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def _write(self, root: Path, payload: dict) -> None:
        target = root / ".mawoagent" / "settings.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")

    def test_defaults_when_no_settings_exist(self) -> None:
        policy = load_security_policy(self.project_path)
        self.assertEqual(policy.settings["workspace_only_reads"].value, DEFAULTS["workspace_only_reads"])
        self.assertEqual(policy.settings["workspace_only_writes"].value, DEFAULTS["workspace_only_writes"])
        self.assertEqual(policy.settings["workspace_only_writes"].source, "default")
        self.assertEqual(policy.warnings, ())

    def test_user_settings_apply_without_trust(self) -> None:
        self._write(self.home_path, {"security": {"workspace_only_reads": True}})
        policy = load_security_policy(self.project_path)
        self.assertTrue(policy.settings["workspace_only_reads"].value)
        self.assertEqual(policy.settings["workspace_only_reads"].source, "user")

    def test_untrusted_project_settings_are_ignored_with_a_warning(self) -> None:
        self._write(self.project_path, {"security": {"workspace_only_writes": False}})
        policy = load_security_policy(self.project_path)
        self.assertTrue(policy.settings["workspace_only_writes"].value)
        self.assertEqual(policy.settings["workspace_only_writes"].source, "default")
        self.assertTrue(any("not trusted" in item for item in policy.warnings))

    def test_trusted_project_settings_override_user_settings(self) -> None:
        self._write(self.home_path, {"security": {"workspace_only_reads": True}})
        self._write(self.project_path, {"security": {"workspace_only_reads": False}})
        with patch("agents.project_trust.is_project_trusted", return_value=True):
            policy = load_security_policy(self.project_path)
        self.assertFalse(policy.settings["workspace_only_reads"].value)
        self.assertEqual(policy.settings["workspace_only_reads"].source, "project")

    def test_invalid_values_are_reported_and_ignored(self) -> None:
        self._write(self.home_path, {"security": {"workspace_only_writes": "yes please"}})
        policy = load_security_policy(self.project_path)
        self.assertTrue(policy.settings["workspace_only_writes"].value)
        self.assertTrue(any("invalid value" in item for item in policy.warnings))

    def test_policy_builds_a_workspace_context(self) -> None:
        self._write(self.home_path, {"security": {"protected_paths": ["secrets/"]}})
        policy = load_security_policy(self.project_path)
        context = policy.workspace_context(self.project_path)
        self.assertEqual(Path(context.root).resolve(), self.project_path.resolve())
        self.assertEqual(context.protected_paths, ("secrets",))
        self.assertEqual(context.source, "user")

    def test_root_spelling_does_not_change_classification(self) -> None:
        # The stored root keeps the spelling it was given; classification must
        # not depend on it. Windows 8.3 short names and the macOS /var symlink
        # both make an unresolved root differ from its resolved form.
        alias = Path(self.home.name) / "root-alias"
        if not (_make_junction(alias, self.project_path) or _make_symlink(alias, self.project_path)):
            self.skipTest("alternate root spelling requires a junction or symlink")
        aliased = WorkspaceContext(root=alias)
        direct = WorkspaceContext(root=self.project_path.resolve())
        self.assertEqual(aliased.root_real, direct.root_real)
        self.assertEqual(aliased.classify("config.txt").scope, "workspace")
        self.assertEqual(direct.classify("config.txt").scope, "workspace")

    def test_describe_reports_sources_without_secrets(self) -> None:
        self._write(
            self.home_path,
            {"security": {"workspace_only_writes": False}, "api_key": "sk-live-abcdefghijkl"},
        )
        described = load_security_policy(self.project_path).describe()
        dumped = json.dumps(described, ensure_ascii=False)
        self.assertIn("sources", described)
        self.assertNotIn("sk-live-abcdefghijkl", dumped)

    def test_redaction_masks_credential_keys_and_tokens(self) -> None:
        payload = {
            "api_key": "sk-live-abcdefghijkl",
            "nested": {"OPENAI_API_KEY": "value", "note": "token sk-test-1234567890 here"},
            "items": ["plain", "ghp_ABCDEFGHIJKLMN"],
        }
        redacted = redact_secrets(payload)
        self.assertEqual(redacted["api_key"], "[redacted]")
        self.assertEqual(redacted["nested"]["OPENAI_API_KEY"], "[redacted]")
        self.assertNotIn("sk-test-1234567890", json.dumps(redacted))
        self.assertNotIn("ghp_ABCDEFGHIJKLMN", json.dumps(redacted))
        self.assertIn("plain", json.dumps(redacted))


class PermissionScopeTests(unittest.TestCase):
    """The workspace range must apply before blanket mode shortcuts."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.outside = Path(self.tmp.name) / "outside.txt"
        self.outside.write_text("x\n", encoding="utf-8")
        self.workspace = WorkspaceContext(root=self.root)
        self.rules = patch("agents.tools._check_permission_rules", return_value=None)
        self.rules.start()
        self.addCleanup(self.rules.stop)

    def test_reading_inside_is_unchanged(self) -> None:
        decision = check_permission("read_file", {"file_path": "README.md"}, "default", None, self.workspace)
        self.assertEqual(decision["action"], "allow")

    def test_reading_outside_is_allowed_by_default(self) -> None:
        decision = check_permission(
            "read_file", {"file_path": str(self.outside)}, "default", None, self.workspace
        )
        self.assertEqual(decision["action"], "allow")

    def test_reading_outside_is_denied_when_the_policy_says_so(self) -> None:
        workspace = WorkspaceContext(root=self.root, workspace_only_reads=True)
        decision = check_permission(
            "read_file", {"file_path": str(self.outside)}, "default", None, workspace
        )
        self.assertEqual(decision["action"], "confirm")
        denied = check_permission(
            "read_file", {"file_path": str(self.outside)}, "dontAsk", None, workspace
        )
        self.assertEqual(denied["action"], "deny")

    def test_writing_outside_requires_confirmation(self) -> None:
        decision = check_permission(
            "write_file", {"file_path": str(self.outside)}, "default", None, self.workspace
        )
        self.assertEqual(decision["action"], "confirm")
        self.assertIn("outside the workspace", decision["message"])

    def test_accept_edits_does_not_bypass_the_range(self) -> None:
        decision = check_permission(
            "write_file", {"file_path": str(self.outside)}, "acceptEdits", None, self.workspace
        )
        self.assertEqual(decision["action"], "confirm")

    def test_dont_ask_denies_outside_writes(self) -> None:
        decision = check_permission(
            "write_file", {"file_path": str(self.outside)}, "dontAsk", None, self.workspace
        )
        self.assertEqual(decision["action"], "deny")

    def test_writing_inside_is_unchanged(self) -> None:
        decision = check_permission(
            "write_file", {"file_path": "notes.txt"}, "acceptEdits", None, self.workspace
        )
        self.assertEqual(decision["action"], "allow")

    def test_policy_files_need_confirmation_even_inside(self) -> None:
        decision = check_permission(
            "write_file", {"file_path": ".mawoagent/settings.json"}, "acceptEdits", None, self.workspace
        )
        self.assertEqual(decision["action"], "confirm")
        self.assertIn("policy file", decision["message"])

    def test_runtime_state_writes_need_confirmation(self) -> None:
        decision = check_permission(
            "edit_file", {"file_path": ".mawoagent/sessions/a.json"}, "acceptEdits", None, self.workspace
        )
        self.assertEqual(decision["action"], "confirm")

    def test_an_explicit_rule_widens_the_range(self) -> None:
        with patch("agents.tools._check_permission_rules", return_value="allow"):
            decision = check_permission(
                "write_file", {"file_path": str(self.outside)}, "default", None, self.workspace
            )
        self.assertEqual(decision["action"], "allow")

    def test_shell_commands_are_not_governed_by_the_range(self) -> None:
        # The policy is an approval boundary for file tools, not a sandbox.
        decision = check_permission(
            "run_shell", {"command": "echo hi"}, "default", None, self.workspace
        )
        self.assertEqual(decision["action"], "confirm")
        self.assertNotIn("workspace", json.dumps(decision))

    def test_instruction_bearing_files_are_protected(self) -> None:
        # rules/ and the Skill/agent definitions are read into later prompts.
        for name in (
            ".mawoagent/rules/team.md",
            ".mawoagent/skills/demo/SKILL.md",
            ".mawoagent/agents/reviewer.md",
        ):
            with self.subTest(name=name):
                decision = self.workspace.classify(name)
                self.assertTrue(decision.protected, name)
                self.assertEqual(decision.scope, "workspace")

    def test_bypass_permissions_still_refuses_policy_file_writes(self) -> None:
        # "Do not ask" must not become "may rewrite its own policy".
        for name in (
            ".mawoagent/settings.json",
            ".mcp.json",
            ".mawoagent/rules/team.md",
            ".mawoagent/skills/demo/SKILL.md",
            ".mawoagent/agents/reviewer.md",
        ):
            with self.subTest(name=name):
                decision = check_permission(
                    "write_file", {"file_path": name}, "bypassPermissions", None, self.workspace
                )
                self.assertEqual(decision["action"], "deny", name)
                self.assertIn("bypasspermissions", decision["message"].lower())

    def test_bypass_permissions_still_allows_ordinary_writes(self) -> None:
        decision = check_permission(
            "write_file", {"file_path": str(self.outside)}, "bypassPermissions", None, self.workspace
        )
        self.assertEqual(decision["action"], "allow")

    def test_bypass_permissions_without_a_workspace_keeps_legacy_behaviour(self) -> None:
        decision = check_permission("write_file", {"file_path": ".mcp.json"}, "bypassPermissions")
        self.assertEqual(decision["action"], "allow")


if __name__ == "__main__":
    unittest.main()
