"""Check whether the configured execution backend really works.

Intended to be run by a human on a machine that has Docker, because no CI
runner in this repository does. It answers one question: does a command run
inside the sandbox, and does the workspace stay the only writable place?

    python -m agents.sandbox_check
    MAWOAGENT_SANDBOX=docker python -m agents.sandbox_check --image mawoagent/sandbox:0.2

Exit code 0 means every check passed; 1 means at least one failed; 2 means the
backend is not usable here at all (for example no docker daemon), which is not a
failure of the code but an environment limitation.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

from .sandbox import get_backend


async def _probe(backend) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    # The container has `python` on PATH; the host may only have sys.executable.
    interpreter = "python" if backend.name == "docker" else f'"{sys.executable}"'
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        (workspace / "probe.txt").write_text("before\n", encoding="utf-8")
        (workspace / "probe_read.py").write_text(
            "import os\nfrom pathlib import Path\nprint(os.getcwd())\nprint(Path('probe.txt').read_text().strip())\n",
            encoding="utf-8",
        )
        (workspace / "probe_write.py").write_text(
            "from pathlib import Path\nPath('probe.txt').write_text('after\\n')\nprint(Path('probe.txt').read_text().strip())\n",
            encoding="utf-8",
        )
        (workspace / "probe_net.py").write_text(
            "import socket\nsocket.create_connection(('1.1.1.1', 443), 3)\nprint('connected')\n",
            encoding="utf-8",
        )

        result = await backend.run(f'{interpreter} probe_read.py', timeout_s=60, cwd=workspace)
        ok = result.returncode == 0 and "before" in result.stdout
        results.append(("runs a command in the workspace", ok, result.error or result.stderr.strip()[:200]))

        write = await backend.run(f'{interpreter} probe_write.py', timeout_s=60, cwd=workspace)
        visible = (workspace / "probe.txt").read_text(encoding="utf-8").strip() == "after"
        results.append(("writes reach the workspace", write.returncode == 0 and visible, write.error))

        # Only an isolating backend is expected to cut the network; the local
        # backend runs on the host and always has it.
        if backend.name != "local":
            network = await backend.run(f'{interpreter} probe_net.py', timeout_s=30, cwd=workspace)
            blocked = network.returncode != 0
            results.append(("network is blocked", blocked, "" if blocked else "connection succeeded"))

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe the configured execution backend.")
    parser.add_argument("--image", default=None, help="container image for the docker backend")
    args = parser.parse_args(argv)

    if args.image:
        import os
        os.environ["MAWOAGENT_SANDBOX_IMAGE"] = args.image

    try:
        backend = get_backend()
    except ValueError as exc:
        print(f"backend unavailable: {exc}")
        return 2

    try:
        results = asyncio.run(_probe(backend))
    except FileNotFoundError as exc:
        print(f"backend unavailable: {exc}")
        return 2

    failed = 0
    for name, ok, detail in results:
        print(f"[{'ok' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        failed += 0 if ok else 1
    print(f"\nbackend: {backend.name}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
