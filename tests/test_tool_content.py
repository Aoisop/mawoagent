"""Typed tool results: text compatibility and non-text references (I5 / T15)."""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents.tool_content import (
    MAX_ATTACHMENT_BYTES,
    Attachment,
    capability_report,
    from_mcp_content,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload" * 8


def png_block(data: bytes = PNG_BYTES, mime: str = "image/png") -> dict:
    return {"type": "image", "mimeType": mime, "data": base64.b64encode(data).decode()}


class HomeIsolated(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)


class TextCompatibilityTests(HomeIsolated):
    def test_text_blocks_are_joined_exactly_as_before(self) -> None:
        content = from_mcp_content([{"type": "text", "text": "one"}, {"type": "text", "text": "two"}])
        self.assertEqual(content.text, "one\ntwo")
        self.assertEqual(content.attachments, [])
        self.assertEqual(content.render(), "one\ntwo")

    def test_empty_text_block_is_preserved(self) -> None:
        content = from_mcp_content([{"type": "text", "text": ""}, {"type": "text", "text": "x"}])
        self.assertEqual(content.text, "\nx")

    def test_non_dict_blocks_are_ignored(self) -> None:
        content = from_mcp_content(["junk", None, {"type": "text", "text": "ok"}])
        self.assertEqual(content.text, "ok")

    def test_render_matches_text_when_nothing_is_attached(self) -> None:
        content = from_mcp_content([{"type": "text", "text": "body"}])
        self.assertEqual(content.render(), content.text)


class ImageAttachmentTests(HomeIsolated):
    def test_image_is_written_once_and_referenced_by_path(self) -> None:
        content = from_mcp_content([png_block()], "screenshot")
        self.assertEqual(len(content.attachments), 1)
        attachment = content.attachments[0]
        self.assertEqual(attachment.type, "image")
        self.assertEqual(attachment.mime_type, "image/png")
        self.assertEqual(attachment.size, len(PNG_BYTES))
        self.assertTrue(Path(attachment.uri).is_file())
        self.assertEqual(Path(attachment.uri).read_bytes(), PNG_BYTES)

    def test_payload_never_enters_the_rendered_text(self) -> None:
        content = from_mcp_content([{"type": "text", "text": "here"}, png_block()], "screenshot")
        rendered = content.render()
        self.assertIn("here", rendered)
        self.assertIn("image/png", rendered)
        self.assertNotIn(base64.b64encode(PNG_BYTES).decode()[:40], rendered)

    def test_data_url_prefix_is_accepted(self) -> None:
        block = {"type": "image", "data": "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()}
        content = from_mcp_content([block], "shot")
        self.assertEqual(content.attachments[0].size, len(PNG_BYTES))

    def test_invalid_base64_is_reported_not_stored(self) -> None:
        content = from_mcp_content([{"type": "image", "mimeType": "image/png", "data": "@@@"}], "shot")
        attachment = content.attachments[0]
        self.assertEqual(attachment.uri, "")
        self.assertIn("invalid base64", attachment.note)

    def test_oversized_payload_is_not_written(self) -> None:
        block = png_block(b"x" * (MAX_ATTACHMENT_BYTES + 1))
        content = from_mcp_content([block], "shot")
        attachment = content.attachments[0]
        self.assertEqual(attachment.uri, "")
        self.assertIn("larger than the attachment limit", attachment.note)


class OtherContentTests(HomeIsolated):
    def test_audio_is_reported_as_unsupported(self) -> None:
        content = from_mcp_content([{"type": "audio", "mimeType": "audio/wav", "data": "AAAA"}], "rec")
        self.assertEqual(content.attachments[0].type, "audio")
        self.assertIn("not supported", content.attachments[0].note)

    def test_text_resource_is_still_text(self) -> None:
        block = {"type": "resource", "resource": {"uri": "file:///a.txt", "text": "inside"}}
        content = from_mcp_content([block])
        self.assertEqual(content.text, "inside")
        self.assertEqual(content.attachments, [])

    def test_link_resource_keeps_its_uri(self) -> None:
        block = {"type": "resource", "resource": {"uri": "https://example.test/x", "mimeType": "text/html"}}
        content = from_mcp_content([block])
        self.assertEqual(content.attachments[0].uri, "https://example.test/x")
        self.assertIn("https://example.test/x", content.render())


class CapabilityTests(HomeIsolated):
    def test_images_are_unsupported_unless_the_model_takes_them(self) -> None:
        content = from_mcp_content([png_block()], "shot")
        report = capability_report(content)
        self.assertFalse(report["images"])
        self.assertIn("image", report["unsupported"])
        self.assertEqual(len(report["attachments"]), 1)

    def test_images_become_supported_when_declared(self) -> None:
        content = from_mcp_content([png_block()], "shot")
        report = capability_report(content, model_supports_images=True)
        self.assertTrue(report["images"])
        self.assertEqual(report["unsupported"], [])

    def test_text_only_result_has_no_unsupported_kinds(self) -> None:
        report = capability_report(from_mcp_content([{"type": "text", "text": "x"}]))
        self.assertEqual(report["unsupported"], [])
        self.assertEqual(report["attachments"], [])

    def test_description_carries_size_and_location(self) -> None:
        text = Attachment(type="image", mime_type="image/png", uri="/tmp/a.png", size=2048).describe()
        self.assertIn("image/png", text)
        self.assertIn("2.0 KB", text)
        self.assertIn("/tmp/a.png", text)


class McpCallTests(HomeIsolated):
    """The MCP client must describe attachments instead of inlining them."""

    @staticmethod
    def _connection(payload: dict):
        from agents.mcp_client import McpConnection

        client = McpConnection.__new__(McpConnection)

        async def send(method, params):
            return payload

        client._send_request = send
        return client

    def test_text_result_is_unchanged(self) -> None:
        text = asyncio.run(self._connection(
            {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        ).call_tool("x", {}))
        self.assertEqual(text, "a\nb")

    def test_image_result_is_a_reference_line(self) -> None:
        text = asyncio.run(self._connection({"content": [png_block()]}).call_tool("shot", {}))
        self.assertIn("image/png", text)
        self.assertNotIn(base64.b64encode(PNG_BYTES).decode()[:40], text)
        self.assertIn("not displayable in this runtime: image", text)

    def test_unknown_blocks_are_counted_not_dumped(self) -> None:
        text = asyncio.run(self._connection(
            {"content": [{"type": "something-new", "blob": "x"}]}
        ).call_tool("x", {}))
        self.assertIn("1 content block(s)", text)
        self.assertNotIn("blob", text)

    def test_structured_content_is_still_appended(self) -> None:
        text = asyncio.run(self._connection(
            {"content": [{"type": "text", "text": "a"}], "structuredContent": {"k": 1}}
        ).call_tool("x", {}))
        self.assertEqual(text, 'a\n{"k": 1}')


if __name__ == "__main__":
    unittest.main()
