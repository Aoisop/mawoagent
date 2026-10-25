"""Web access from a container: bind address, host allow-list, assets (I4 / T12)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from agents.web import allowed_hosts, web_bind_host

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


class HostPolicyTests(unittest.TestCase):
    def test_only_loopback_is_allowed_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAWOAGENT_WEB_ALLOWED_HOSTS", None)
            hosts = allowed_hosts(8765)
        self.assertEqual(hosts, {"127.0.0.1:8765", "localhost:8765"})

    def test_published_container_port_is_added_explicitly(self) -> None:
        with mock.patch.dict(os.environ, {"MAWOAGENT_WEB_ALLOWED_HOSTS": "localhost:9000, 10.0.0.5:8765"}):
            hosts = allowed_hosts(8765)
        self.assertIn("localhost:9000", hosts)
        self.assertIn("10.0.0.5:8765", hosts)
        self.assertIn("127.0.0.1:8765", hosts)

    def test_blank_entries_do_not_create_an_empty_host(self) -> None:
        with mock.patch.dict(os.environ, {"MAWOAGENT_WEB_ALLOWED_HOSTS": " , "}):
            self.assertEqual(len(allowed_hosts(1)), 2)

    def test_bind_host_defaults_to_loopback(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAWOAGENT_WEB_HOST", None)
            self.assertEqual(web_bind_host(), "127.0.0.1")

    def test_bind_host_can_be_opened_for_a_container(self) -> None:
        with mock.patch.dict(os.environ, {"MAWOAGENT_WEB_HOST": "0.0.0.0"}):
            self.assertEqual(web_bind_host(), "0.0.0.0")


class DockerfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = DOCKERFILE.read_text(encoding="utf-8")

    def test_web_assets_are_copied(self) -> None:
        # Without this the workbench answers 404 inside the image.
        self.assertIn("COPY web ./web", self.text)

    def test_port_is_declared(self) -> None:
        self.assertIn("EXPOSE 8765", self.text)

    def test_container_host_settings_are_documented(self) -> None:
        self.assertIn("MAWOAGENT_WEB_HOST=0.0.0.0", self.text)
        self.assertIn("MAWOAGENT_WEB_ALLOWED_HOSTS", self.text)


if __name__ == "__main__":
    unittest.main()
