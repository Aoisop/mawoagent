"""Optional OS scheduling for unattended runs (iteration I7 / T21).

A schedule is a small recorded intent: which workspace, which cron time, which
budget, and what to do when a previous run is still going. Nothing here installs
itself — ``install`` renders the platform entry and only touches the OS when
asked, because a scheduler that writes to crontab on import is a surprise.

Three behaviours are deliberate:

* **One writer per project.** Two schedules in the same workspace take turns;
  the second one skips instead of interleaving edits.
* **Results are persisted.** Every attempt appends a record, including the ones
  that never started, so "it ran at 9am and did nothing" is answerable.
* **No retry after unknown side effects.** A run that failed before any tool
  call is safe to retry; a run that failed after editing files is not, because
  repeating it could duplicate the work.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .storage import append_jsonl, try_state_lock, write_json

OVERLAP_POLICIES = ("skip", "queue")
RETRY_POLICIES = ("never", "startup_only")
# bypassPermissions is deliberately absent: a schedule runs without a human.
PERMISSION_MODES = ("default", "plan", "acceptEdits", "dontAsk")
CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


class ScheduleError(ValueError):
    pass


def schedules_root() -> Path:
    return Path.home() / ".mawoagent" / "schedules"


def _safe_name(name: str) -> str:
    if not name or not all(character.isalnum() or character in "-_." for character in name):
        raise ScheduleError(f"invalid schedule name: {name!r}")
    return name


def _valid_cron_field(field: str, low: int, high: int) -> bool:
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or int(raw_step) < 1:
                return False
            step = int(raw_step)
        if part == "*":
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            if not (start.isdigit() and end.isdigit()):
                return False
            if not (low <= int(start) <= int(end) <= high):
                return False
            continue
        if not part.isdigit() or not (low <= int(part) <= high):
            return False
        if step > 1 and part == "":
            return False
    return True


def validate_cron(expression: str) -> str:
    """Five fields, each a value, range, list or step within its bounds."""

    fields = expression.split()
    if len(fields) != 5:
        raise ScheduleError("cron must have five fields: minute hour day month weekday")
    for field, (low, high) in zip(fields, CRON_RANGES):
        if not _valid_cron_field(field, low, high):
            raise ScheduleError(f"invalid cron field: {field!r}")
    return " ".join(fields)


@dataclass(frozen=True)
class ScheduleSpec:
    name: str
    prompt: str
    workspace: Path
    cron: str
    timezone: str = "local"
    model: str | None = None
    max_turns: int | None = None
    max_cost: float | None = None
    overlap: str = "skip"
    retry: str = "never"
    permission_mode: str = "dontAsk"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def validate(self) -> None:
        _safe_name(self.name)
        if not self.prompt.strip():
            raise ScheduleError("prompt must not be empty")
        validate_cron(self.cron)
        if self.overlap not in OVERLAP_POLICIES:
            raise ScheduleError(f"unknown overlap policy: {self.overlap}")
        if self.retry not in RETRY_POLICIES:
            raise ScheduleError(f"unknown retry policy: {self.retry}")
        if self.permission_mode not in PERMISSION_MODES:
            # A stored spec runs unattended; allowing bypassPermissions here
            # would let a file on disk opt the agent out of every confirmation.
            raise ScheduleError(
                f"permission_mode must be one of {', '.join(PERMISSION_MODES)}; "
                f"got {self.permission_mode!r}"
            )
        if not self.workspace.is_dir():
            raise ScheduleError(f"workspace does not exist: {self.workspace}")
        if self.max_turns is not None and self.max_turns < 1:
            raise ScheduleError("max_turns must be positive")
        if self.max_cost is not None and self.max_cost <= 0:
            raise ScheduleError("max_cost must be positive")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["workspace"] = str(self.workspace)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduleSpec":
        values = dict(data)
        values["workspace"] = Path(values["workspace"])
        return cls(**{key: value for key, value in values.items() if key in cls.__dataclass_fields__})


def save_spec(spec: ScheduleSpec, *, overwrite: bool = False) -> Path:
    spec.validate()
    root = schedules_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{spec.name}.json"
    if path.exists() and not overwrite:
        raise ScheduleError(f"schedule already exists: {spec.name}")
    write_json(path, spec.to_dict())
    return path


def load_spec(name: str) -> ScheduleSpec:
    path = schedules_root() / f"{_safe_name(name)}.json"
    if not path.exists():
        raise ScheduleError(f"unknown schedule: {name}")
    spec = ScheduleSpec.from_dict(json.loads(path.read_text(encoding="utf-8")))
    # A file on disk has no more authority than one typed at the prompt.
    spec.validate()
    return spec


def list_specs() -> list[ScheduleSpec]:
    root = schedules_root()
    if not root.exists():
        return []
    specs = []
    for path in sorted(root.glob("*.json")):
        try:
            spec = ScheduleSpec.from_dict(json.loads(path.read_text(encoding="utf-8")))
            spec.validate()
            specs.append(spec)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return specs


def remove_spec(name: str) -> bool:
    path = schedules_root() / f"{_safe_name(name)}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


def runs_path(name: str) -> Path:
    return schedules_root() / "runs" / f"{_safe_name(name)}.jsonl"


def record_run(name: str, record: dict[str, Any]) -> None:
    record = {"type": "run", "schedule": name, **record}
    append_jsonl(runs_path(name), record)


def read_runs(name: str, limit: int = 20) -> list[dict[str, Any]]:
    path = runs_path(name)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]


def project_lock(workspace: Path) -> Path:
    """One writer per project: two schedules must not edit at the same time."""

    return Path(workspace) / ".mawoagent" / "schedule.lock"


# ─── execution ─────────────────────────────────────────────────────────────

Runner = Callable[[ScheduleSpec], dict[str, Any]]


def default_runner(spec: ScheduleSpec) -> dict[str, Any]:
    """Run the agent headless; tool calls are counted where they happen."""

    from .agent import Agent
    from .config import load_env_file, resolve_api_config, resolve_agent_options
    from .workspace import WorkspaceContext

    load_env_file()
    api_config = resolve_api_config()
    if not api_config[1]:
        return {"status": "error", "error": "API key is required", "tool_calls": 0, "turns": 0, "cost_usd": 0.0}

    agent = Agent(
        **resolve_agent_options(api_config=api_config, model=spec.model),
        permission_mode=spec.permission_mode,
        max_cost_usd=spec.max_cost,
        max_turns=spec.max_turns,
        enable_mcp=False,
        workspace=WorkspaceContext(root=spec.workspace),
    )
    calls = {"count": 0}
    original = agent._run_checked_tool

    async def counting(name, inp):
        calls["count"] += 1
        return await original(name, inp)

    agent._run_checked_tool = counting
    status, error = "ok", ""
    try:
        asyncio.run(agent.chat(spec.prompt))
    except BaseException as exc:  # noqa: BLE001 - recorded, not raised
        status, error = "error", f"{type(exc).__name__}: {exc}"
    finally:
        asyncio.run(agent.aclose())
    if getattr(agent, "last_finish_reason", None) == "budget_exceeded":
        status, error = "budget_exceeded", "budget exhausted"
    return {
        "status": status,
        "error": error,
        "tool_calls": calls["count"],
        "turns": agent.current_turns,
        "cost_usd": round(agent._get_current_cost_usd(), 6),
    }


def should_retry(spec: ScheduleSpec, record: dict[str, Any]) -> bool:
    """Retry only when nothing was attempted; edits are not safe to repeat."""

    if spec.retry == "never":
        return False
    if spec.retry == "startup_only":
        return record.get("status") == "error" and int(record.get("tool_calls", 0)) == 0
    return False


def run_once(name: str, *, runner: Runner | None = None, attempts: int = 2) -> dict[str, Any]:
    spec = load_spec(name)
    runner = runner or default_runner
    lock = project_lock(spec.workspace)

    with try_state_lock(lock) as acquired:
        if not acquired:
            record = {
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "status": "skipped_overlap",
                "tool_calls": 0,
                "turns": 0,
                "cost_usd": 0.0,
                "error": "another run holds the project lock",
                "attempts": 0,
            }
            record_run(name, record)
            return record

        attempt = 0
        while True:
            attempt += 1
            started = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                outcome = runner(spec)
            except BaseException as exc:  # noqa: BLE001 - the record carries the failure
                outcome = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "tool_calls": 0}
            record = {"started_at": started, "attempts": attempt, **outcome}
            if attempt < attempts and should_retry(spec, record):
                continue
            record_run(name, record)
            return record


# ─── OS integration ────────────────────────────────────────────────────────

def _fixed_time(cron: str) -> tuple[int, int]:
    fields = cron.split()
    minute, hour = fields[0], fields[1]
    if not (minute.isdigit() and hour.isdigit()):
        raise ScheduleError(
            "this platform needs a fixed minute and hour; express the schedule as "
            "'M H * * *' or keep using an external cron"
        )
    return int(minute), int(hour)


def _argv(spec: ScheduleSpec) -> list[str]:
    """The command as arguments, never as a string to be re-parsed."""

    return [sys.executable or "python", "-m", "agents.schedule", "run", spec.name]


def _posix_command(spec: ScheduleSpec) -> str:
    log = shlex.quote(str(schedules_root() / f"{spec.name}.log"))
    return f"cd {shlex.quote(str(spec.workspace))} && {shlex.join(_argv(spec))} >> {log} 2>&1"


def _windows_launcher(spec: ScheduleSpec) -> str:
    """A .cmd file keeps the quoting out of schtasks.

    /TR takes a single string that cmd.exe re-parses, so a workspace path with a
    quote or an ampersand changes the meaning of the command. A launcher file is
    written once, quoted once, and referenced by an argument list.
    """

    log = schedules_root() / f"{spec.name}.log"
    python = sys.executable or "python"
    return (
        "@echo off\r\n"
        f'cd /d "{spec.workspace}"\r\n'
        f'"{python}" -m agents.schedule run {spec.name} >> "{log}" 2>&1\r\n'
    )


def render_os_entry(spec: ScheduleSpec, platform: str | None = None) -> dict[str, Any]:
    """Return the file, line or command that would install this schedule."""

    platform = platform or ("windows" if os.name == "nt" else sys.platform)
    root = schedules_root()
    if platform == "windows":
        minute, hour = _fixed_time(spec.cron)
        task = f"mawoagent-{spec.name}"
        launcher = root / f"{spec.name}.cmd"
        argv = [
            "schtasks", "/Create", "/TN", task, "/SC", "DAILY",
            "/ST", f"{hour:02d}:{minute:02d}", "/TR", str(launcher), "/F",
        ]
        return {
            "platform": "windows", "kind": "task", "path": str(launcher),
            "content": _windows_launcher(spec), "argv": argv,
            "command": subprocess.list2cmdline(argv),
        }
    if platform == "darwin":
        from xml.sax.saxutils import escape as _xml_escape

        minute, hour = _fixed_time(spec.cron)
        label = f"dev.mawoagent.{spec.name}"
        # The path is shell-quoted by _posix_command and XML-escaped here: both
        # layers have their own metacharacters.
        command = _xml_escape(_posix_command(spec))
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>-c</string><string>{command}</string>
  </array>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>{hour}</integer>
    <key>Minute</key><integer>{minute}</integer>
  </dict>
  <key>StandardOutPath</key><string>{root / (spec.name + '.log')}</string>
  <key>StandardErrorPath</key><string>{root / (spec.name + '.log')}</string>
</dict></plist>
"""
        return {"platform": "darwin", "kind": "file",
                "path": str(Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"), "content": plist}
    line = f"{spec.cron} {_posix_command(spec)} # mawoagent:{spec.name}"
    return {"platform": "linux", "kind": "crontab", "path": "crontab", "content": line}


def install(name: str, *, apply: bool = False, executor: Callable[[list[str]], int] | None = None) -> dict[str, Any]:
    """Render the platform entry; only touch the system when *apply* is set."""

    spec = load_spec(name)
    entry = render_os_entry(spec)
    if not apply:
        return {**entry, "applied": False}

    if entry["kind"] == "crontab":
        # Editing a user's crontab is destructive and hard to undo; print instead.
        raise ScheduleError(
            "crontab installation requires a manual step: append this line with `crontab -e`:\n"
            + entry["content"]
        )

    path = Path(entry["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(entry["content"], encoding="utf-8")
    if entry["kind"] == "file":
        return {**entry, "applied": True}

    runner = executor or (lambda argv: subprocess.run(argv, check=False).returncode)
    code = runner(entry["argv"])
    return {**entry, "applied": code == 0, "exit_code": code}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="agents.schedule", description="Manage scheduled agent runs.")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="record a schedule")
    add.add_argument("--name", required=True)
    add.add_argument("--prompt", required=True)
    add.add_argument("--cron", required=True)
    add.add_argument("--workspace", default=None)
    add.add_argument("--timezone", default="local")
    add.add_argument("--model", default=None)
    add.add_argument("--max-turns", type=int, default=None)
    add.add_argument("--max-cost", type=float, default=None)
    add.add_argument("--overlap", choices=OVERLAP_POLICIES, default="skip")
    add.add_argument("--retry", choices=RETRY_POLICIES, default="never")
    add.add_argument("--overwrite", action="store_true")

    for name in ("list", "remove", "show", "install", "run"):
        command = sub.add_parser(name)
        if name == "list":
            command.add_argument("name", nargs="?")
        else:
            command.add_argument("name")
        if name == "install":
            command.add_argument("--apply", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "add":
            spec = ScheduleSpec(
                name=args.name, prompt=args.prompt,
                workspace=Path(args.workspace or Path.cwd()),
                cron=args.cron, timezone=args.timezone, model=args.model,
                max_turns=args.max_turns, max_cost=args.max_cost,
                overlap=args.overlap, retry=args.retry,
            )
            print(f"saved {save_spec(spec, overwrite=args.overwrite)}")
        elif args.command == "list":
            for spec in list_specs():
                print(f"{spec.name} | {spec.cron} | {spec.workspace}")
        elif args.command == "show":
            print(json.dumps(load_spec(args.name).to_dict(), ensure_ascii=False, indent=2))
        elif args.command == "remove":
            print("removed" if remove_spec(args.name) else "not found")
        elif args.command == "install":
            print(json.dumps(install(args.name, apply=args.apply), ensure_ascii=False, indent=2))
        elif args.command == "run":
            record = run_once(args.name)
            print(json.dumps(record, ensure_ascii=False))
            return 0 if record.get("status") == "ok" else 1
    except ScheduleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
