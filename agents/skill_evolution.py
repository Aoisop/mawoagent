from __future__ import annotations

import json
import os
import re
import shutil
import time
import hashlib
from pathlib import Path
from typing import Any

from .frontmatter import format_frontmatter, parse_frontmatter
from .storage import append_jsonl, atomic_write_text, state_lock, write_json


USAGE_LOG = "usage.jsonl"
ONLINE_PROVENANCE_LOG = "online_provenance.jsonl"
ONLINE_PROVENANCE_INDEX = "online_skill_provenance.json"
SKILL_USAGE_STATS = "skill_usage_stats.json"
HISTORY_DIR = "history"


def get_evolution_dir() -> Path:
    from .workspace import project_root

    return project_root() / ".mawoagent" / "skill-evolution"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    append_jsonl(path, row)


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    # Never turn corrupted counters or provenance into a new, empty history.
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    write_json(path, value)


def _safe_skill_slug(name: str) -> str:
    raw = str(name or "").strip()
    slug = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+", "-", raw)
    slug = re.sub(r"-{2,}", "-", slug).strip("-.")
    if slug:
        return slug[:120].rstrip("-.") or slug[:120]
    if raw:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        return f"skill-{digest}"
    return "unknown"


def _preview(value: object, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _compact_messages(messages: list[dict[str, Any]], *, max_messages: int = 12, max_chars: int = 4000) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in list(messages or [])[-max_messages:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        out.append({"role": role, "content": _preview(content, max_chars)})
    return out


def record_skill_invocation(
    *,
    skill_name: str,
    source: str,
    context: str,
    args: object = "",
) -> None:
    row = {
        "event": "invoke",
        "time": _utc_now(),
        "skill": skill_name,
        "source": source,
        "context": context,
        "args_preview": _preview(args),
    }
    _append_jsonl(get_evolution_dir() / USAGE_LOG, row)


def record_skill_feedback(
    *,
    skill_name: str,
    rating: str,
    note: str = "",
) -> None:
    row = {
        "event": "feedback",
        "time": _utc_now(),
        "skill": skill_name,
        "rating": str(rating or "").strip(),
        "note": _preview(note, 1200),
    }
    _append_jsonl(get_evolution_dir() / USAGE_LOG, row)


def record_online_skill_provenance(
    *,
    action: str,
    skill_name: str = "",
    result: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    retrieved_reference: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    error: str = "",
    replay_context: dict[str, Any] | None = None,
) -> None:
    row = {
        "event": "online_ingest",
        "replay_context": dict(replay_context or {}),
        "time": _utc_now(),
        "action": str(action or "none").strip() or "none",
        "skill": str(skill_name or "").strip(),
        "ok": bool((result or {}).get("ok")) if result is not None else not bool(error),
        "result": result or {},
        "messages": _compact_messages(list(messages or [])),
        "retrieved_reference": retrieved_reference or {},
        "decision": decision or {},
        "error": _preview(error, 1200),
    }
    _append_jsonl(get_evolution_dir() / ONLINE_PROVENANCE_LOG, row)
    _update_online_provenance_index(row)


def _update_online_provenance_index(row: dict[str, Any]) -> None:
    skill = str(row.get("skill") or "").strip()
    if not skill:
        return
    root = get_evolution_dir()
    path = root / ONLINE_PROVENANCE_INDEX
    with state_lock(path):
        index = _read_json(path, {})
        item = index.setdefault(
            skill,
            {
                "skill": skill,
                "source_count": 0,
                "history_count": 0,
                "sources": [],
                "version_timeline": [],
                "usage": {},
            },
        )
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        item["current_version"] = result.get("version") or item.get("current_version", "")
        item["last_action"] = row.get("action")
        item["last_time"] = row.get("time")
        item["last_ok"] = row.get("ok")
        item["last_error"] = row.get("error", "")
        item["source_count"] = int(item.get("source_count", 0)) + 1
        source = {
            "replay_context": dict(row.get("replay_context") or {}),
            "time": row.get("time"),
            "action": row.get("action"),
            "ok": row.get("ok"),
            "messages": row.get("messages", []),
            "retrieved_reference": row.get("retrieved_reference", {}),
            "decision": row.get("decision", {}),
            "error": row.get("error", ""),
        }
        sources = list(item.get("sources") or [])
        sources.append(source)
        item["sources"] = sources[-20:]
        item["history_count"] = len(sources)
        if result.get("version"):
            timeline = list(item.get("version_timeline") or [])
            timeline.append({"time": row.get("time"), "version": result.get("version"), "action": row.get("action")})
            item["version_timeline"] = timeline[-50:]
        index[skill] = item
        _write_json(path, index)


def _parse_int(value: str | None, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except Exception:
        return default


def _bump_patch(version: str | None) -> str:
    raw = str(version or "0.1.0").strip()
    parts = raw.split(".")
    if len(parts) < 3 or not all(p.isdigit() for p in parts[:3]):
        return "0.1.1"
    parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts[:3])


def _find_skill_file_by_name(base_dir: Path, skill_name: str) -> Path | None:
    if not base_dir.is_dir():
        return None
    wanted = str(skill_name or "").strip()
    for entry in base_dir.iterdir():
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            continue
        try:
            parsed = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = parsed.meta.get("name") or entry.name
        if name == wanted:
            return skill_file
    return None


def resolve_skill_file(skill_name: str, *, target: str = "active", active_dir: str = "") -> Path | None:
    target = (target or "active").strip().lower()
    if target == "active" and active_dir:
        skill_file = Path(active_dir) / "SKILL.md"
        if skill_file.is_file():
            return skill_file

    if target in ("project", "active"):
        found = _find_skill_file_by_name(_skills_root("project"), skill_name)
        if found:
            return found

    if target in ("user", "active"):
        found = _find_skill_file_by_name(Path.home() / ".mawoagent" / "skills", skill_name)
        if found:
            return found

    return None


def _skills_root(target: str) -> Path:
    from .workspace import project_root

    target = (target or "project").strip().lower()
    if target == "user":
        return Path.home() / ".mawoagent" / "skills"
    return project_root() / ".mawoagent" / "skills"


def _normalize_context(context: str | None) -> str:
    return "fork" if str(context or "").strip().lower() == "fork" else "inline"


def _normalize_allowed_tools(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return ",".join(part.strip() for part in str(value).split(",") if part.strip())


def _skill_body(instructions: str, evidence: str = "") -> str:
    body = str(instructions or "").strip()
    if not body:
        body = (
            "# Goal\n\n"
            "Apply this reusable skill when the user's request matches the trigger.\n\n"
            "# Workflow\n\n"
            "1. Identify the user's concrete task and constraints.\n"
            "2. Follow the reusable rules captured in this skill.\n"
            "3. Produce the requested output directly.\n"
        )
    if not body.lstrip().startswith("#"):
        body = "# Skill Instructions\n\n" + body
    if evidence:
        body = body.rstrip() + "\n\n## Creation Evidence\n\n" + str(evidence).strip() + "\n"
    return body.rstrip() + "\n"


def create_skill_file(
    *,
    name: str,
    description: str,
    instructions: str,
    when_to_use: str = "",
    target: str = "project",
    context: str = "inline",
    user_invocable: bool = False,
    allowed_tools: object = None,
    evidence: str = "",
    actor: str = "agent",
    tags: list[str] | None = None,
    _prepared_body: str | None = None,
) -> dict[str, Any]:
    resolved_name = str(name or "").strip()
    if not resolved_name:
        return {"ok": False, "error": "skill name is required"}
    if not str(description or "").strip():
        return {"ok": False, "error": "description is required"}

    existing = resolve_skill_file(resolved_name, target="active")
    if existing:
        return {"ok": False, "error": f"Skill already exists: {resolved_name}", "file": str(existing)}

    root = _skills_root(target)
    skill_dir = root / _safe_skill_slug(resolved_name)
    skill_file = skill_dir / "SKILL.md"
    if skill_file.exists():
        return {"ok": False, "error": f"Skill file already exists: {skill_file}"}

    meta = {
        "name": resolved_name,
        "description": re.sub(r"\s+", " ", str(description).strip()),
        "version": "0.1.0",
        "created-at": _utc_now(),
        "user-invocable": "true" if user_invocable else "false",
        "context": _normalize_context(context),
    }
    if when_to_use:
        meta["when-to-use"] = re.sub(r"\s+", " ", str(when_to_use).strip())
    if tags:
        meta["tags"] = ",".join(re.sub(r"\s+", "-", str(tag).strip()) for tag in tags if str(tag).strip())
    tools = _normalize_allowed_tools(allowed_tools)
    if tools:
        meta["allowed-tools"] = tools

    skill_dir.mkdir(parents=True, exist_ok=False)
    # Candidate publication supplies the exact body used during evaluation.
    body = _skill_body(instructions, evidence) if _prepared_body is None else _prepared_body
    atomic_write_text(skill_file, format_frontmatter(meta, body))

    event = {
        "event": "create",
        "time": _utc_now(),
        "actor": actor,
        "skill": resolved_name,
        "file": str(skill_file),
        "target": "user" if (target or "").strip().lower() == "user" else "project",
        "description": _preview(description, 1200),
        "when_to_use": _preview(when_to_use, 1200),
        "evidence": _preview(evidence, 1200),
    }
    _append_jsonl(get_evolution_dir() / USAGE_LOG, event)
    return {"ok": True, **event}


def _append_evolution_note(body: str, lesson: str, rationale: str = "", *, evolved_on: str | None = None) -> str:
    lesson = re.sub(r"\s+", " ", str(lesson or "").strip())
    rationale = re.sub(r"\s+", " ", str(rationale or "").strip())
    if not lesson:
        raise ValueError("lesson is required")

    bullet = f"- {evolved_on if evolved_on is not None else _today()}: {lesson}"
    if rationale:
        bullet += f" Reason: {rationale}"

    body = str(body or "").rstrip()
    marker = "## Evolution Notes"
    if marker in body:
        if lesson in body:
            return body + "\n"
        return body + "\n" + bullet + "\n"
    return body + "\n\n" + marker + "\n\n" + bullet + "\n"


def evolve_skill_file(
    *,
    skill_name: str,
    lesson: str,
    rationale: str = "",
    target: str = "active",
    active_dir: str = "",
    actor: str = "agent",
    instructions: str = "",
    description: str = "",
    when_to_use: str = "",
    tags: list[str] | None = None,
    preserve_existing: bool = False,
    _prepared_body: str | None = None,
) -> dict[str, Any]:
    skill_file = resolve_skill_file(skill_name, target=target, active_dir=active_dir)
    if not skill_file:
        return {"ok": False, "error": f"Skill not found: {skill_name}"}

    with state_lock(skill_file):
        raw = skill_file.read_text(encoding="utf-8")
        parsed = parse_frontmatter(raw)
        meta = dict(parsed.meta)
        resolved_name = meta.get("name") or skill_file.parent.name

        snapshot = {
            "time": _utc_now(),
            "event": "snapshot",
            "actor": actor,
            "skill": resolved_name,
            "file": str(skill_file),
            "version": meta.get("version", "0.1.0"),
            "lesson": _preview(lesson, 1200),
            "rationale": _preview(rationale, 1200),
            "content": raw,
        }
        history_path = get_evolution_dir() / HISTORY_DIR / f"{_safe_skill_slug(resolved_name)}.jsonl"
        _append_jsonl(history_path, snapshot)

        meta["name"] = resolved_name
        meta["version"] = _bump_patch(meta.get("version"))
        meta["last-evolved"] = _utc_now()
        meta["evolution-count"] = str(_parse_int(meta.get("evolution-count"), 0) + 1)
        if description.strip():
            meta["description"] = re.sub(r"\s+", " ", description.strip())
        if when_to_use.strip():
            meta["when-to-use"] = re.sub(r"\s+", " ", when_to_use.strip())
        if tags:
            existing_tags = [part.strip() for part in str(meta.get("tags") or "").split(",") if part.strip()]
            merged_tags = existing_tags[:]
            for tag in tags:
                normalized = re.sub(r"\s+", "-", str(tag).strip())
                if normalized and normalized not in merged_tags:
                    merged_tags.append(normalized)
            if merged_tags:
                meta["tags"] = ",".join(merged_tags[:12])

        if _prepared_body is not None:
            new_body = _prepared_body
        elif instructions.strip():
            new_body = _skill_body(instructions.strip())
            # The maintainer supplies a complete merged body. Preserve previous
            # instructions in the snapshot, not as conflicting active guidance.
            note = _append_evolution_note("", lesson, rationale).strip()
            if note:
                new_body = new_body.rstrip() + "\n\n" + note + "\n"
        else:
            new_body = _append_evolution_note(parsed.body, lesson, rationale)
        atomic_write_text(skill_file, format_frontmatter(meta, new_body))

        event = {
            "event": "evolve",
            "time": _utc_now(),
            "actor": actor,
            "skill": resolved_name,
            "file": str(skill_file),
            "version": meta["version"],
            "target": target,
            "lesson": _preview(lesson, 1200),
            "rationale": _preview(rationale, 1200),
            "history": str(history_path),
        }
        _append_jsonl(get_evolution_dir() / USAGE_LOG, event)
        return {"ok": True, **event}


def record_skill_usage_judgments(judgments: list[dict[str, Any]], *, allow_prune: bool = False) -> dict[str, Any]:
    if any(type(item.get("relevant")) is not bool or type(item.get("used")) is not bool
           or (item["used"] and not item["relevant"]) for item in judgments):
        raise ValueError("Skill usage judgments require consistent boolean values")
    stats_path = get_evolution_dir() / SKILL_USAGE_STATS
    with state_lock(stats_path):
        stats = _read_json(stats_path, {})
        pruned: list[str] = []
        for judgment in judgments:
            skill = str(judgment.get("name") or judgment.get("skill") or "").strip()
            if not skill:
                continue
            item = stats.setdefault(
                skill,
                {
                    "retrieved": 0,
                    "relevant": 0,
                    "used": 0,
                    "last_retrieved": "",
                    "last_used": "",
                    "source": judgment.get("source", ""),
                    "skill_dir": judgment.get("skill_dir", ""),
                },
            )
            item["retrieved"] = int(item.get("retrieved", 0)) + 1
            item["last_retrieved"] = _utc_now()
            item["source"] = judgment.get("source", item.get("source", ""))
            item["skill_dir"] = judgment.get("skill_dir", item.get("skill_dir", ""))
            if judgment.get("relevant"):
                item["relevant"] = int(item.get("relevant", 0)) + 1
            if judgment.get("used"):
                item["used"] = int(item.get("used", 0)) + 1
                item["last_used"] = _utc_now()
            item["last_reason"] = _preview(judgment.get("reason", ""), 500)
            item["last_score"] = judgment.get("score", 0)
            if allow_prune and _maybe_prune_stale_skill(skill, item):
                pruned.append(skill)
        _write_json(stats_path, stats)
        _sync_usage_into_provenance(stats)
        return {"ok": True, "judgments": len(judgments), "pruned": pruned}


def restore_pruned_skill(skill_name: str) -> dict[str, Any]:
    """Restore an archive after the caller has authorized this file operation."""
    stats_path = get_evolution_dir() / SKILL_USAGE_STATS
    with state_lock(stats_path):
        stats = _read_json(stats_path, {})
        item = stats.get(skill_name, {})
        if not item.get("pruned") or not item.get("pruned_to") or not item.get("skill_dir"):
            return {"ok": False, "error": "No archived skill found"}
        archive = Path(item["pruned_to"]).resolve()
        original = Path(item["skill_dir"]).resolve()
        archive_root = (get_evolution_dir() / "pruned").resolve()
        source_root = _skills_root(item.get("source", "project")).resolve()
        if archive.parent != archive_root or original.parent != source_root:
            return {"ok": False, "error": "Archive paths are outside the expected skill directories"}
        if original.exists() or not (archive / "SKILL.md").is_file():
            return {"ok": False, "error": "Original path is occupied or archive is missing"}
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(archive), str(original))
        item.update(pruned=False, pruned_to="", retrieved=0, relevant=0, used=0,
                    last_retrieved="", last_used="")
        _write_json(stats_path, stats)
        _append_jsonl(get_evolution_dir() / USAGE_LOG,
                      {"event": "restore", "time": _utc_now(), "skill": skill_name,
                       "from": str(archive), "to": str(original)})
        _sync_usage_into_provenance(stats)
        return {"ok": True, "skill": skill_name, "file": str(original / "SKILL.md")}


def _maybe_prune_stale_skill(skill_name: str, stats: dict[str, Any]) -> bool:
    min_retrieved = _parse_int(os.environ.get("MAWOAGENT_SKILL_USAGE_PRUNE_MIN_RETRIEVED"), 40)
    max_used = _parse_int(os.environ.get("MAWOAGENT_SKILL_USAGE_PRUNE_MAX_USED"), 0)
    source = str(stats.get("source") or "").strip().lower()
    if source != "user" and os.environ.get("MAWOAGENT_SKILL_PRUNE_PROJECT", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    if int(stats.get("retrieved", 0)) < min_retrieved:
        return False
    if int(stats.get("used", 0)) > max_used:
        return False
    skill_dir = Path(str(stats.get("skill_dir") or ""))
    if not skill_dir.is_dir():
        return False
    if skill_dir.name.startswith("."):
        return False
    archive_root = get_evolution_dir() / "pruned"
    archive_root.mkdir(parents=True, exist_ok=True)
    destination = archive_root / f"{_safe_skill_slug(skill_name)}-{int(time.time())}"
    try:
        shutil.move(str(skill_dir), str(destination))
    except Exception:
        return False
    event = {
        "event": "prune",
        "time": _utc_now(),
        "skill": skill_name,
        "from": str(skill_dir),
        "to": str(destination),
        "retrieved": stats.get("retrieved", 0),
        "used": stats.get("used", 0),
    }
    _append_jsonl(get_evolution_dir() / USAGE_LOG, event)
    stats["pruned"] = True
    stats["pruned_to"] = str(destination)
    return True


def _sync_usage_into_provenance(stats: dict[str, Any]) -> None:
    path = get_evolution_dir() / ONLINE_PROVENANCE_INDEX
    with state_lock(path):
        index = _read_json(path, {})
        if not isinstance(index, dict):
            return
        changed = False
        for skill, usage in stats.items():
            if skill in index:
                index[skill]["usage"] = usage
                changed = True
        if changed:
            _write_json(path, index)


def load_skill_stats() -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    usage_path = get_evolution_dir() / USAGE_LOG
    if usage_path.is_file():
        for line in usage_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            skill = str(row.get("skill") or "").strip()
            if not skill:
                continue
            item = stats.setdefault(skill, {"created": 0, "invocations": 0, "feedback": 0, "evolutions": 0})
            event = row.get("event")
            if event == "create":
                item["created"] = int(item.get("created", 0)) + 1
                item["created_at"] = row.get("time")
                item["file"] = row.get("file")
            elif event == "invoke":
                item["invocations"] = int(item.get("invocations", 0)) + 1
                item["last_invoked"] = row.get("time")
            elif event == "feedback":
                item["feedback"] = int(item.get("feedback", 0)) + 1
                item["last_feedback"] = row.get("time")
            elif event == "evolve":
                item["evolutions"] = int(item.get("evolutions", 0)) + 1
                item["last_evolved"] = row.get("time")
                item["version"] = row.get("version")
                item["file"] = row.get("file")

    history_root = get_evolution_dir() / HISTORY_DIR
    if history_root.is_dir():
        for path in history_root.glob("*.jsonl"):
            count = len([line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()])
            skill = path.stem
            item = stats.setdefault(skill, {"created": 0, "invocations": 0, "feedback": 0, "evolutions": 0})
            item["snapshots"] = count
    usage_stats = _read_json(get_evolution_dir() / SKILL_USAGE_STATS, {})
    if isinstance(usage_stats, dict):
        for skill, usage in usage_stats.items():
            if not isinstance(usage, dict):
                continue
            item = stats.setdefault(str(skill), {"created": 0, "invocations": 0, "feedback": 0, "evolutions": 0})
            item["retrieved"] = usage.get("retrieved", 0)
            item["relevant"] = usage.get("relevant", 0)
            item["used"] = usage.get("used", 0)
            item["last_retrieved"] = usage.get("last_retrieved", "")
            item["last_used"] = usage.get("last_used", "")
            item["pruned"] = usage.get("pruned", False)
    return stats


def format_skill_stats() -> str:
    stats = load_skill_stats()
    if not stats:
        return "No skill evolution events recorded yet."

    lines = ["Skill evolution stats:"]
    for name in sorted(stats):
        item = stats[name]
        parts = [
            f"created={item.get('created', 0)}",
            f"invoked={item.get('invocations', 0)}",
            f"feedback={item.get('feedback', 0)}",
            f"evolved={item.get('evolutions', 0)}",
            f"snapshots={item.get('snapshots', 0)}",
            f"retrieved={item.get('retrieved', 0)}",
            f"used={item.get('used', 0)}",
        ]
        if item.get("created_at"):
            parts.append(f"created_at={item['created_at']}")
        if item.get("version"):
            parts.append(f"version={item['version']}")
        if item.get("last_invoked"):
            parts.append(f"last_invoked={item['last_invoked']}")
        if item.get("last_used"):
            parts.append(f"last_used={item['last_used']}")
        if item.get("pruned"):
            parts.append("pruned=true")
        lines.append(f"  {name}: " + ", ".join(parts))
    return "\n".join(lines)
