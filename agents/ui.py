"""Terminal UI rendering — colored output, spinner, tool display."""

from __future__ import annotations

import sys
import threading
import time

from rich import box
from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)
_stream = sys.stdout
_text_sink = None


def configure_stream(*, stderr: bool = False) -> None:
    """Send all human-facing output somewhere else than stdout.

    Structured output modes own stdout: a streamed assistant line mixed into a
    JSONL stream would corrupt every record, so the UI moves to stderr instead
    of being switched off.
    """

    global console, _stream
    _stream = sys.stderr if stderr else sys.stdout
    console = Console(highlight=False, stderr=stderr)


def set_text_sink(callback) -> None:
    """Observe assistant text without changing where it is rendered."""

    global _text_sink
    _text_sink = callback


def _safe_print(*args, **kwargs):
    """Console rendering must never break the agent loop (e.g. CI pipes)."""
    try:
        Console.print(console, *args, **kwargs)
    except (OSError, UnicodeError, ValueError):
        pass

# ─── Basic output ──────────────────────────────────────────


def _safe_text(value: object) -> str:
    return str(value).encode("utf-8", errors="replace").decode("utf-8")


def _safe_stdout_write(text: object) -> None:
    _stream.write(_safe_text(text))
    _stream.flush()


COOKIE_ART = r"""
    /\_/\
   ( o.o )
    > ^ <
"""


def print_welcome() -> None:
    title = Text("mawoagent", style="bold #f6c177")
    subtitle = Text("Evolvable Coding Agent CLI", style="bold cyan")
    cookie = Text(COOKIE_ART, style="bold #d19a66")

    commands = Table.grid(padding=(0, 2))
    commands.add_column(style="bold cyan", no_wrap=True)
    commands.add_column(style="dim")
    commands.add_row("/plan", "read-only planning workflow")
    commands.add_row("/skills", "list reusable skills")
    commands.add_row("/skill-create", "create a reusable skill")
    commands.add_row("/skill-stats", "show skill evolution stats")
    commands.add_row("/memory", "list long-term memories")
    commands.add_row("/compact", "compact current context")
    commands.add_row("/symbols", "outline or search workspace symbols")
    commands.add_row("/diff", "show what the last turn changed")
    commands.add_row("/undo", "revert the last turn")
    commands.add_row("exit", "quit the session")

    body = Table.grid()
    body.add_row(Align.center(cookie))
    body.add_row(Align.center(title))
    body.add_row(Align.center(subtitle))
    body.add_row("")
    body.add_row(Panel(commands, title="Quick Commands", border_style="cyan", box=box.ROUNDED))

    _safe_print()
    _safe_print(Panel(
        body,
        title="[bold #f6c177] mawoagent ready [/bold #f6c177]",
        subtitle="[dim]Type your request below[/dim]",
        border_style="#d19a66",
        box=box.ROUNDED,
        padding=(1, 2),
    ))
    _safe_print()


def print_user_prompt() -> None:
    _safe_print("\n[bold #f6c177]mawoagent[/bold #f6c177] [dim]❯[/dim] ", end="")


def print_assistant_text(text: str) -> None:
    if _text_sink is not None:
        try:
            _text_sink(text)
        except Exception:  # noqa: BLE001 - an observer must not break the stream
            pass
    _safe_stdout_write(text)


def print_tool_call(name: str, inp: dict) -> None:
    icon = _get_tool_icon(name)
    summary = _get_tool_summary(name, inp)
    table = Table.grid(padding=(0, 1))
    table.add_column(style="bold yellow", no_wrap=True)
    table.add_column(style="white")
    table.add_row("tool", f"{icon} {name}")
    if summary:
        table.add_row("input", _safe_text(summary))
    _safe_print(Panel(
        table,
        title="[bold yellow]Tool Call[/bold yellow]",
        border_style="yellow",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_tool_result(name: str, result: str) -> None:
    result = _safe_text(result)
    if (name in ("edit_file", "write_file")) and not result.startswith("Error"):
        _print_file_change_result(name, result)
        return
    max_len = 500
    truncated = result
    if len(result) > max_len:
        truncated = result[:max_len] + f"\n  ... ({len(result)} chars total)"
    _safe_print(Panel(
        _safe_text(truncated),
        title=f"[dim]{name} result[/dim]",
        border_style="dim",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def _print_file_change_result(_name: str, result: str) -> None:
    lines = result.split("\n")
    _safe_print(Panel(
        _safe_text(lines[0]),
        title="[bold green]File Change[/bold green]",
        border_style="green",
        box=box.ROUNDED,
        padding=(0, 1),
    ))

    max_display = 40
    content_lines = lines[1:]
    display_lines = content_lines[:max_display]

    for line in display_lines:
        if not line.strip():
            continue
        if line.startswith("@@"):
            _safe_print(f"[cyan]  {line}[/cyan]")
        elif line.startswith("- "):
            _safe_print(f"[red]  {line}[/red]")
        elif line.startswith("+ "):
            _safe_print(f"[green]  {line}[/green]")
        else:
            _safe_print(f"[dim]  {line}[/dim]")
    if len(content_lines) > max_display:
        _safe_print(f"[dim]  ... ({len(content_lines) - max_display} more lines)[/dim]")


def print_error(msg: str) -> None:
    _safe_print(Panel(
        _safe_text(msg),
        title="[bold red]Error[/bold red]",
        border_style="red",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_confirmation(command: str) -> None:
    # File changes now arrive with a diff attached, so the generic "dangerous
    # command" heading would misdescribe what the user is being asked to review.
    editing = command.startswith(("edit file:", "write file:"))
    _safe_print(Panel(
        _safe_text(command),
        title="[bold yellow]Review change[/bold yellow]" if editing else "[bold yellow]Dangerous command[/bold yellow]",
        border_style="yellow",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_divider() -> None:
    console.rule("[dim]turn complete[/dim]", style="dim")


def print_cost(input_tokens: int, output_tokens: int, input_price: float = 3, output_price: float = 15) -> None:
    cost_in = (input_tokens / 1_000_000) * input_price
    cost_out = (output_tokens / 1_000_000) * output_price
    total = cost_in + cost_out
    table = Table.grid(padding=(0, 2))
    table.add_column(style="cyan")
    table.add_column(style="white")
    table.add_row("input", f"{input_tokens} tokens")
    table.add_row("output", f"{output_tokens} tokens")
    table.add_row("estimate", f"${total:.4f}")
    _safe_print(Panel(table, title="Cost", border_style="cyan", box=box.ROUNDED))


def print_retry(attempt: int, max_retries: int, reason: str) -> None:
    _safe_print(_safe_text(f"\n  [yellow]↻ Retry {attempt}/{max_retries}: {reason}[/yellow]"))


def print_info(msg: str) -> None:
    _safe_print(Panel(
        _safe_text(msg),
        title="[bold cyan]Info[/bold cyan]",
        border_style="cyan",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_warning(msg: str) -> None:
    _safe_print(Panel(
        _safe_text(msg),
        title="[bold yellow]Notice[/bold yellow]",
        border_style="yellow",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_goodbye() -> None:
    _safe_print(Panel(
        Text("Bye. See you next time.", style="bold #f6c177"),
        border_style="#d19a66",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_interrupted() -> None:
    print_warning("Interrupted. Press Ctrl+C again to exit.")


# ─── Spinner ──────────────────────────────────────────────

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_spinner_thread: threading.Thread | None = None
_spinner_stop = threading.Event()


def start_spinner(label: str = "Thinking") -> None:
    global _spinner_thread
    if _spinner_thread is not None:
        return
    _spinner_stop.clear()

    def _run() -> None:
        frame = 0
        _safe_stdout_write(f"\n  {SPINNER_FRAMES[0]} {label}...")
        while not _spinner_stop.is_set():
            time.sleep(0.08)
            frame = (frame + 1) % len(SPINNER_FRAMES)
            _safe_stdout_write(f"\r  {SPINNER_FRAMES[frame]} {label}...")

    _spinner_thread = threading.Thread(target=_run, daemon=True)
    _spinner_thread.start()


def stop_spinner() -> None:
    global _spinner_thread
    if _spinner_thread is None:
        return
    _spinner_stop.set()
    _spinner_thread.join(timeout=1)
    _spinner_thread = None
    _safe_stdout_write("\r\033[K")


# ─── Plan approval display ──────────────────────────────────


def print_plan_for_approval(plan_content: str) -> None:
    lines = plan_content.split("\n")
    max_lines = 60
    preview = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        preview += f"\n\n... ({len(lines) - max_lines} more lines)"
    _safe_print(Panel(
        _safe_text(preview),
        title="[bold cyan]Plan for Approval[/bold cyan]",
        border_style="cyan",
        box=box.ROUNDED,
        padding=(1, 2),
    ))


def print_plan_approval_options() -> None:
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column("choice", style="bold yellow", no_wrap=True)
    table.add_column("action", style="white")
    table.add_column("detail", style="dim")
    table.add_row("1", "Clear context and execute", "fresh start with auto-accept edits")
    table.add_row("2", "Execute", "keep context, auto-accept edits")
    table.add_row("3", "Manually approve edits", "keep context, confirm each edit")
    table.add_row("4", "Keep planning", "provide feedback to revise")
    _safe_print(Panel(table, title="[bold yellow]Choose an option[/bold yellow]", border_style="yellow", box=box.ROUNDED))


# ─── Sub-agent display ──────────────────────────────────────


def print_sub_agent_start(agent_type: str, description: str) -> None:
    _safe_print(Panel(
        _safe_text(description),
        title=f"[bold magenta]Sub-agent started: {agent_type}[/bold magenta]",
        border_style="magenta",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_sub_agent_end(agent_type: str, _description: str) -> None:
    _safe_print(Panel(
        "completed",
        title=f"[bold magenta]Sub-agent finished: {agent_type}[/bold magenta]",
        border_style="magenta",
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def print_memory_entries(memories: list[object]) -> None:
    table = Table(box=box.ROUNDED, header_style="bold cyan", border_style="cyan")
    table.add_column("Type", style="bold #f6c177", no_wrap=True)
    table.add_column("Name", style="white")
    table.add_column("Description", style="dim")
    for m in memories:
        table.add_row(
            _safe_text(getattr(m, "type", "")),
            _safe_text(getattr(m, "name", "")),
            _safe_text(getattr(m, "description", "")),
        )
    _safe_print(Panel(table, title="[bold cyan]Memories[/bold cyan]", border_style="cyan", box=box.ROUNDED))


def print_skill_entries(skills: list[object]) -> None:
    table = Table(box=box.ROUNDED, header_style="bold cyan", border_style="cyan")
    table.add_column("Skill", style="bold #f6c177", no_wrap=True)
    table.add_column("Source", style="cyan", no_wrap=True)
    table.add_column("Mode", style="magenta", no_wrap=True)
    table.add_column("Description", style="white")
    for s in skills:
        name = getattr(s, "name", "")
        tag = f"/{name}" if getattr(s, "user_invocable", False) else name
        table.add_row(
            _safe_text(tag),
            _safe_text(getattr(s, "source", "")),
            _safe_text(getattr(s, "context", "")),
            _safe_text(getattr(s, "description", "")),
        )
    _safe_print(Panel(table, title="[bold cyan]Skills[/bold cyan]", border_style="cyan", box=box.ROUNDED))


# ─── Tool icons and summaries ───────────────────────────────

_TOOL_ICONS = {
    "read_file": "📖",
    "write_file": "✏️",
    "edit_file": "🔧",
    "list_files": "📁",
    "grep_search": "🔍",
    "run_shell": "💻",
    "skill": "⚡",
    "skill_create": "🍪",
    "skill_evolve": "🧬",
    "agent": "🤖",
}


def _get_tool_icon(name: str) -> str:
    return _TOOL_ICONS.get(name, "🔨")


def _get_tool_summary(name: str, inp: dict) -> str:
    if name == "read_file":
        return inp.get("file_path", "")
    if name == "write_file":
        return inp.get("file_path", "")
    if name == "edit_file":
        return inp.get("file_path", "")
    if name == "list_files":
        return inp.get("pattern", "")
    if name == "grep_search":
        return f'"{inp.get("pattern", "")}" in {inp.get("path", ".")}'
    if name == "run_shell":
        cmd = inp.get("command", "")
        return cmd[:60] + "..." if len(cmd) > 60 else cmd
    if name == "skill":
        return inp.get("skill_name", "")
    if name == "skill_create":
        return inp.get("name", "")
    if name == "skill_evolve":
        return inp.get("skill_name", "")
    if name == "agent":
        return f'[{inp.get("type", "general")}] {inp.get("description", "")}'
    return ""
