"""Paired, per-question evaluation of mawoagent's full session folding."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import unicodedata
from typing import Any

from .agent import Agent, AUTO_COMPACT_THRESHOLD
from .config import load_env_file as _load_env_file, resolve_api_config as _resolve_api_config
from .tools import tool_definitions
from .storage import write_json


READ_ONLY_TOOLS = {"read_file", "list_files", "grep_search", "compact_context"}
AVAILABLE_CASE_TOOLS = READ_ONLY_TOOLS - {"compact_context"}
TEXT_ATTACHMENTS = {".txt", ".md", ".csv", ".json", ".jsonl", ".xml", ".html", ".py"}
FINAL_ANSWER = re.compile(r"(?im)^\s*FINAL_ANSWER\s*[:：]\s*(.*?)\s*$")
DECIMAL_ANSWER = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?$")


class EvaluationBudgetExceeded(Exception):
    pass


class EvaluationOutputTruncated(Exception):
    pass


def _write_json(path: Path, data: Any) -> None:
    write_json(path, data)


def load_cases(path: Path, *, text_only: bool = False, limit: int | None = None) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    else:
        rows = json.loads(raw)
        if isinstance(rows, dict):
            rows = rows.get("cases")
    if not isinstance(rows, list):
        raise ValueError("cases file must contain a JSON array or JSONL objects")

    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"case {index + 1} is not an object")
        if text_only and (row.get("problem_type") not in (None, "text") or row.get("image") or row.get("file_name")):
            continue
        case_id = str(row.get("task_id") or row.get("id") or "").strip()
        if not case_id or case_id in seen:
            raise ValueError(f"case {index + 1} needs a unique task_id or id")
        seen.add(case_id)
        prompts = row.get("prompts")
        if prompts is None:
            prompts = [row.get("prompt") or row.get("Question") or row.get("question")]
        if not isinstance(prompts, list) or not prompts or any(not isinstance(item, str) or not item.strip() for item in prompts):
            raise ValueError(f"case {case_id} needs a non-empty prompt or prompts list")
        answers = row.get("answers", row.get("answer"))
        if not isinstance(answers, list):
            answers = [answers]
        if not answers or any(answer is None or str(answer).strip() == "" for answer in answers):
            raise ValueError(f"case {case_id} needs answer or answers")

        required_tools = row.get("required_tools", [])
        if not isinstance(required_tools, list) or any(not isinstance(tool, str) for tool in required_tools):
            raise ValueError(f"case {case_id} required_tools must be a list of tool names")
        missing_tools = sorted(set(required_tools) - AVAILABLE_CASE_TOOLS)
        if missing_tools:
            raise ValueError(f"case {case_id} needs unavailable tools: {', '.join(missing_tools)}")

        file_name = str(row.get("file_name") or "").strip()
        attachment_path: Path | None = None
        if file_name:
            files_dir = (path.parent / "files").resolve()
            attachment = (files_dir / file_name).resolve()
            if files_dir not in attachment.parents:
                raise ValueError(f"case {case_id} attachment must be inside {files_dir}")
            if not attachment.is_file():
                raise ValueError(f"case {case_id} attachment missing: {attachment}")
            if attachment.suffix.lower() not in TEXT_ATTACHMENTS:
                raise ValueError(f"case {case_id} attachment type {attachment.suffix or '(none)'} is not readable by this evaluation profile")
            attachment_path = attachment
            prompts = list(prompts)
            prompts[-1] += f"\n\nAttached file: {attachment.resolve()}"
        if row.get("image") or row.get("problem_type") == "mm":
            raise ValueError(f"case {case_id} needs image input, which this evaluation profile cannot provide")
        cases.append({
            "task_id": case_id,
            "prompts": prompts,
            "answers": [str(answer).strip() for answer in answers],
            "source_type": str(row.get("problem_type") or "custom"),
            "required_tools": required_tools,
            "capabilities_verified": "required_tools" in row or row.get("problem_type") is None,
            "attachment_path": str(attachment_path) if attachment_path else None,
        })
        if limit is not None and len(cases) >= limit:
            break
    if not cases:
        raise ValueError("no cases selected")
    return cases


def extract_answer(output: str) -> str:
    matches = FINAL_ANSWER.findall(output or "")
    return (matches[-1] if matches else str(output or "")).strip()


def _normalized_answer(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


def _numeric_answer(text: str) -> Decimal | None:
    value = unicodedata.normalize("NFKC", str(text)).strip()
    if not DECIMAL_ANSWER.fullmatch(value):
        return None
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None


def score_details(prediction: str, answers: list[str], *, mode: str) -> dict[str, Any]:
    if mode not in {"exact", "normalized", "numeric"}:
        raise ValueError(f"unsupported score mode: {mode}")
    if mode == "numeric":
        normalized_prediction = _numeric_answer(prediction)
        normalized_answers = [_numeric_answer(answer) for answer in answers]
    else:
        normalize = (lambda text: str(text).strip()) if mode == "exact" else _normalized_answer
        normalized_prediction = normalize(prediction)
        normalized_answers = [normalize(answer) for answer in answers]
    matched = next((answer for answer, value in zip(answers, normalized_answers)
                    if normalized_prediction is not None and normalized_prediction != "" and value == normalized_prediction), None)
    return {
        "correct": matched is not None,
        "matched_answer": matched,
        "normalized_prediction": str(normalized_prediction) if normalized_prediction is not None else None,
        "reason": "matched" if matched is not None else "invalid_numeric_prediction" if mode == "numeric" and normalized_prediction is None else "no_match",
        "review_required": matched is None,
    }


def score_answer(prediction: str, answers: list[str], *, mode: str) -> bool:
    return score_details(prediction, answers, mode=mode)["correct"]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise ValueError("no evaluation results")
    paired: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        pair = paired.setdefault(row["task_id"], {})
        if row["condition"] in pair:
            raise ValueError(f"duplicate result: {row['task_id']} {row['condition']}")
        pair[row["condition"]] = row
    counts = {"both_correct": 0, "fold_only": 0, "no_fold_only": 0, "both_wrong": 0}
    valid_pairs: list[dict[str, dict[str, Any]]] = []
    invalid_pairs: list[dict[str, Any]] = []
    for task_id, pair in paired.items():
        if set(pair) != {"fold_on", "fold_off"}:
            raise ValueError(f"incomplete pair: {task_id}")
        reasons = []
        for condition in ("fold_on", "fold_off"):
            if pair[condition]["status"] != "completed":
                reasons.append(f"{condition}:{pair[condition]['status']}")
        if int(pair["fold_on"]["fold_count"]) < 1:
            reasons.append("fold_on:no_fold")
        if int(pair["fold_off"]["fold_count"]) > 0:
            reasons.append("fold_off:unexpected_fold")
        if not pair["fold_on"].get("capabilities_verified", True):
            reasons.append("case:capabilities_unverified")
        if reasons:
            invalid_pairs.append({"task_id": task_id, "reasons": reasons})
            continue
        valid_pairs.append(pair)
        on = bool(pair["fold_on"]["correct"])
        off = bool(pair["fold_off"]["correct"])
        bucket = "both_correct" if on and off else "fold_only" if on else "no_fold_only" if off else "both_wrong"
        counts[bucket] += 1
    n = len(valid_pairs)
    def condition_totals(condition: str, correct: int) -> dict[str, Any]:
        selected = [pair[condition] for pair in valid_pairs]
        return {
            "correct": correct,
            "accuracy": correct / n if n else None,
            "total_folds": sum(int(row["fold_count"]) for row in selected),
            "main_input_tokens": sum(int(row["main_input_tokens"]) for row in selected),
            "main_output_tokens": sum(int(row["main_output_tokens"]) for row in selected),
            "side_input_tokens": sum(int(row["side_input_tokens"]) for row in selected),
            "side_output_tokens": sum(int(row["side_output_tokens"]) for row in selected),
            "estimated_cost_usd": round(sum(float(row.get("estimated_cost_usd", 0)) for row in selected), 6),
            "elapsed_seconds": round(sum(float(row.get("elapsed_seconds", 0)) for row in selected), 3),
            "statuses": {status: sum(row["status"] == status for row in selected) for status in sorted({row["status"] for row in selected})},
        }
    return {
        "cases": len(paired),
        "valid_pairs": n,
        "invalid_pairs": invalid_pairs,
        "attempt_statuses": {
            condition: {status: sum(row["condition"] == condition and row["status"] == status for row in rows)
                        for status in sorted({row["status"] for row in rows if row["condition"] == condition})}
            for condition in ("fold_on", "fold_off")
        },
        "attempted_usage": {
            condition: {
                "main_input_tokens": sum(int(row.get("main_input_tokens", 0)) for row in rows if row["condition"] == condition),
                "main_output_tokens": sum(int(row.get("main_output_tokens", 0)) for row in rows if row["condition"] == condition),
                "side_input_tokens": sum(int(row.get("side_input_tokens", 0)) for row in rows if row["condition"] == condition),
                "side_output_tokens": sum(int(row.get("side_output_tokens", 0)) for row in rows if row["condition"] == condition),
                "estimated_cost_usd": round(sum(float(row.get("estimated_cost_usd", 0)) for row in rows if row["condition"] == condition), 6),
            }
            for condition in ("fold_on", "fold_off")
        },
        "paired_outcomes": counts,
        "cases_with_fold": sum(1 for pair in paired.values() if int(pair["fold_on"]["fold_count"]) > 0),
        "fold_off_unexpected_folds": sum(int(pair["fold_off"]["fold_count"]) for pair in paired.values()),
        "note": "Accuracy and delta use valid pairs only; they are null if no pair completed with folding only in fold_on.",
        "fold_on": condition_totals("fold_on", counts["both_correct"] + counts["fold_only"]),
        "fold_off": condition_totals("fold_off", counts["both_correct"] + counts["no_fold_only"]),
        "accuracy_delta_percentage_points": 100 * (counts["fold_only"] - counts["no_fold_only"]) / n if n else None,
    }


async def _run_case(
    case: dict[str, Any], condition: str, *, output_dir: Path, model: str,
    api_base: str | None, api_key: str, use_openai: bool,
    fold_threshold: float, max_turns: int, max_cost: float, timeout: float,
    score_mode: str, fold_after_turn: int | None,
    max_fold_limit: int = 3, fold_memory_mode: str = "three_parallel",
    max_output_tokens: int = 2048,
) -> dict[str, Any]:
    case_dir = "case-" + hashlib.sha256(case["task_id"].encode("utf-8")).hexdigest()[:16]
    run_dir = output_dir / case_dir / condition
    run_dir.mkdir(parents=True, exist_ok=False)
    fold_events: list[dict[str, Any]] = []
    agent = Agent(
        model=model,
        api_base=api_base if use_openai else None,
        anthropic_base_url=api_base if not use_openai else None,
        api_key=api_key,
        api_protocol="openai" if use_openai else "anthropic",
        permission_mode="dontAsk",
        custom_tools=[tool for tool in tool_definitions if tool["name"] in READ_ONLY_TOOLS],
        max_turns=max_turns,
        max_output_tokens=max_output_tokens,
        max_cost_usd=max_cost,
        enable_folding=condition == "fold_on",
        max_fold_limit=max_fold_limit,
        fold_memory_mode=fold_memory_mode,
        auto_compact_threshold=fold_threshold,
        enable_memory_prefetch=False,
        enable_mcp=False,
        enable_online_evolution=False,
        fold_observer=fold_events.append,
    )
    output = ""
    status = "completed"
    error = ""
    started = time.monotonic()

    async def run_turns() -> None:
        nonlocal output
        for index, prompt in enumerate(case["prompts"]):
            if index == len(case["prompts"]) - 1:
                prompt += "\n\nEnd your final answer with one line: FINAL_ANSWER: <your answer>."
            result = await agent.run_once(prompt)
            output = str(result.get("text") or "")
            if agent._aborted:
                raise RuntimeError("agent run was aborted")
            if index == len(case["prompts"]) - 1 and getattr(agent, "last_finish_reason", None) in {"max_tokens", "length"}:
                raise EvaluationOutputTruncated("final response hit the per-response output token limit")
            if condition == "fold_on" and fold_after_turn == index + 1 and agent._fold_count == 0:
                if not await agent._compact_conversation(trigger="eval_scheduled"):
                    raise RuntimeError("scheduled fold could not run: insufficient conversation history")
            if index < len(case["prompts"]) - 1 and agent._get_current_cost_usd() >= max_cost:
                raise EvaluationBudgetExceeded("approximate cost cap reached before final turn")

    with (run_dir / "console.log").open("w", encoding="utf-8") as log, redirect_stdout(log):
        try:
            await asyncio.wait_for(run_turns(), timeout=timeout)
            if time.monotonic() - started >= timeout:
                status, error = "timeout", f"exceeded {timeout} seconds"
        except asyncio.TimeoutError:
            status, error = "timeout", f"exceeded {timeout} seconds"
            agent.abort()
        except EvaluationBudgetExceeded as exc:
            status, error = "budget_exceeded", str(exc)
        except EvaluationOutputTruncated as exc:
            status, error = "truncated", str(exc)
        except Exception as exc:
            if agent._aborted and time.monotonic() - started >= timeout:
                status, error = "timeout", f"exceeded {timeout} seconds"
            else:
                status, error = "error", f"{type(exc).__name__}: {exc}"
        finally:
            await agent.aclose()

    if status == "completed" and not output.strip():
        status = "empty"
    prediction = extract_answer(output) if status == "completed" else ""
    scoring = score_details(prediction, case["answers"], mode=score_mode) if status == "completed" else {
        "correct": False, "matched_answer": None, "normalized_prediction": None,
        "reason": f"run_{status}", "review_required": True,
    }
    trace = {
        "messages": agent._openai_messages if use_openai else agent._anthropic_messages,
        "folded_session_memories": agent._folded_session_memories,
        "fold_events_with_pre_fold_transcript": fold_events,
    }
    _write_json(run_dir / "trace.json", trace)
    row = {
        "task_id": case["task_id"], "condition": condition, "session_id": agent.session_id,
        "source_type": case.get("source_type", "custom"), "required_tools": case.get("required_tools", []),
        "capabilities_verified": case.get("capabilities_verified", True),
        "status": status, "error": error, "output": output, "prediction": prediction,
        "answers": case["answers"],
        "answer_marker_found": bool(FINAL_ANSWER.findall(output)) if status == "completed" else False,
        "correct": scoring["correct"], "scoring": scoring,
        "score_mode": score_mode, "fold_count": agent._fold_count,
        "max_fold_limit": max_fold_limit, "fold_memory_mode": fold_memory_mode,
        "max_output_tokens": max_output_tokens,
        "main_input_tokens": agent.total_input_tokens, "main_output_tokens": agent.total_output_tokens,
        "side_input_tokens": agent.side_input_tokens, "side_output_tokens": agent.side_output_tokens,
        "estimated_cost_usd": round(agent._get_current_cost_usd(), 6),
        "tool_turns": agent.current_turns, "elapsed_seconds": round(time.monotonic() - started, 3),
        "trace_file": str(run_dir / "trace.json"), "console_file": str(run_dir / "console.log"),
    }
    _write_json(run_dir / "result.json", row)
    return row


def _git_head() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paired mawoagent folding evaluation")
    parser.add_argument("--cases", type=Path, required=True, help="JSON array or JSONL with id/task_id, prompt(s), answer(s)")
    parser.add_argument("--output", type=Path, required=True, help="New output directory; existing paths are rejected")
    parser.add_argument("--text-only", action="store_true", help="Skip multimodal and attached-file cases")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of selected cases")
    parser.add_argument("--validate-only", action="store_true", help="Validate cases without model calls or output files")
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--fold-threshold", type=float, default=AUTO_COMPACT_THRESHOLD)
    parser.add_argument("--max-fold-limit", type=int, default=3, help="Maximum folds per case in fold_on; follows DeepAgent's default")
    parser.add_argument("--fold-memory-mode", choices=["three_parallel", "single_json"], default="three_parallel")
    parser.add_argument("--fold-after-turn", type=int, default=None, help="Schedule one fold after this user turn in fold_on only (multi-turn cases)")
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--max-output-tokens", type=int, default=2048, help="Maximum tokens in each main-model response")
    parser.add_argument("--max-cost", type=float, default=1.0, help="Per-condition approximate USD cap")
    parser.add_argument("--timeout", type=float, default=600, help="Seconds per condition")
    parser.add_argument("--score-mode", choices=["exact", "normalized", "numeric"], default="normalized")
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> None:
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be positive")
    if args.max_turns < 1 or args.max_cost <= 0 or args.timeout <= 0:
        raise ValueError("turn, cost and timeout limits must be positive")
    if args.max_output_tokens < 2:
        raise ValueError("--max-output-tokens must be at least 2")
    if not 0 < args.fold_threshold < 1:
        raise ValueError("--fold-threshold must be between 0 and 1")
    if args.max_fold_limit < 1:
        raise ValueError("--max-fold-limit must be positive")
    cases = load_cases(args.cases, text_only=args.text_only, limit=args.limit)
    if args.fold_after_turn is not None:
        if args.fold_after_turn < 2 or any(len(case["prompts"]) <= args.fold_after_turn for case in cases):
            raise ValueError("--fold-after-turn needs at least two preceding turns and a later final turn in every case")
    if args.validate_only:
        unverified = sum(not case["capabilities_verified"] for case in cases)
        print(f"Validated {len(cases)} cases; {unverified} need a tool-capability review; no model calls made.")
        return
    _load_env_file()
    import os
    model = args.model or os.environ.get("MODEL") or "deepseek-chat"
    api_base, api_key, use_openai = _resolve_api_config(args.api_base)
    if not api_key:
        raise ValueError("API key missing; configure .env as described in README.md")

    output_dir = args.output.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_json(output_dir / "config.json", {
        "cases_file": str(args.cases.resolve()),
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "case_ids": [case["task_id"] for case in cases],
        "git_head": _git_head(), "model": model, "api_base": api_base,
        "source_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (Path(__file__), Path(__file__).with_name("agent.py"),
                         Path(__file__).with_name("session_memory.py"),
                         Path(__file__).with_name("tools.py"), Path(__file__).with_name("main.py"))
        },
        "attachments_sha256": {
            case["task_id"]: hashlib.sha256(Path(case["attachment_path"]).read_bytes()).hexdigest()
            for case in cases if case["attachment_path"]
        },
        "fold_threshold": args.fold_threshold, "max_turns": args.max_turns,
        "max_output_tokens": args.max_output_tokens,
        "max_fold_limit": args.max_fold_limit, "fold_memory_mode": args.fold_memory_mode,
        "fold_after_turn": args.fold_after_turn,
        "max_cost_approx_usd": args.max_cost, "timeout_seconds": args.timeout,
        "score_mode": args.score_mode, "text_only": args.text_only,
        "tool_names": sorted(READ_ONLY_TOOLS),
        "case_requirements": {case["task_id"]: case["required_tools"] for case in cases},
        "capabilities_unverified_case_ids": [case["task_id"] for case in cases if not case["capabilities_verified"]],
        "notes": "Same cases run in both conditions. Only completed pairs with a fold in fold_on and none in fold_off enter the accuracy denominator. Available tools are local read-only tools; text-only does not imply a question is solvable without web access. String/numeric matching is not the official GAIA/HLE judge. Main and side token counts are separate.",
    })
    rows: list[dict[str, Any]] = []
    with (output_dir / "results.jsonl").open("w", encoding="utf-8") as results:
        for index, case in enumerate(cases):
            conditions = ("fold_on", "fold_off") if index % 2 == 0 else ("fold_off", "fold_on")
            for condition in conditions:
                row = await _run_case(
                    case, condition, output_dir=output_dir, model=model,
                    api_base=api_base, api_key=api_key, use_openai=use_openai,
                    fold_threshold=args.fold_threshold, max_turns=args.max_turns,
                    max_cost=args.max_cost, timeout=args.timeout, score_mode=args.score_mode,
                    fold_after_turn=args.fold_after_turn,
                    max_fold_limit=args.max_fold_limit, fold_memory_mode=args.fold_memory_mode,
                    max_output_tokens=args.max_output_tokens,
                )
                rows.append(row)
                results.write(json.dumps(row, ensure_ascii=False) + "\n")
                results.flush()
                print(f"{case['task_id']} {condition}: {row['status']}, correct={row['correct']}, folds={row['fold_count']}")
    summary = summarize(rows)
    _write_json(output_dir / "summary.json", summary)
    with (output_dir / "review_queue.jsonl").open("w", encoding="utf-8") as review:
        for row in rows:
            if row["scoring"]["review_required"] or not row["answer_marker_found"] or not row["capabilities_verified"]:
                review.write(json.dumps({
                    "task_id": row["task_id"], "condition": row["condition"],
                    "status": row["status"], "prediction": row["prediction"],
                    "answers": row["answers"], "scoring": row["scoring"],
                    "answer_marker_found": row["answer_marker_found"],
                    "capabilities_verified": row["capabilities_verified"],
                    "trace_file": row["trace_file"],
                }, ensure_ascii=False) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    main()
