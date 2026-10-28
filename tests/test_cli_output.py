"""Structured CLI output, exit codes and stream separation (I7 / T19)."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agents import ui
from agents.output import (
    EXIT_BUDGET,
    EXIT_CANCELLED,
    EXIT_ERROR,
    EXIT_OK,
    ResultWriter,
    classify_error,
)

REPO = Path(__file__).resolve().parents[1]


class WriterTests(unittest.TestCase):
    def writer(self, fmt: str) -> tuple[ResultWriter, io.StringIO]:
        stream = io.StringIO()
        return ResultWriter(fmt, stream=stream), stream

    def test_jsonl_writes_one_object_per_line_and_ends_with_result(self) -> None:
        writer, stream = self.writer("jsonl")
        writer.start(prompt="hi", model="m")
        writer.finish(status="ok", exit_code=EXIT_OK, text="hello")

        lines = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([line["type"] for line in lines], ["start", "result"])
        self.assertEqual(lines[-1]["status"], "ok")
        self.assertEqual(lines[-1]["text"], "hello")

    def test_json_writes_a_single_document_with_events(self) -> None:
        writer, stream = self.writer("json")
        writer.start(prompt="hi", model="m")
        writer.finish(status="error", exit_code=EXIT_ERROR, error="boom")

        document = json.loads(stream.getvalue())
        self.assertEqual(document["status"], "error")
        self.assertEqual(document["error"], "boom")
        self.assertEqual([event["type"] for event in document["events"]], ["start"])

    def test_text_mode_writes_nothing(self) -> None:
        writer, stream = self.writer("text")
        writer.start(prompt="hi", model="m")
        code = writer.finish(status="ok", exit_code=EXIT_OK)
        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(code, EXIT_OK)

    def test_unknown_format_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ResultWriter("yaml", stream=io.StringIO())

    def test_usage_is_carried_through(self) -> None:
        writer, stream = self.writer("jsonl")
        writer.finish(status="ok", exit_code=0, usage={"input_tokens": 3})
        self.assertEqual(json.loads(stream.getvalue())["usage"]["input_tokens"], 3)

    def test_finish_returns_the_exit_code_it_was_given(self) -> None:
        writer, _ = self.writer("json")
        self.assertEqual(writer.finish(status="budget_exceeded", exit_code=EXIT_BUDGET), EXIT_BUDGET)


class ClassificationTests(unittest.TestCase):
    def test_budget_exhaustion_is_not_a_generic_error(self) -> None:
        status, code = classify_error(RuntimeError("Model request blocked: Cost limit reached"))
        self.assertEqual((status, code), ("budget_exceeded", EXIT_BUDGET))

    def test_other_failures_are_errors(self) -> None:
        self.assertEqual(classify_error(RuntimeError("boom")), ("error", EXIT_ERROR))

    def test_interrupts_are_cancellations(self) -> None:
        self.assertEqual(classify_error(KeyboardInterrupt())[1], EXIT_CANCELLED)

    def test_asyncio_cancellation_is_a_cancellation(self) -> None:
        self.assertEqual(classify_error(asyncio.CancelledError())[1], EXIT_CANCELLED)


class StreamTests(unittest.TestCase):
    def tearDown(self) -> None:
        ui.configure_stream(stderr=False)
        ui.set_text_sink(None)

    def test_assistant_text_moves_to_stderr(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout, stderr
        try:
            ui.configure_stream(stderr=True)
            ui.print_assistant_text("hello")
        finally:
            sys.stdout, sys.stderr = old_out, old_err

        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("hello", stderr.getvalue())

    def test_sink_sees_the_text_without_swallowing_it(self) -> None:
        seen: list[str] = []
        stdout = io.StringIO()
        old_out = sys.stdout
        sys.stdout = stdout
        try:
            ui.configure_stream(stderr=False)
            ui.set_text_sink(seen.append)
            ui.print_assistant_text("abc")
        finally:
            sys.stdout = old_out

        self.assertEqual(seen, ["abc"])
        self.assertEqual(stdout.getvalue(), "abc")

    def test_a_broken_sink_does_not_break_output(self) -> None:
        stdout = io.StringIO()
        old_out = sys.stdout
        sys.stdout = stdout
        try:
            ui.configure_stream(stderr=False)
            ui.set_text_sink(lambda text: 1 / 0)
            ui.print_assistant_text("still here")
        finally:
            sys.stdout = old_out

        self.assertEqual(stdout.getvalue(), "still here")


class CliTests(unittest.TestCase):
    def run_cli(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items()
               if key not in {"APIKEY", "API", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                              "OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL"}}
        env["PYTHONPATH"] = str(REPO)
        env["PYTHONIOENCODING"] = "utf-8"
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [sys.executable, "-m", "agents.main", *args],
                cwd=cwd or tmp, env=env, capture_output=True, timeout=120,
            )
        # Decode here: the console encoding differs per platform, and a test about
        # streams should not fail on how the OS spells a box-drawing character.
        return subprocess.CompletedProcess(
            completed.args, completed.returncode,
            (completed.stdout or b"").decode("utf-8", "replace"),
            (completed.stderr or b"").decode("utf-8", "replace"),
        )

    def test_structured_output_requires_a_prompt(self) -> None:
        result = self.run_cli("--json")
        self.assertEqual(result.returncode, 2)

    def test_missing_key_is_reported_as_a_result_record(self) -> None:
        result = self.run_cli("--json", "hello")
        self.assertEqual(result.returncode, 1)
        records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(records[-1]["type"], "result")
        self.assertEqual(records[-1]["status"], "error")
        self.assertIn("API key", records[-1]["error"])

    def test_missing_key_in_text_mode_stays_human_readable(self) -> None:
        result = self.run_cli("hello")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('"type"', result.stdout)


if __name__ == "__main__":
    unittest.main()
