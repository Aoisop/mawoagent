"""Isolated online Skill proposals and explicit publish/rollback operations."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import uuid

from .skill_evolution import get_evolution_dir, resolve_skill_file, _safe_skill_slug
from .storage import atomic_write_text, state_lock, write_json


def _hash_file(path: Path | None) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None


def _path(candidate_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{24}", candidate_id):
        raise ValueError("invalid candidate id")
    return get_evolution_dir() / "candidates" / f"{candidate_id}.json"


def stage_candidate(action: str, write_input: dict) -> dict:
    from .skills import get_skill_by_name
    name = write_input.get("skill_name") or write_input.get("name")
    skill = get_skill_by_name(name)
    base = Path(skill.skill_dir) / "SKILL.md" if skill else resolve_skill_file(name)
    candidate_id = uuid.uuid4().hex[:24]
    proposal = {"id": candidate_id, "status": "pending", "action": action, "skill": name,
                "write_input": write_input, "base_hash": _hash_file(base)}
    _validate_candidate(proposal, candidate_id)
    _freeze_snapshot(proposal)
    write_json(_path(candidate_id), proposal)
    return {"ok": True, "action": action + "_staged", "skill": name,
            "candidate_id": candidate_id, "file": str(_path(candidate_id))}


def list_candidates() -> list[dict]:
    root = get_evolution_dir() / "candidates"
    return [read_candidate(path.stem) for path in sorted(root.glob("*.json"))]


def read_candidate(candidate_id: str) -> dict:
    path = _path(candidate_id)
    with state_lock(path):
        candidate = json.loads(path.read_text(encoding="utf-8"))
        _validate_candidate(candidate, candidate_id)
        if "snapshot" not in candidate and "snapshot_date" not in candidate:
            # Old proposals have no reliable creation/evaluation date. Freeze on
            # first use; their old champion cannot authorize this new snapshot.
            _freeze_snapshot(candidate)
            write_json(path, candidate)
        candidate_snapshot(candidate)
        return candidate


def _validate_candidate(candidate: dict, candidate_id: str) -> None:
    if (not isinstance(candidate, dict) or candidate.get("id") != candidate_id
            or candidate.get("action") not in {"add", "merge"}
            or candidate.get("status") not in {"pending", "applied"}
            or not isinstance(candidate.get("write_input"), dict)
            or not isinstance(candidate.get("skill"), str) or "base_hash" not in candidate
            or not (candidate["base_hash"] is None or isinstance(candidate["base_hash"], str))):
        raise ValueError("invalid Skill candidate")
    inp = candidate["write_input"]
    if (not isinstance(inp.get("instructions"), str) or not inp["instructions"].strip()
            or (inp.get("skill_name") or inp.get("name")) != candidate["skill"]):
        raise ValueError("invalid candidate instructions or identity")
    from .tools import tool_definitions
    from .validation import validate_json
    from copy import deepcopy
    tool = "skill_evolve" if candidate["action"] == "merge" else "skill_create"
    schema = deepcopy(next(item["input_schema"] for item in tool_definitions if item["name"] == tool))
    schema["properties"].update({"instructions": {"type": "string"}, "description": {"type": "string"},
        "when_to_use": {"type": "string"}, "tags": {"type": ["array", "null"], "items": {"type": "string"}},
        "actor": {"type": "string"}, "preserve_existing": {"type": "boolean"}})
    schema["properties"].pop("actor" if tool == "skill_evolve" else "preserve_existing", None)
    schema["additionalProperties"] = False
    error = validate_json(inp, schema)
    if error:
        raise ValueError(f"invalid candidate parameters: {error}")


def _freeze_snapshot(proposal: dict) -> None:
    from .skill_evolution import _today
    proposal["snapshot_date"] = _today()
    proposal["snapshot"] = _render_snapshot(proposal)


def _render_snapshot(proposal: dict) -> dict:
    from .skill_evolution import _skill_body, _append_evolution_note
    inp = proposal["write_input"]
    body = _skill_body(inp.get("instructions", ""), inp.get("evidence", "") if proposal["action"] == "add" else "")
    if proposal["action"] == "merge":
        note = _append_evolution_note("", inp.get("lesson", ""), inp.get("rationale", ""),
                                      evolved_on=proposal["snapshot_date"]).strip()
        if note:
            body = body.rstrip() + "\n\n" + note + "\n"
    identity = {key: proposal[key] for key in ("action", "skill", "write_input", "base_hash", "snapshot_date")}
    proposal_hash = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {"name": proposal["skill"], "description": inp.get("description", ""),
            "instructions": body.strip(), "candidate_id": proposal["id"], "proposal_hash": proposal_hash}


def candidate_snapshot(proposal: dict) -> dict:
    """Return a validated, detached copy of the staged evaluation input."""
    try:
        date.fromisoformat(proposal["snapshot_date"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid candidate snapshot date; reload or regenerate the candidate") from None
    snapshot = proposal.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot != _render_snapshot(proposal):
        raise ValueError("candidate parameters or snapshot changed; regenerate the candidate")
    return dict(snapshot)


def apply_candidate(candidate_id: str, *, manual_review: bool = False, expected_hash: str | None = None) -> dict:
    """Caller must authorize the concrete create/evolve operation first."""
    from .skills import get_skill_by_name, reset_skill_cache
    from .skill_evolution import create_skill_file, evolve_skill_file
    path = _path(candidate_id)
    with state_lock(path):
        candidate = read_candidate(candidate_id)
        if expected_hash is not None and hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest() != expected_hash:
            return {"ok": False, "error": "candidate changed during approval"}
        if candidate["status"] != "pending":
            return {"ok": False, "error": "candidate is no longer pending"}
        snapshot = candidate_snapshot(candidate)
        registry_path = get_evolution_dir() / "online-eval" / "champions.json"
        with state_lock(registry_path) if not manual_review else nullcontext():
            if not manual_review:
                from .online_skill_eval import _lineage_id_for_skill
                registry = get_evolution_dir() / "online-eval" / "champions.json"
                champion = (json.loads(registry.read_text()).get("champions", {}).get(_lineage_id_for_skill(candidate["skill"]), {})
                            if registry.exists() else {})
                if champion.get("snapshot") != snapshot:
                    return {"ok": False, "error": "candidate has no matching passed evaluation; evaluate or explicitly use --manual-review"}
            skill = get_skill_by_name(candidate["skill"])
            base = Path(skill.skill_dir) / "SKILL.md" if skill else resolve_skill_file(candidate["skill"])
            lock_path = base or (get_evolution_dir() / "candidate-publish")
            with state_lock(lock_path):
                if _hash_file(base) != candidate["base_hash"]:
                    return {"ok": False, "error": "active Skill changed since proposal; regenerate/review the candidate"}
                write_input = dict(candidate["write_input"], _prepared_body=snapshot["instructions"] + "\n")
                if candidate["action"] == "merge":
                    result = evolve_skill_file(**write_input, active_dir=skill.skill_dir if skill else "")
                else:
                    result = create_skill_file(**write_input)
                if result.get("ok"):
                    reset_skill_cache()
                    candidate["status"] = "applied"
                    write_json(path, candidate)
                return result


def rollback_skill(name: str) -> dict:
    """Restore the most recent pre-change snapshot after caller authorization."""
    from .skills import get_skill_by_name, reset_skill_cache
    skill = get_skill_by_name(name)
    target = resolve_skill_file(name, active_dir=skill.skill_dir if skill else "")
    if target is None:
        return {"ok": False, "error": "Skill not found"}
    history = get_evolution_dir() / "history" / f"{_safe_skill_slug(name)}.jsonl"
    if not history.is_file():
        return {"ok": False, "error": "No pre-change snapshot found"}
    with state_lock(target), state_lock(history):
        rows = [json.loads(line) for line in history.read_text().splitlines() if line.strip()]
        snapshot = next((row for row in reversed(rows) if row.get("event") == "snapshot"
                         and row.get("file") == str(target) and isinstance(row.get("content"), str)), None)
        if snapshot is None:
            return {"ok": False, "error": "No matching snapshot found"}
        # Save the current version too, making an explicit rollback reversible.
        from .storage import append_jsonl
        append_jsonl(history, {"event": "snapshot", "actor": "rollback", "skill": name,
                              "file": str(target), "content": target.read_text()})
        atomic_write_text(target, snapshot["content"])
        reset_skill_cache()
        return {"ok": True, "skill": name, "file": str(target)}
