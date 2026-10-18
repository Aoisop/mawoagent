"""Persist large tool output once; keep bounded previews in sessions and journals."""
from __future__ import annotations

from pathlib import Path
import hashlib
import re
import uuid

from .storage import atomic_write_text


MAX_INLINE_RESULT_BYTES = 30 * 1024


def _matches_artifact(path: Path, encoded: bytes) -> bool:
    if path.is_symlink():
        return False
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        return digest.digest() == hashlib.sha256(encoded).digest()
    except OSError:
        return False


def persist_tool_result(tool_name: str, result: str, *, reuse_existing: bool = False) -> dict[str, str]:
    encoded = result.encode("utf-8", errors="replace")
    result = encoded.decode("utf-8")
    size = len(encoded)
    if size <= MAX_INLINE_RESULT_BYTES:
        return {"result": result}

    directory = Path.home() / ".mawoagent" / "tool-results"
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", tool_name)[:80]
    identity = hashlib.sha256(encoded).hexdigest() if reuse_existing else uuid.uuid4().hex
    path = directory / f"{identity}-{safe_name}.txt"
    # Publish the complete artifact durably before its checkpoint refers to it.
    if not reuse_existing or not _matches_artifact(path, encoded):
        atomic_write_text(path, result)
    preview = "\n".join(result[:6000].split("\n", 200)[:200])
    summary = (
        f"[Result too large ({size / 1024:.1f} KB, {result.count(chr(10)) + 1} lines). "
        f"Full output saved to {path}. "
        "Use read_file with offset/limit to read the required lines.]\n\n"
        f"Preview (up to 200 lines / 6000 characters):\n{preview}"
    )
    return {"result": summary, "result_file": str(path)}
