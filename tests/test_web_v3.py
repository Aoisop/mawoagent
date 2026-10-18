"""Revisioned browser streams: sparse updates, rollback, and safe resync."""
import asyncio
from copy import deepcopy
import json
import http.client
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agents.web import Conversation, WebApp, WebServer, WEB_TEXT_LIMIT, WEB_TEXT_TRUNCATION


def payload(frame):
    return json.loads(next(line[6:] for line in frame.decode().splitlines() if line.startswith("data: ")))


class WebRevisionTests(unittest.TestCase):
    def view(self):
        return Conversation(SimpleNamespace(config={"model": "test"}), sid="conversation")

    def test_initial_snapshot_incremental_text_and_idle(self):
        view = self.view()
        revision, frames = view.stream_updates(-1)
        self.assertTrue(frames[0].startswith(b"event: state\n"))
        self.assertEqual(payload(frames[0]), view.snapshot)
        view.entries.append({"id": "answer", "kind": "message", "role": "assistant", "text": "hello"})
        view.changed()
        revision, frames = view.stream_updates(revision)
        self.assertEqual(payload(frames[0])["order"], ["answer"])
        view.entries[-1]["text"] += " world"
        view.changed()
        revision, frames = view.stream_updates(revision)
        delta = payload(frames[0])
        self.assertEqual(delta["entries"], [{"id": "answer", "set": {}, "append": {"text": " world"}}])
        self.assertEqual(view.stream_updates(revision), (revision, []))

    def test_large_history_is_shared_and_not_resent_for_one_token(self):
        view = self.view()
        view.entries = [{"id": str(i), "kind": "tool", "result": "x" * 16000} for i in range(499)]
        view.entries.append({"id": "answer", "kind": "message", "role": "assistant", "text": "hello"})
        view.changed()
        previous = view.snapshot
        view.entries[-1]["text"] += "!"
        view.changed()
        _, frames = view.stream_updates(previous["revision"])
        self.assertLess(sum(map(len, frames)), 300)
        self.assertIs(previous["entries"][0], view.snapshot["entries"][0])
        self.assertEqual(previous["entries"][-1]["text"], "hello")

    def test_retry_rollback_replaces_text_and_removes_failed_entries(self):
        view = self.view()
        original = {"id": "answer", "kind": "message", "role": "assistant", "text": "prefix"}
        view.entries = [deepcopy(original)]
        view.changed()
        view.entries[0]["text"] += " FAILED"
        view.entries.append({"id": "failed-thinking", "kind": "thinking", "text": "discard"})
        view.changed()
        revision = view.revision
        view.entries = [original]
        view.changed()
        delta = payload(view.stream_updates(revision)[1][0])
        self.assertEqual(delta["order"], ["answer"])
        self.assertEqual(delta["entries"][0]["set"], {"text": "prefix"})
        self.assertEqual(delta["entries"][0]["append"], {})

    def test_lagging_clients_and_reconnections_get_latest_snapshot(self):
        view = self.view()
        initial = view.revision
        with patch("agents.web.STREAM_EVENT_LIMIT", 3):
            for i in range(7):
                view.title = str(i)
                view.changed()
        self.assertEqual(len(view._events), 3)
        for revision in (-1, initial, view.revision + 10):
            current, frames = view.stream_updates(revision)
            self.assertEqual(current, view.revision)
            self.assertTrue(frames[0].startswith(b"event: state\n"))
            self.assertEqual(payload(frames[0]), view.snapshot)
        _, frames = view.stream_updates(view.revision - 2)
        self.assertEqual([payload(frame)["revision"] for frame in frames], [view.revision - 1, view.revision])

    def test_event_queue_has_byte_limit_and_can_resync_after_large_update(self):
        view = self.view()
        revision = view.revision
        with patch("agents.web.STREAM_BYTE_LIMIT", 100):
            view.entries.append({"id": "large", "text": "x" * 1000})
            view.changed()
        self.assertLessEqual(view._event_bytes, 100)
        self.assertTrue(view.stream_updates(revision)[1][0].startswith(b"event: state\n"))

    def test_text_preview_is_bounded_for_answers_and_thinking(self):
        async def run():
            for thinking in (False, True):
                view = self.view()
                emit = view.emit_thinking if thinking else view.emit_text
                emit("文" * (WEB_TEXT_LIMIT + 99))
                emit("later")
                view.changed()
                text = view.snapshot["entries"][0]["text"]
                self.assertEqual(text, "文" * WEB_TEXT_LIMIT + WEB_TEXT_TRUNCATION)
        asyncio.run(run())

    def test_http_snapshots_do_not_include_unpublished_stream_text(self):
        # Mixing live entries with an older revision would double-append the next delta.
        async def run():
            view = self.view()
            view.emit_text("published")
            view.changed()
            revision = view.revision
            view.emit_text("pending")
            app = object.__new__(WebApp)
            app.conversations = {view.id: view}
            response = await app.dispatch("GET", "/api/sessions/" + view.id)
            self.assertEqual(response["revision"], revision)
            self.assertEqual(response["entries"][0]["text"], "published")
            view.changed()
            delta = payload(view.stream_updates(revision)[1][0])
            self.assertEqual(delta["entries"][0]["append"], {"text": "pending"})
        asyncio.run(run())

    def test_metadata_updates_include_clearing_requests_and_errors(self):
        view = self.view()
        view.error = "failed"
        view.requests = {"a": {"id": "a", "future": object(), "message": "permission"}}
        view.changed()
        revision = view.revision
        view.error = None
        view.requests = {}
        view.changed()
        delta = payload(view.stream_updates(revision)[1][0])
        self.assertEqual(delta["changes"], {"error": None, "requests": []})


class WebHttpStreamTests(unittest.TestCase):
    def test_http_delta_heartbeat_and_reconnect_snapshot(self):
        app = WebApp(options={"api_key": None, "model": "test"})
        self.addCleanup(app.close)
        server = WebServer(("127.0.0.1", 0), app)
        self.addCleanup(server.server_close)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.shutdown)
        state = app.call(app.dispatch("POST", "/api/sessions", {}))
        view = app.conversations[state["id"]]
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        self.addCleanup(client.close)
        cookie = {"Cookie": "mawoagent_web=" + app.auth}
        client.request("GET", "/conversation-state.mjs", headers=cookie)
        response = client.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn(b"applyConversationDelta", response.read())

        def read_frame(response):
            lines = []
            while True:
                line = response.readline()
                if not line or line == b"\n":
                    return b"".join(lines)
                lines.append(line)

        with patch("agents.web.STREAM_HEARTBEAT_SECONDS", .02):
            client.request("GET", "/api/events?session=" + view.id, headers=cookie)
            response = client.getresponse()
            initial = read_frame(response)
            self.assertTrue(initial.startswith(b"event: state\n"))
            self.assertIn(f"id: {view.id}:{view.revision}".encode(), initial)
            initial_revision = view.revision

            async def change():
                view.entries.append({"id": "answer", "kind": "message", "role": "assistant", "text": "new"})
                view.changed()
            app.call(change())
            frame = read_frame(response)
            while frame.startswith(b":"):
                frame = read_frame(response)
            self.assertTrue(frame.startswith(b"event: delta\n"))
            self.assertEqual(payload(frame)["baseRevision"], initial_revision)
            self.assertEqual(read_frame(response), b": heartbeat\n")
            response.close()
            client.close()

            # EventSource reconnects with Last-Event-ID; the complete current
            # snapshot repairs any revisions lost while the browser was offline.
            client.request("GET", "/api/events?session=" + view.id,
                           headers={**cookie, "Last-Event-ID": f"{view.id}:{initial_revision}"})
            response = client.getresponse()
            restored = read_frame(response)
            self.assertTrue(restored.startswith(b"event: state\n"))
            self.assertEqual(payload(restored), view.snapshot)
            response.close()


if __name__ == "__main__":
    unittest.main()
