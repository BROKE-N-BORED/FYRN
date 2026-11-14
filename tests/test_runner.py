"""Tests for fyrn.runner: the engine interface, the fake
runner with known timing, and the real runner's failure paths."""

import importlib.util
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from fyrn.runner import (FakeRunner, GenerationCancelled,
                               LoadCancelled, LlamaRunner, RunnerError,
                               auto_context_tokens, auto_threads,
                               compute_tokens_per_sec)

GB = 1024 ** 3


class TokensPerSecTests(unittest.TestCase):
    def test_exact_math(self):
        self.assertEqual(compute_tokens_per_sec(200, 10.0), 20.0)
        self.assertEqual(compute_tokens_per_sec(100, 10.0), 10.0)
        self.assertEqual(compute_tokens_per_sec(50, 10.0), 5.0)

    def test_zero_or_missing_elapsed(self):
        self.assertEqual(compute_tokens_per_sec(10, 0), 0.0)
        self.assertEqual(compute_tokens_per_sec(10, None), 0.0)
        self.assertEqual(compute_tokens_per_sec(0, 5.0), 0.0)


class AutoConfigTests(unittest.TestCase):
    def test_threads_at_least_one(self):
        self.assertGreaterEqual(auto_threads(), 1)

    def test_context_roomy_vs_tight(self):
        self.assertEqual(auto_context_tokens(8 * GB), 4096)
        self.assertEqual(auto_context_tokens(6 * GB), 4096)
        self.assertEqual(auto_context_tokens(2 * GB), 2048)
        self.assertEqual(auto_context_tokens(None), 2048)


class FakeRunnerLoadTests(unittest.TestCase):
    def test_load_reports_progress_to_100(self):
        runner = FakeRunner()
        seen = []
        result = runner.load(
            "/tmp/fake.gguf",
            progress_cb=lambda pct, msg: seen.append(pct))
        self.assertTrue(runner.is_loaded)
        self.assertEqual(runner.model_name, "fake.gguf")
        self.assertEqual(seen[0], 0)
        self.assertEqual(seen[-1], 100)
        self.assertGreaterEqual(result["load_time_s"], 0.0)

    def test_load_delay_is_known(self):
        runner = FakeRunner(load_delay_s=0.2)
        started = time.monotonic()
        runner.load("/tmp/fake.gguf")
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.15)
        self.assertLess(elapsed, 1.0)

    def test_load_cancel(self):
        runner = FakeRunner(load_delay_s=1.0)
        event = threading.Event()
        event.set()
        with self.assertRaises(LoadCancelled):
            runner.load("/tmp/fake.gguf", cancel_event=event)
        self.assertFalse(runner.is_loaded)

    def test_unload(self):
        runner = FakeRunner()
        runner.load("/tmp/fake.gguf")
        runner.unload()
        self.assertFalse(runner.is_loaded)
        self.assertEqual(runner.model_name, "")


class FakeRunnerGenerateTests(unittest.TestCase):
    def test_generate_without_load_fails(self):
        runner = FakeRunner()
        with self.assertRaises(RunnerError):
            runner.generate("hello")

    def test_generate_streams_words(self):
        runner = FakeRunner(reply_text="one two three")
        runner.load("/tmp/fake.gguf")
        pieces = []
        stats = runner.generate("hi", on_token=pieces.append)
        self.assertEqual("".join(pieces), "one two three")
        self.assertEqual(stats["tokens"], 3)
        self.assertGreater(stats["elapsed_s"], 0.0)
        self.assertGreaterEqual(stats["ttft_s"], 0.0)
        # tokens per second is exact from the measured numbers.
        self.assertAlmostEqual(
            stats["tokens_per_sec"],
            stats["tokens"] / stats["elapsed_s"])

    def test_generate_ttft_delay_is_known(self):
        runner = FakeRunner(reply_text="a b", ttft_delay_s=0.2,
                            token_delay_s=0.0)
        runner.load("/tmp/fake.gguf")
        started = time.monotonic()
        stats = runner.generate("hi")
        self.assertGreaterEqual(stats["ttft_s"], 0.15)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_generate_token_delay_and_cancel(self):
        runner = FakeRunner(reply_text="one two three four five",
                            token_delay_s=0.2)
        runner.load("/tmp/fake.gguf")
        event = threading.Event()

        def cancel_soon():
            time.sleep(0.25)
            event.set()

        thread = threading.Thread(target=cancel_soon)
        thread.start()
        with self.assertRaises(GenerationCancelled):
            runner.generate("hi", cancel_event=event)
        thread.join()


class LlamaRunnerTests(unittest.TestCase):
    def test_missing_backend_is_plain_words(self):
        if importlib.util.find_spec("llama_cpp") is not None:
            self.skipTest("llama_cpp is installed here")
        runner = LlamaRunner()
        with self.assertRaises(RunnerError) as ctx:
            runner.load("/tmp/fake.gguf")
        self.assertIn("not installed", str(ctx.exception))
        self.assertFalse(runner.is_loaded)

    def test_generate_without_load_fails(self):
        runner = LlamaRunner()
        with self.assertRaises(RunnerError):
            runner.generate("hello")

    def test_load_reports_no_fake_percent(self):
        # The real backend loads in one opaque step, so it must
        # never report a mid-load percent: -1 (indeterminate) with
        # honest stage text, then 100 when it is really loaded.
        # (An earlier version reported 10, so the bar sat at 10%
        # for the whole load and then jumped to 100%.)
        class FakeLlama:
            def __init__(self, model_path, n_threads, n_ctx,
                         verbose):
                time.sleep(0.05)  # a load takes a moment

        module = types.ModuleType("llama_cpp")
        module.Llama = FakeLlama
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "small.gguf"
            path.write_bytes(b"x" * 2048)
            seen = []
            runner = LlamaRunner()
            with mock.patch.dict(sys.modules,
                                 {"llama_cpp": module}):
                result = runner.load(
                    str(path),
                    progress_cb=lambda pct, msg: seen.append(
                        (pct, msg)))
            self.assertTrue(runner.is_loaded)
            self.assertGreaterEqual(result["load_time_s"], 0.0)
            self.assertTrue(seen)
            for pct, _msg in seen:
                self.assertIn(pct, (-1, 100),
                              "fake mid-load percent: %s" % pct)
            self.assertEqual(seen[0][0], -1)
            self.assertIn("small.gguf", seen[0][1])
            self.assertIn("2 KB", seen[0][1])
            self.assertEqual(seen[-1], (100, "Loaded"))


if __name__ == "__main__":
    unittest.main()
