"""Typed tool results (iteration I5 / T15).

Before this module every tool result was a string. That worked because most
tools return text, but an MCP server can also return images, audio and resource
links: their bytes were either dropped or, worse, serialised as JSON and pushed
into the conversation — base64 counts against the context budget and tells the
model nothing.

Three rules govern this module:

* A text-only result is returned byte for byte as before. This is a foundation
  change, not a formatting change.
* Non-text content is written to disk once and referenced by path. The model
  receives a one-line description, never the payload.
* What the caller can actually consume is stated explicitly, so an unsupported
  attachment is reported as unsupported instead of being silently dropped.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .storage import atomic_write_bytes

TEXT = "text"
IMAGE = "image"
AUDIO = "audio"
RESOURCE = "resource"

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "audio/wav": ".wav",
    "audio/mpeg": ".mp3",
}


@dataclass(frozen=True)
class Attachment:
    """A non-text resource referenced by a tool result."""

    type: str
    mime_type: str = ""
    uri: str = ""
    size: int = 0
    note: str = ""

    def describe(self) -> str:
        size = f"{self.size / 1024:.1f} KB" if self.size else "unknown size"
        label = self.mime_type or self.type
        where = f" saved to {self.uri}" if self.uri else ""
        note = f" ({self.note})" if self.note else ""
        return f"[{label}, {size}{where}]{note}"

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "mime_type": self.mime_type,
            "uri": self.uri,
            "size": self.size,
            "note": self.note,
        }


@dataclass
class ToolContent:
    """The text the model reads, plus anything that is not text."""

    text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    supports_images: bool = False

    def render(self) -> str:
        """Text for the conversation: body first, then one line per attachment."""

        lines = [self.text] if self.text else []
        for attachment in self.attachments:
            lines.append(attachment.describe())
        return "\n".join(line for line in lines if line)


def _attachment_path(mime_type: str, payload: bytes, tool_name: str) -> Path:
    digest = hashlib.sha256(payload).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", tool_name)[:40] or "tool"
    suffix = _EXTENSIONS.get(mime_type, ".bin")
    directory = Path.home() / ".mawoagent" / "tool-results"
    return directory / f"{digest}-{safe}{suffix}"


def _decode_image(block: dict) -> tuple[bytes, str, str]:
    data = block.get("data")
    mime_type = str(block.get("mimeType") or block.get("mime_type") or "")
    if not isinstance(data, str):
        return b"", mime_type, "missing data"
    if data.startswith("data:"):
        header, _, payload = data.partition(",")
        mime_type = header[5:].split(";")[0] or mime_type
        data = payload
    try:
        # Whitespace is legal in MCP payloads; anything else that is not base64
        # is reported instead of being silently decoded to an empty buffer.
        compact = "".join(data.split())
        return base64.b64decode(compact, validate=True), mime_type, ""
    except (binascii.Error, ValueError):
        return b"", mime_type, "invalid base64"


def _store_image(block: dict, tool_name: str) -> Attachment:
    payload, mime_type, problem = _decode_image(block)
    if problem:
        return Attachment(type=IMAGE, mime_type=mime_type, note=f"not stored: {problem}")
    if not payload:
        return Attachment(type=IMAGE, mime_type=mime_type, note="not stored: empty payload")
    if len(payload) > MAX_ATTACHMENT_BYTES:
        return Attachment(
            type=IMAGE,
            mime_type=mime_type,
            size=len(payload),
            note="not stored: larger than the attachment limit",
        )
    path = _attachment_path(mime_type, payload, tool_name)
    if not path.exists():
        atomic_write_bytes(path, payload)
    return Attachment(type=IMAGE, mime_type=mime_type, uri=str(path), size=len(payload))


def from_mcp_content(blocks: list, tool_name: str = "tool") -> ToolContent:
    """Split MCP content blocks into text plus attachments.

    The text is joined exactly as the previous implementation did, so a result
    made only of text blocks is unchanged.
    """

    texts: list[str] = []
    attachments: list[Attachment] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == TEXT:
            texts.append(str(block.get("text", "")))
        elif kind == IMAGE:
            attachments.append(_store_image(block, tool_name))
        elif kind == AUDIO:
            attachments.append(Attachment(
                type=AUDIO,
                mime_type=str(block.get("mimeType") or ""),
                note="not stored: audio content is not supported yet",
            ))
        elif kind == RESOURCE:
            resource = block.get("resource") or {}
            uri = str(resource.get("uri") or "")
            mime_type = str(resource.get("mimeType") or "")
            if isinstance(resource.get("text"), str):
                # An embedded text resource is still text.
                texts.append(resource["text"])
            else:
                attachments.append(Attachment(
                    type=RESOURCE, mime_type=mime_type, uri=uri,
                    note="" if uri else "no uri",
                ))

    return ToolContent(text="\n".join(texts), attachments=attachments)


def capability_report(content: ToolContent, *, model_supports_images: bool = False) -> dict:
    """What the caller is able to hand to the model, stated explicitly."""

    images = [item for item in content.attachments if item.type == IMAGE]
    unsupported = []
    if images and not model_supports_images:
        unsupported.append("image")
    unsupported.extend(item.type for item in content.attachments if item.type == AUDIO)
    return {
        "text": True,
        "images": model_supports_images and bool(images),
        "attachments": [item.to_dict() for item in content.attachments],
        "unsupported": sorted(set(unsupported)),
    }
