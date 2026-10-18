import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents import online_skill_eval as evaluation
from agents import skill_candidates as candidates
from agents import skill_evolution as evolution
from agents.frontmatter import parse_frontmatter


class CandidateSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module in (candidates, evolution, evaluation):
            self.stack.enter_context(patch.object(module, "get_evolution_dir", return_value=self.root / "evolution"))
        self.stack.enter_context(patch.object(evolution, "_skills_root", return_value=self.root / "skills"))
        self.path = self.root / "skills" / "demo" / "SKILL.md"
        self.path.parent.mkdir(parents=True)
        self.path.write_text("---\nname: demo\ndescription: demo\nversion: 0.1.0\n---\nBASE instructions\n")
        skill = SimpleNamespace(name="demo", skill_dir=str(self.path.parent))
        self.stack.enter_context(patch("agents.skills.get_skill_by_name", side_effect=lambda name: skill if name == "demo" else None))

    def stage(self):
        with patch.object(evolution, "_today", return_value="2026-10-15"):
            result = candidates.stage_candidate("merge", {
                "skill_name": "demo", "lesson": "Prefer the verified response",
                "rationale": "It satisfies the regression case", "instructions": "CANDIDATE instructions",
            })
        return result["candidate_id"]

    def save_champion(self, snapshot):
        registry = self.root / "evolution" / "online-eval" / "champions.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps({"champions": {
            evaluation._lineage_id_for_skill("demo"): {"snapshot": snapshot},
        }}))

    def replace_proposal(self, candidate_id, proposal):
        candidates._path(candidate_id).write_text(json.dumps(proposal))

    @staticmethod
    def approval_hash(proposal):
        return hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()

    async def test_cross_day_evaluation_and_publication_use_identical_body(self):
        candidate_id = self.stage()
        proposal = candidates.read_candidate(candidate_id)
        snapshot = candidates.candidate_snapshot(proposal)
        approval_hash = self.approval_hash(proposal)
        samples = [{"sample_id": split, "split": split, "task_user": "task", "context_messages": [],
                    "requirements": [], "checks": [{"mode": "contains", "text": "GOOD"}]}
                   for split in ("mutate_dev", "promotion_test")]

        async def responder(system, user):
            return "GOOD" if "CANDIDATE instructions" in system else "BAD"

        with patch.object(evolution, "_today", return_value="2026-10-16"):
            next_day = candidates.candidate_snapshot(candidates.read_candidate(candidate_id))
            self.assertEqual(next_day, snapshot)
            bundle = await evaluation._run_comparison(
                {"name": "demo", "instructions": "BASE instructions"}, samples,
                evaluation.QueryBudget(responder), {}, max_candidates=1, min_replay_samples=2,
                min_promotion_tests=1, min_rule_pass_rate=.8, usage_eligible=True,
                proposed_candidates=[next_day],
            )
            self.assertTrue(bundle["promoted"])
            evaluation._persist_eval_artifacts("demo", samples, bundle)
        # Publishing must not render the body again, even if the clock changes.
        with patch.object(evolution, "_today", side_effect=AssertionError("unexpected date lookup")):
            result = candidates.apply_candidate(candidate_id, expected_hash=approval_hash)
        self.assertTrue(result["ok"], result)
        body = parse_frontmatter(self.path.read_text()).body
        self.assertEqual(body, snapshot["instructions"])
        self.assertIn("2026-10-15", body)
        self.assertNotIn("2026-10-16", body)
        self.assertEqual(body.count("## Evolution Notes"), 1)

    def test_added_skill_publishes_exact_snapshot_with_evidence_once(self):
        with patch.object(candidates, "resolve_skill_file", return_value=None), \
                patch.object(evolution, "resolve_skill_file", return_value=None):
            result = candidates.stage_candidate("add", {
                "name": "new", "description": "Reusable workflow", "instructions": "Follow the workflow",
                "evidence": "User correction", "context": "fork", "allowed_tools": "read_file",
            })
            proposal = candidates.read_candidate(result["candidate_id"])
            snapshot = candidates.candidate_snapshot(proposal)
            applied = candidates.apply_candidate(result["candidate_id"], manual_review=True)
        self.assertTrue(applied["ok"], applied)
        published = parse_frontmatter(Path(applied["file"]).read_text())
        self.assertEqual(published.body, snapshot["instructions"])
        self.assertEqual(published.body.count("## Creation Evidence"), 1)
        self.assertEqual(published.meta["allowed-tools"], "read_file")
        self.assertEqual(published.meta["context"], "fork")

    def test_legacy_candidate_migrates_once_and_requires_new_review(self):
        candidate_id = self.stage()
        legacy = candidates.read_candidate(candidate_id)
        old_snapshot = legacy.pop("snapshot")
        old_snapshot.pop("proposal_hash")
        legacy.pop("snapshot_date")
        self.replace_proposal(candidate_id, legacy)
        self.save_champion(old_snapshot)
        old_approval = self.approval_hash(legacy)
        with patch.object(evolution, "_today", return_value="2026-10-16"):
            migrated = candidates.read_candidate(candidate_id)
        disk_bytes = candidates._path(candidate_id).read_bytes()
        with patch.object(evolution, "_today", side_effect=AssertionError("migration repeated")):
            self.assertEqual(candidates.read_candidate(candidate_id), migrated)
            self.assertEqual(candidates._path(candidate_id).read_bytes(), disk_bytes)
            self.assertIn("no matching passed evaluation", candidates.apply_candidate(candidate_id)["error"])
            denied = candidates.apply_candidate(candidate_id, manual_review=True, expected_hash=old_approval)
            self.assertIn("changed during approval", denied["error"])
            accepted = candidates.apply_candidate(candidate_id, manual_review=True,
                                                  expected_hash=self.approval_hash(migrated))
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(parse_frontmatter(self.path.read_text()).body, migrated["snapshot"]["instructions"])

    def test_changed_body_parameters_or_date_are_rejected_even_with_manual_review(self):
        for mutation in ("body", "instructions", "date", "partial"):
            with self.subTest(mutation=mutation):
                candidate_id = self.stage()
                proposal = candidates.read_candidate(candidate_id)
                if mutation == "body":
                    proposal["snapshot"]["instructions"] = "UNEVALUATED"
                elif mutation == "instructions":
                    proposal["write_input"]["instructions"] = "UNEVALUATED"
                elif mutation == "date":
                    proposal["snapshot_date"] = "2026-10-17"
                else:
                    del proposal["snapshot"]
                self.replace_proposal(candidate_id, proposal)
                original = self.path.read_bytes()
                with self.assertRaises(ValueError):
                    candidates.apply_candidate(candidate_id, manual_review=True)
                self.assertEqual(self.path.read_bytes(), original)

    def test_changed_metadata_with_rebuilt_snapshot_cannot_reuse_evaluation_or_approval(self):
        candidate_id = self.stage()
        proposal = candidates.read_candidate(candidate_id)
        snapshot = candidates.candidate_snapshot(proposal)
        approval_hash = self.approval_hash(proposal)
        self.save_champion(snapshot)
        proposal["write_input"]["when_to_use"] = "A different trigger"
        proposal["snapshot"] = candidates._render_snapshot(proposal)
        self.assertEqual(proposal["snapshot"]["instructions"], snapshot["instructions"])
        self.replace_proposal(candidate_id, proposal)
        self.assertIn("changed during approval", candidates.apply_candidate(candidate_id, expected_hash=approval_hash)["error"])
        self.assertIn("no matching passed evaluation", candidates.apply_candidate(candidate_id)["error"])

    def test_base_change_blocks_publication_even_after_passed_evaluation(self):
        candidate_id = self.stage()
        proposal = candidates.read_candidate(candidate_id)
        self.save_champion(candidates.candidate_snapshot(proposal))
        self.path.write_text(self.path.read_text() + "External modification\n")
        for manual in (False, True):
            result = candidates.apply_candidate(candidate_id, manual_review=manual)
            self.assertIn("active Skill changed", result["error"])
        self.assertEqual(candidates.read_candidate(candidate_id)["status"], "pending")


if __name__ == "__main__":
    unittest.main()
