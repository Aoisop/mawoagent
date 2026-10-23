"""CLI entry point and interactive REPL — mirrors cli.ts."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import threading
from pathlib import Path

from .agent import Agent
from .config import (
    load_env_file as _load_env_file,
    resolve_api_config as _resolve_api_config,
    resolve_agent_options,
)
from .version import __version__
from .ui import (
    print_welcome,
    print_user_prompt,
    print_error,
    print_info,
    print_plan_for_approval,
    print_plan_approval_options,
    print_goodbye,
    print_interrupted,
    print_memory_entries,
    print_skill_entries,
    print_warning,
)
from .session import load_session, get_latest_session_id
from .skill_evolution import restore_pruned_skill
from .tools import check_permission
from .memory import list_memories
from .skills import (
    create_skill,
    discover_skills,
    evolve_skill,
    execute_skill,
    get_skill_by_name,
    record_feedback,
    skill_stats,
)
from .online_skill_eval import format_online_skill_eval_async


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mawoagent",
        description="mawoagent — a minimal coding agent",
        add_help=False,
    )
    parser.add_argument("--version", action="version", version=f"mawoagent {__version__}")
    parser.add_argument("prompt", nargs="*", help="One-shot prompt")
    parser.add_argument("--yolo", "-y", action="store_true", help="Skip all confirmation prompts")
    parser.add_argument("--plan", action="store_true", help="Plan mode: read-only")
    parser.add_argument("--accept-edits", action="store_true", help="Auto-approve file edits")
    parser.add_argument("--dont-ask", action="store_true", help="Auto-deny confirmations (for CI)")
    parser.add_argument("--thinking", action="store_true", help="Enable extended thinking")
    parser.add_argument("--model", "-m", default=None, help="Model to use")
    parser.add_argument("--api-base", default=None, help="OpenAI-compatible API base URL")
    parser.add_argument("--resume", action="store_true", help="Resume latest session in this project")
    parser.add_argument("--resume-id", help="Resume a specific session in this project")
    parser.add_argument("--input-price", type=float, default=None, help="Estimated USD per million input tokens")
    parser.add_argument("--output-price", type=float, default=None, help="Estimated USD per million output tokens")
    parser.add_argument("--max-cost", type=float, default=None, help="Max USD spend")
    parser.add_argument("--max-turns", type=int, default=None, help="Max agentic turns")
    parser.add_argument("--context-window", type=int, default=None, help="Model context capacity in tokens")
    parser.add_argument("--no-folding", action="store_true", help="Disable full session folding")
    parser.add_argument("--fold-threshold", type=float, default=0.70, help="Auto-fold at this fraction of the context window")
    parser.add_argument("--help", "-h", action="store_true", help="Show help")
    return parser.parse_args()


def _resolve_permission_mode(args: argparse.Namespace) -> str:
    if args.yolo:
        return "bypassPermissions"
    if args.plan:
        return "plan"
    if args.accept_edits:
        return "acceptEdits"
    if args.dont_ask:
        return "dontAsk"
    return "default"


_pending_input: asyncio.Future | None = None


async def _read_repl_input(prompt: str = "") -> str:
    """Let background tasks run while stdin waits; don't keep exit alive on stdin."""
    global _pending_input
    loop = asyncio.get_running_loop()
    if _pending_input is not None and _pending_input.get_loop() is loop:
        pending = _pending_input
        try:
            value = await asyncio.shield(pending)
        except asyncio.CancelledError:
            raise
        except BaseException:
            _pending_input = None
            raise
        _pending_input = None
        return value
    result = _pending_input = loop.create_future()

    def complete(value: str | None, error: BaseException | None) -> None:
        if not result.done():
            if error is not None:
                result.set_exception(error)
            else:
                result.set_result(value)

    def read() -> None:
        value, error = None, None
        try:
            value = input(prompt) if prompt else input()
        except (EOFError, OSError) as exc:
            error = exc
        try:
            loop.call_soon_threadsafe(complete, value, error)
        except RuntimeError:
            pass  # The loop may already have closed after cancellation/exit.

    threading.Thread(target=read, daemon=True).start()
    try:
        value = await asyncio.shield(result)
    except asyncio.CancelledError:
        raise
    except BaseException:
        _pending_input = None
        raise
    _pending_input = None
    return value


def _turn_offset(parts: list[str]) -> int | None:
    """Translate a 1-based turn number into a skip count from the newest turn."""

    if not parts:
        return 0
    if len(parts) > 1:
        return None
    try:
        number = int(parts[0])
    except ValueError:
        return None
    if number < 1:
        return None
    return number - 1


async def run_repl(agent: Agent) -> None:
    previous_handler = signal.getsignal(signal.SIGINT)
    try:
        await _run_repl(agent)
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        await agent.aclose()


async def _run_repl(agent: Agent) -> None:
    """Interactive REPL loop."""

    async def confirm_fn(message: str) -> bool:
        try:
            answer = await _read_repl_input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False

    agent.set_confirm_fn(confirm_fn)

    async def plan_approval_fn(plan_content: str) -> dict:
        print_plan_for_approval(plan_content)
        print_plan_approval_options()
        while True:
            try:
                choice = (await _read_repl_input("  Enter choice (1-4): ")).strip()
            except EOFError:
                return {"choice": "cancel"}
            if choice == "1":
                return {"choice": "clear-and-execute"}
            elif choice == "2":
                return {"choice": "execute"}
            elif choice == "3":
                return {"choice": "manual-execute"}
            elif choice == "4":
                try:
                    feedback = (await _read_repl_input("  Feedback (what to change): ")).strip()
                except EOFError:
                    feedback = ""
                return {"choice": "keep-planning", "feedback": feedback or None}
            else:
                print_warning("Invalid choice. Enter 1, 2, 3, or 4.")

    agent.set_plan_approval_fn(plan_approval_fn)

    sigint_count = 0

    def handle_sigint(sig, frame):
        nonlocal sigint_count
        if agent.is_processing:
            # Agent is processing
            agent.abort()
            print_interrupted()
            sigint_count = 0
            print_user_prompt()
        else:
            sigint_count += 1
            if sigint_count >= 2:
                print_goodbye()
                sys.exit(0)
            print_warning("Press Ctrl+C again to exit.")
            print_user_prompt()

    signal.signal(signal.SIGINT, handle_sigint)
    print_welcome()

    while True:
        print_user_prompt()
        try:
            line = await _read_repl_input()
        except (EOFError, KeyboardInterrupt):
            print_goodbye()
            break

        inp = line.strip()
        sigint_count = 0

        if not inp:
            continue
        if inp in ("exit", "quit"):
            print_goodbye()
            break

        # REPL commands
        if inp == "/clear":
            agent.clear_history()
            continue
        if inp == "/trust-project":
            from .project_trust import trust_project
            try:
                trust_project()
                print_info(f"Trusted project configuration: {Path.cwd().resolve()}")
            except (ValueError, OSError) as exc:
                print_error(str(exc))
            continue
        if inp.startswith("/cleanup-state"):
            from .maintenance import cleanup_state
            try:
                parts = inp.split()[1:]
                apply = "--apply" in parts
                days = next((int(part) for part in parts if part != "--apply"), 30)
                preview = cleanup_state(days)
                print_info(str(preview))
                if apply and await agent.authorize_operation("state_cleanup", preview):
                    print_info(str(cleanup_state(days, apply=True, expected_paths=preview["paths"])))
            except (ValueError, OSError) as exc:
                print_error(str(exc))
            continue
        if inp == "/plan":
            agent.toggle_plan_mode()
            continue
        if inp.startswith("/symbols"):
            from .repo_index import format_search, format_stats, refresh_index
            parts = inp.split(maxsplit=1)
            index = refresh_index(agent.workspace.root)
            if len(parts) == 1:
                print_info(format_stats(index))
                outline = index.map_text(4000)
                print(outline if outline.strip() else "No symbols found in this workspace.")
            else:
                print_info(format_search(index.search(parts[1], limit=40)))
            continue
        if inp.startswith("/diff"):
            from .snapshots import build_diff, describe_turn, latest_turn
            parts = inp.split()[1:]
            skip = _turn_offset(parts)
            if skip is None:
                print_error("Usage: /diff [n] — n counts back from the latest turn.")
                continue
            turn = latest_turn(agent.session_id, agent.workspace.root, skip)
            if turn is None:
                print_info("No recorded turns for this session yet.")
                continue
            print_info(describe_turn(turn))
            diff = build_diff(turn)
            print(diff if diff.strip() else "No text changes to show.")
            continue
        if inp.startswith("/undo"):
            from .snapshots import describe_turn, latest_turn, undo_turn
            parts = inp.split()[1:]
            dry_run = "--dry-run" in parts
            skip = _turn_offset([part for part in parts if part != "--dry-run"])
            if skip is None:
                print_error("Usage: /undo [n] [--dry-run]")
                continue
            turn = latest_turn(agent.session_id, agent.workspace.root, skip)
            if turn is None:
                print_info("No recorded turns for this session yet.")
                continue
            print_info(describe_turn(turn))
            if dry_run:
                print_info("Dry run: nothing restored. Run /undo without --dry-run to apply.")
                continue
            result = undo_turn(turn, agent.workspace.root)
            if result["restored"]:
                print_info("Restored: " + ", ".join(result["restored"]))
            if result["removed"]:
                print_info("Removed (created during the turn): " + ", ".join(result["removed"]))
            if result["conflicts"]:
                print_warning(
                    "Skipped because the file changed after this turn: " + ", ".join(result["conflicts"])
                )
            if result["skipped"]:
                for item in result["skipped"]:
                    print_warning("Skipped: " + item)
            if not any(result[key] for key in ("restored", "removed", "conflicts", "skipped")):
                print_info("Nothing to undo.")
            continue
        if inp == "/cost":
            agent.show_cost()
            continue
        if inp == "/compact":
            try:
                await agent.compact()
            except Exception as e:
                print_error(str(e))
            continue
        if inp == "/memory":
            memories = list_memories()
            if not memories:
                print_info("No memories saved yet.")
            else:
                print_memory_entries(memories)
            continue
        if inp == "/skills":
            skills = discover_skills()
            if not skills:
                print_info("No skills found. Add skills to .mawoagent/skills/<name>/SKILL.md")
            else:
                print_skill_entries(skills)
            continue
        if inp == "/skill-candidates":
            from .skill_candidates import list_candidates
            try:
                for proposal in list_candidates():
                    print_info(f"{proposal['id']} | {proposal['status']} | {proposal['action']} {proposal['skill']}\n"
                               f"{proposal['write_input'].get('instructions', '')}")
            except (ValueError, OSError, KeyError, TypeError) as exc:
                print_error(str(exc))
            continue
        if inp.startswith("/skill-apply "):
            from .skill_candidates import read_candidate, apply_candidate
            try:
                candidate_id = inp[len("/skill-apply "):].strip()
                parts = candidate_id.split()
                if not parts or len(parts) > 2 or (len(parts) == 2 and parts[1] != "--manual-review"):
                    raise ValueError("Usage: /skill-apply <id> [--manual-review]")
                candidate_id = parts[0]
                proposal = read_candidate(candidate_id)
                import hashlib
                import json
                proposal_hash = hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()
                print_info(f"Publish proposed Skill {proposal['skill']}:\n{proposal['write_input'].get('instructions', '')}")
                operation = "skill_evolve" if proposal["action"] == "merge" else "skill_create"
                if await agent.authorize_operation(operation, proposal["write_input"]):
                    result = apply_candidate(candidate_id, manual_review="--manual-review" in parts, expected_hash=proposal_hash)
                    print_info(str(result))
                    agent._refresh_runtime_system_prompt()
                else:
                    print_info("Skill publication denied.")
            except (ValueError, OSError) as exc:
                print_error(str(exc))
            continue
        if inp.startswith("/skill-rollback "):
            from .skill_candidates import rollback_skill
            name = inp[len("/skill-rollback "):].strip()
            if await agent.authorize_operation("skill_evolve", {"skill_name": name}):
                try:
                    print_info(str(rollback_skill(name)))
                    agent._refresh_runtime_system_prompt()
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    print_error(str(exc))
            else:
                print_info("Skill rollback denied.")
            continue
        if inp == "/skill-stats":
            print_info(skill_stats())
            continue
        if inp == "/skill-eval":
            try:
                # Finish pending writes before reading provenance and active snapshots.
                await agent.drain_background_skill_tasks()
                side_query = agent._build_side_query(max_tokens=2400, require_complete=True)
                judge_model = os.environ.get("MAWOAGENT_SKILL_JUDGE_MODEL")
                judge_query = agent._build_side_query(max_tokens=2400, require_complete=True, model_override=judge_model) if judge_model else None
                print_info(await format_online_skill_eval_async(side_query=side_query, judge_side_query=judge_query))
            except (ValueError, OSError) as exc:
                print_error(f"Skill evaluation could not complete: {exc}")
            continue
        if inp.startswith("/extract_now"):
            hint = inp[len("/extract_now") :].strip()
            result = await agent.extract_now(hint)
            if result.get("ok"):
                print_info("Ran online skill extraction for the current pending window.")
            else:
                print_error(str(result.get("error") or result))
            continue
        if inp.startswith("/skill-feedback "):
            _, rest = inp.split(" ", 1)
            parts = rest.strip().split(" ", 2)
            if len(parts) < 2:
                print_error("Usage: /skill-feedback <skill-name> <rating> [note]")
                continue
            note = parts[2] if len(parts) > 2 else ""
            record_feedback(parts[0], parts[1], note)
            print_info(f"Recorded feedback for skill: {parts[0]}")
            continue
        if inp.startswith("/skill-evolve "):
            _, rest = inp.split(" ", 1)
            parts = rest.strip().split(" ", 1)
            if len(parts) < 2:
                print_error("Usage: /skill-evolve <skill-name> <durable lesson>")
                continue
            if not await agent.authorize_operation("skill_evolve", {"skill_name": parts[0], "lesson": parts[1]}):
                print_info("Skill evolution denied.")
                continue
            result = evolve_skill(parts[0], parts[1], rationale="Manual REPL evolution", target="active")
            if result.get("ok"):
                print_info(f"Evolved skill {result.get('skill')} to version {result.get('version')}")
            else:
                print_error(str(result.get("error") or result))
            continue
        if inp.startswith("/skill-restore "):
            name = inp[len("/skill-restore "):].strip()
            permission = check_permission("skill_restore", {"skill_name": name}, agent.permission_mode)
            if permission["action"] == "deny" or (permission["action"] == "confirm" and
                    not await agent._confirm_tool_call("skill_restore", permission)):
                print_info("Skill restoration denied.")
                continue
            try:
                result = restore_pruned_skill(name)
                if result.get("ok"):
                    from .skills import reset_skill_cache
                    reset_skill_cache()
                    agent._refresh_runtime_system_prompt()
                    print_info(f"Restored skill: {name}")
                else:
                    print_error(str(result.get("error")))
            except (ValueError, OSError) as exc:
                print_error(f"Skill restoration failed: {exc}")
            continue
        if inp.startswith("/skill-create "):
            _, rest = inp.split(" ", 1)
            parts = [part.strip() for part in rest.split("|", 3)]
            if len(parts) < 4 or not all(parts[:4]):
                print_error("Usage: /skill-create <name> | <description> | <when-to-use> | <instructions>")
                continue
            if not await agent.authorize_operation("skill_create", {"name": parts[0], "description": parts[1],
                    "when_to_use": parts[2], "instructions": parts[3], "target": "project"}):
                print_info("Skill creation denied.")
                continue
            result = create_skill(
                name=parts[0],
                description=parts[1],
                when_to_use=parts[2],
                instructions=parts[3],
                target="project",
                context="inline",
                user_invocable=False,
                evidence="Manual REPL skill creation",
            )
            if result.get("ok"):
                print_info(f"Created skill {result.get('skill')} at {result.get('file')}")
            else:
                print_error(str(result.get("error") or result))
            continue

        # Skill invocation: /<skill-name> [args]
        if inp.startswith("/"):
            space_idx = inp.find(" ")
            cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
            cmd_args = inp[space_idx + 1:] if space_idx > 0 else ""
            skill = get_skill_by_name(cmd_name)
            if skill and skill.user_invocable:
                print_info(f"Invoking skill: {skill.name}")
                try:
                    if skill.context == "fork":
                        await agent.chat(f'Use the skill tool to invoke "{skill.name}" with args: {cmd_args or "(none)"}')
                    else:
                        result = execute_skill(skill.name, cmd_args)
                        if not result:
                            print_error(f"Unknown skill: {skill.name}")
                            continue
                        await agent.chat(result["prompt"])
                except Exception as e:
                    if "abort" not in str(e).lower():
                        print_error(str(e))
                continue

        # Normal chat
        try:
            await agent.chat(inp)
        except Exception as e:
            if "abort" not in str(e).lower():
                print_error(str(e))


async def run_one_shot(agent: Agent, prompt: str) -> None:
    if agent.permission_mode == "plan":
        async def plan_only_approval_fn(plan_content: str) -> dict:
            print_plan_for_approval(plan_content)
            return {"choice": "plan-only"}

        agent.set_plan_approval_fn(plan_only_approval_fn)

    try:
        await agent.chat(prompt)
    finally:
        await agent.aclose()


def main() -> None:
    """CLI 程序入口：准备运行配置，创建 Agent，并按参数选择一次性执行或交互模式。"""
    # 解析命令行参数，例如 --plan、--resume、--model，以及可选的一次性 prompt。
    args = parse_args()
    _load_env_file()

    if args.help:
        # 自定义帮助文本，展示 mawoagent 支持的启动参数和 REPL 内置命令。
        print("""
Usage: mawoagent [options] [prompt]

Options:
  --yolo, -y          Skip all confirmation prompts (bypassPermissions mode)
  --plan              Plan mode: read-only, describe changes without executing
  --accept-edits      Auto-approve file edits, still confirm every shell command
  --dont-ask          Auto-deny anything needing confirmation (for CI)
  --thinking          Enable extended thinking (Anthropic only)
  --model, -m         Model to use (default: deepseek-chat, or MODEL env)
  --api-base URL      Override API base URL from CLI or .env
  --resume            Resume the latest session in the current project
  --resume-id ID      Resume a specific session in the current project
  --input-price USD   Estimated price per million input tokens (default: 3)
  --output-price USD  Estimated price per million output tokens (default: 15)
  --max-cost USD      Stop when estimated cost exceeds this amount
  --max-turns N       Stop after N agentic turns
  --context-window N Model context capacity in tokens
  --no-folding        Disable full session folding
  --fold-threshold X  Auto-fold threshold (0 < X < 1, default: 0.70)
  --help, -h          Show this help

REPL commands:
  /clear              Clear conversation history
  /plan               Toggle plan mode (read-only <-> normal)
  /cost               Show token usage and cost
  /compact            Manually compact conversation
  /symbols [query]    Outline the workspace symbols, or search them
  /diff [n]           Show what the n-th latest turn changed
  /undo [n]           Revert the n-th latest turn (--dry-run to preview)
  /memory             List saved memories
  /skills             List available skills
  /skill-stats        Show skill usage and evolution stats
  /skill-eval         Evaluate online skill evolution quality
  /extract_now        Extract the current pending online skill window: /extract_now [hint]
  /skill-feedback     Record feedback: /skill-feedback <skill> <rating> [note]
  /skill-evolve       Evolve a skill: /skill-evolve <skill> <durable lesson>
  /skill-restore      Restore an archived skill: /skill-restore <skill>
  /skill-create       Create a skill: /skill-create <name> | <description> | <when-to-use> | <instructions>
  /skill-candidates   Review isolated online Skill proposals
  /skill-apply        Publish a passed candidate: /skill-apply <id> [--manual-review]
  /skill-rollback     Restore the most recent snapshot: /skill-rollback <skill>
  /trust-project      Trust current project permission/MCP configuration
  /cleanup-state      Preview old artifacts: /cleanup-state [days] [--apply]
  /<skill-name>       Invoke a skill (e.g. /commit "fix types")

Examples:
  mawoagent "fix the bug in src/app.ts"
  mawoagent --yolo "run all tests and fix failures"
  mawoagent --plan "how would you refactor this?"
  mawoagent --max-cost 0.50 --max-turns 20 "implement feature X"
  MODEL=deepseek-chat APIKEY=sk-xxx API=https://api.deepseek.com/anthropic mawoagent "hello"
  MODEL=gpt-4o OPENAI_API_KEY=sk-xxx OPENAI_BASE_URL=https://aihubmix.com/v1 mawoagent "hello"
  mawoagent --resume
  mawoagent  # starts interactive REPL
""")
        sys.exit(0)

    # 将命令行布尔开关统一转换成 Agent 内部使用的权限模式。
    permission_mode = _resolve_permission_mode(args)
    api_config = _resolve_api_config(args.api_base)

    # 没有可用 API key 时无法调用模型，直接提示配置方式并退出。
    if not api_config[1]:
        print_error(
            "API key is required.\n"
            "  Set APIKEY (+ optional API) in .env for generic config,\n"
            "  or use ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL,\n"
            "  or use OPENAI_API_KEY / OPENAI_BASE_URL."
        )
        sys.exit(1)

    # 创建主 Agent。OpenAI-compatible 和 Anthropic 原生接口使用不同的 base URL 参数名传入。
    agent = Agent(
        **resolve_agent_options(
            api_config=api_config,
            model=args.model,
            input_price=args.input_price,
            output_price=args.output_price,
            context_window=args.context_window,
        ),
        permission_mode=permission_mode,
        thinking=args.thinking,
        max_cost_usd=args.max_cost,
        max_turns=args.max_turns,
        enable_folding=not args.no_folding,
        auto_compact_threshold=args.fold_threshold,
    )

    # Resume session
    # --resume 会加载最近一次会话，把历史消息恢复到新建的 Agent 中。
    if args.resume or args.resume_id:
        session_id = args.resume_id or get_latest_session_id()
        if session_id:
            session = load_session(session_id)
            if session:
                try:
                    agent.restore_session(session)
                except ValueError as exc:
                    print_error(str(exc))
                    return
            else:
                print_info("No session found to resume.")
        else:
            print_info("No previous sessions found.")

    # 如果命令行后面带了普通文本参数，就拼成一次性 prompt；否则进入交互式 REPL。
    prompt = " ".join(args.prompt) if args.prompt else None

    if prompt:
        # One-shot mode
        # 一次性模式：执行完用户 prompt 后进程结束。
        try:
            asyncio.run(run_one_shot(agent, prompt))
        except Exception as e:
            print_error(str(e))
            sys.exit(1)
    else:
        # Interactive REPL
        # 交互模式：启动循环读取用户输入，直到用户退出。
        asyncio.run(run_repl(agent))


if __name__ == "__main__":
    main()
