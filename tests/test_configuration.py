from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agents import main as cli
from agents.config import load_env_file, resolve_agent_options, resolve_api_config
from agents.web import WebApp


class ConfigurationTests(unittest.TestCase):
    def test_provider_selection_and_key_precedence(self):
        cases = [
            ({}, None, (None, None, False)),
            ({"APIKEY": " generic "}, None, (None, "generic", False)),
            ({"OPENAI_API_KEY": "openai", "ANTHROPIC_API_KEY": "anthropic"}, None,
             (None, "anthropic", False)),
            ({"OPENAI_BASE_URL": "https://proxy.test/custom", "ANTHROPIC_API_KEY": "anthropic"}, None,
             ("https://proxy.test/custom", "anthropic", True)),
            ({"ANTHROPIC_BASE_URL": "https://proxy.test/custom", "OPENAI_API_KEY": "openai"}, None,
             ("https://proxy.test/custom", "openai", False)),
            ({"API": "https://proxy.test/anthropic/v1", "OPENAI_API_KEY": "openai",
              "ANTHROPIC_API_KEY": "anthropic"}, None,
             ("https://proxy.test/anthropic/v1", "anthropic", False)),
            ({"API": "https://proxy.test/anthropic", "APIKEY": "generic",
              "OPENAI_API_KEY": "openai"}, " https://override.test/v1 ",
             ("https://override.test/v1", "generic", True)),
            ({"API": "https://api.anthropic.com/v1", "APIKEY": "generic"}, None,
             ("https://api.anthropic.com/v1", "generic", False)),
        ]
        for environment, override, expected in cases:
            with self.subTest(environment=environment, override=override), patch.dict(os.environ, environment, clear=True):
                self.assertEqual(resolve_api_config(override), expected)

    def test_cli_and_web_use_the_same_environment_defaults(self):
        environment = {
            "APIKEY": "fake-key", "API": "https://proxy.test/anthropic", "MODEL": "custom-model",
            "MAWOAGENT_INPUT_PRICE_PER_MILLION": "1.5", "MAWOAGENT_OUTPUT_PRICE_PER_MILLION": "4.5",
            "MAWOAGENT_SHUTDOWN_TIMEOUT": "7.5", "MAWOAGENT_CONTEXT_WINDOW": "64000",
            "MAWOAGENT_SKILL_CONCURRENCY": "3", "MAWOAGENT_SKILL_QUEUE_LIMIT": "8",
        }
        with patch.dict(os.environ, environment, clear=True), \
                patch("sys.argv", ["mawoagent"]), patch("agents.main._load_env_file"), \
                patch("agents.main.Agent") as agent, patch("agents.main.run_repl", new=AsyncMock()):
            cli.main()
            app = WebApp()
            try:
                expected = {
                    "model": "custom-model", "api_key": "fake-key", "api_base": None,
                    "anthropic_base_url": "https://proxy.test/anthropic", "api_protocol": "anthropic",
                    "input_price_per_million": 1.5, "output_price_per_million": 4.5,
                    "shutdown_timeout": 7.5, "context_window": 64000,
                    "background_skill_concurrency": 3, "background_skill_queue_limit": 8,
                }
                self.assertEqual(app.options, expected)
                self.assertEqual({key: agent.call_args.kwargs[key] for key in expected}, expected)
            finally:
                app.close()

    def test_cli_overrides_environment_and_accepts_zero_prices(self):
        environment = {
            "APIKEY": "fake-key", "API": "https://proxy.test/anthropic", "MODEL": "env-model",
            "MAWOAGENT_INPUT_PRICE_PER_MILLION": "invalid", "MAWOAGENT_OUTPUT_PRICE_PER_MILLION": "invalid",
            "MAWOAGENT_CONTEXT_WINDOW": "invalid",
        }
        arguments = ["mawoagent", "--model", "cli-model", "--api-base", "https://override.test/v1",
                     "--input-price", "0", "--output-price", "0", "--context-window", "32000"]
        with patch.dict(os.environ, environment, clear=True), \
                patch("sys.argv", arguments), patch("agents.main._load_env_file"), \
                patch("agents.main.Agent") as agent, patch("agents.main.run_repl", new=AsyncMock()):
            cli.main()
        options = agent.call_args.kwargs
        self.assertEqual(options["model"], "cli-model")
        self.assertEqual(options["api_base"], "https://override.test/v1")
        self.assertIsNone(options["anthropic_base_url"])
        self.assertEqual(options["api_protocol"], "openai")
        self.assertEqual(options["input_price_per_million"], 0)
        self.assertEqual(options["output_price_per_million"], 0)
        self.assertEqual(options["context_window"], 32000)

    def test_empty_model_configuration_preserves_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            defaults = resolve_agent_options()
            self.assertEqual(defaults["model"], "deepseek-chat")
            self.assertEqual(defaults["input_price_per_million"], 3)
            self.assertEqual(defaults["output_price_per_million"], 15)
            self.assertEqual(defaults["shutdown_timeout"], 5)
            self.assertEqual(defaults["background_skill_concurrency"], 2)
            self.assertEqual(defaults["background_skill_queue_limit"], 16)
            self.assertIsNone(defaults["context_window"])
        with patch.dict(os.environ, {"MODEL": "", "MAWOAGENT_CONTEXT_WINDOW": "128000"}, clear=True):
            options = resolve_agent_options(model="", context_window=0)
            self.assertEqual(options["model"], "deepseek-chat")
            self.assertEqual(options["context_window"], 128000)

    def test_explicit_web_options_skip_environment_defaults(self):
        options = {"model": "explicit", "api_key": "fake"}
        with patch.dict(os.environ, {"MAWOAGENT_SHUTDOWN_TIMEOUT": "invalid"}, clear=True):
            app = WebApp(options=options)
            try:
                self.assertIs(app.options, options)
            finally:
                app.close()

    def test_dotenv_does_not_replace_process_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("MODEL=file-model\nAPIKEY=file-key\n", encoding="utf-8")
            with patch.dict(os.environ, {"MODEL": "process-model"}, clear=True), \
                    patch("agents.config.find_dotenv", return_value=str(path)):
                load_env_file()
                self.assertEqual(os.environ["MODEL"], "process-model")
                self.assertEqual(os.environ["APIKEY"], "file-key")


if __name__ == "__main__":
    unittest.main()
