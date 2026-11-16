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

from fyrn.runner import (MAX_GENERATION_TOKENS, FakeRunner,
                               GenerationCancelled, LoadCancelled,
                               LlamaRunner, RunnerError,
                               auto_context_tokens, auto_threads,
                               compute_tokens_per_sec, trim_history)

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
                         verbose, chat_handler=None):
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


class TrimHistoryTests(unittest.TestCase):
    def _turns(self, count, size=10):
        turns = []
        for index in range(count):
            turns.append({"role": "user",
                          "content": "u%d" % index + "x" * size})
            turns.append({"role": "assistant",
                          "content": "a%d" % index + "y" * size})
        return turns

    def test_under_budget_is_unchanged(self):
        history = self._turns(2)
        self.assertEqual(trim_history(history, 1000), history)

    def test_over_budget_drops_oldest_first(self):
        history = self._turns(3, size=100)
        trimmed = trim_history(history, 250)
        # The newest exchange survives; the oldest goes first.
        self.assertEqual(trimmed[-1]["content"][:2], "a2")
        self.assertNotIn(history[0], trimmed)

    def test_newest_exchange_always_kept(self):
        history = self._turns(1, size=500)
        self.assertEqual(trim_history(history, 10), history)

    def test_empty_stays_empty(self):
        self.assertEqual(trim_history([]), [])


class HistoryFlowTests(unittest.TestCase):
    def test_fake_runner_receives_history(self):
        runner = FakeRunner(reply_text="ok")
        runner.load("/tmp/fake.gguf")
        history = [{"role": "user", "content": "first"},
                   {"role": "assistant", "content": "ok"}]
        runner.generate("second", history=history)
        self.assertEqual(runner.last_history, history)

    def test_fake_runner_without_history_gets_empty(self):
        runner = FakeRunner(reply_text="ok")
        runner.load("/tmp/fake.gguf")
        runner.generate("hello")
        self.assertEqual(runner.last_history, [])


class CancellableLoadTests(unittest.TestCase):
    def test_fake_load_can_be_cancelled(self):
        self.assertTrue(FakeRunner().cancellable_load)

    def test_real_load_cannot_be_cancelled(self):
        # The Llama() constructor never checks for a cancel, so
        # the UI must not promise one.
        self.assertFalse(LlamaRunner().cancellable_load)


class FakeChatLlama:
    """A stand-in for llama_cpp.Llama that records its calls."""

    CREATED = []

    def __init__(self, model_path, n_threads, n_ctx, verbose,
                 chat_handler=None):
        self.calls = []
        self.chat_handler = chat_handler
        FakeChatLlama.CREATED.append(self)

    def create_chat_completion(self, messages, stream,
                               max_tokens=None):
        self.calls.append((list(messages), max_tokens))

        def chunks():
            yield {"choices": [{"delta": {"content": "hi"}}]}
            yield {"choices": [{"delta": {"content": " there"}}]}

        return chunks()

    def tokenize(self, data):
        return [1, 2, 3]


class LlamaGenerateTests(unittest.TestCase):
    def _make_runner(self):
        FakeChatLlama.CREATED.clear()
        module = types.ModuleType("llama_cpp")
        module.Llama = FakeChatLlama
        runner = LlamaRunner()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "small.gguf"
            path.write_bytes(b"x" * 2048)
            with mock.patch.dict(sys.modules,
                                 {"llama_cpp": module}):
                runner.load(str(path))
        return runner

    def test_history_is_sent_with_the_prompt(self):
        runner = self._make_runner()
        history = [{"role": "user", "content": "my name is Jo"},
                   {"role": "assistant", "content": "hello Jo"}]
        pieces = []
        stats = runner.generate("what is my name?",
                                on_token=pieces.append,
                                history=history)
        self.assertEqual("".join(pieces), "hi there")
        self.assertEqual(stats["tokens"], 3)
        llama = FakeChatLlama.CREATED[0]
        self.assertEqual(len(llama.calls), 1)
        messages, _max_tokens = llama.calls[0]
        self.assertEqual(messages, history + [
            {"role": "user", "content": "what is my name?"}])

    def test_generation_is_capped(self):
        runner = self._make_runner()
        runner.generate("hello")
        _messages, max_tokens = FakeChatLlama.CREATED[0].calls[0]
        self.assertEqual(max_tokens, MAX_GENERATION_TOKENS)
        self.assertEqual(MAX_GENERATION_TOKENS, 1024)


class FakeMTMDChatHandler:
    """A stand-in for llama_cpp's MTMDChatHandler: records how
    it was built (CPU only, never the GPU)."""

    CREATED = []
    FAIL = False

    def __init__(self, clip_model_path, verbose=True, use_gpu=True):
        if FakeMTMDChatHandler.FAIL:
            raise RuntimeError("projector is broken")
        self.clip_model_path = clip_model_path
        self.verbose = verbose
        self.use_gpu = use_gpu
        FakeMTMDChatHandler.CREATED.append(self)


class VisionTests(unittest.TestCase):
    """Vision, with the engine's vision path mocked: no real
    vision model exists in the sandbox, so these tests prove the
    wiring (projector found, handler built CPU-only, images sent
    as data: URLs), not that the engine sees."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        FakeChatLlama.CREATED.clear()
        FakeMTMDChatHandler.CREATED.clear()
        FakeMTMDChatHandler.FAIL = False
        self.picture = self.folder / "photo.png"
        self.picture.write_bytes(b"\x89PNG fake image bytes")

    def tearDown(self):
        self.tmp.cleanup()

    def _modules(self):
        module = types.ModuleType("llama_cpp")
        module.Llama = FakeChatLlama
        chat_format = types.ModuleType("llama_cpp.llama_chat_format")
        chat_format.MTMDChatHandler = FakeMTMDChatHandler
        return {"llama_cpp": module,
                "llama_cpp.llama_chat_format": chat_format}

    def _model(self, name="model.gguf", projector=None):
        path = self.folder / name
        path.write_bytes(b"x" * 2048)
        if projector is not None:
            (self.folder / projector).write_bytes(b"p" * 100)
        return path

    def _load(self, path):
        runner = LlamaRunner()
        with mock.patch.dict(sys.modules, self._modules()):
            result = runner.load(str(path))
        return runner, result

    def test_fake_runner_records_images(self):
        runner = FakeRunner(supports_vision=True)
        runner.load("model.gguf")
        runner.generate("what is this?",
                        images=[str(self.picture)])
        self.assertEqual(runner.last_images, [str(self.picture)])

    def test_fake_runner_refuses_images_without_vision(self):
        runner = FakeRunner()  # supports_vision=False
        runner.load("model.gguf")
        with self.assertRaises(Exception) as ctx:
            runner.generate("what is this?",
                            images=[str(self.picture)])
        self.assertIn("cannot see images", str(ctx.exception))

    def test_projector_loads_vision_cpu_only(self):
        path = self._model(projector="mmproj-model.gguf")
        runner, result = self._load(path)
        self.assertTrue(runner.supports_vision)
        self.assertEqual(runner.projector_path,
                         str(self.folder / "mmproj-model.gguf"))
        self.assertNotIn("vision_warning", result)
        handler = FakeMTMDChatHandler.CREATED[0]
        self.assertEqual(handler.clip_model_path,
                         str(self.folder / "mmproj-model.gguf"))
        self.assertFalse(handler.use_gpu)
        self.assertFalse(handler.verbose)
        self.assertIs(FakeChatLlama.CREATED[0].chat_handler, handler)

    def test_broken_projector_loads_text_only(self):
        FakeMTMDChatHandler.FAIL = True
        path = self._model(projector="mmproj-model.gguf")
        runner, result = self._load(path)
        # The load still succeeds: a projector that cannot be used
        # never blocks the model itself.
        self.assertTrue(runner.is_loaded)
        self.assertFalse(runner.supports_vision)
        self.assertIsNone(runner.projector_path)
        self.assertIn("projector file could not be used",
                      result["vision_warning"])

    def test_vision_model_without_projector_warns(self):
        from helpers import write_gguf_file
        path = write_gguf_file(
            self.folder / "qwen3vl.gguf",
            {"general.architecture": "qwen3vl"})
        runner, result = self._load(path)
        self.assertTrue(runner.is_loaded)
        self.assertFalse(runner.supports_vision)
        self.assertIn("projector file is not next to it",
                      result["vision_warning"])

    def test_text_model_loads_without_warning(self):
        path = self._model()
        _runner, result = self._load(path)
        self.assertNotIn("vision_warning", result)

    def test_images_sent_as_data_urls(self):
        path = self._model(projector="mmproj-model.gguf")
        runner, _result = self._load(path)
        runner.generate("what is in this picture?",
                        images=[str(self.picture)])
        llama = FakeChatLlama.CREATED[0]
        messages, _max_tokens = llama.calls[0]
        user_message = messages[-1]
        self.assertEqual(user_message["role"], "user")
        parts = user_message["content"]
        self.assertEqual(parts[0],
                         {"type": "text",
                          "text": "what is in this picture?"})
        self.assertEqual(parts[1]["type"], "image_url")
        url = parts[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        import base64
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]),
                         b"\x89PNG fake image bytes")

    def test_images_without_vision_refused(self):
        path = self._model()
        runner, _result = self._load(path)
        with self.assertRaises(Exception) as ctx:
            runner.generate("what is this?",
                            images=[str(self.picture)])
        self.assertIn("cannot see images", str(ctx.exception))

    def test_bad_image_type_refused(self):
        path = self._model(projector="mmproj-model.gguf")
        runner, _result = self._load(path)
        gif = self.folder / "anim.gif"
        gif.write_bytes(b"GIF89a")
        with self.assertRaises(Exception) as ctx:
            runner.generate("what is this?", images=[str(gif)])
        self.assertIn("PNG or JPEG", str(ctx.exception))

    def test_unreadable_image_refused(self):
        path = self._model(projector="mmproj-model.gguf")
        runner, _result = self._load(path)
        with self.assertRaises(Exception) as ctx:
            runner.generate(
                "what is this?",
                images=[str(self.folder / "gone.png")])
        self.assertIn("could not be read", str(ctx.exception))

    def test_unload_clears_vision(self):
        path = self._model(projector="mmproj-model.gguf")
        runner, _result = self._load(path)
        self.assertTrue(runner.supports_vision)
        runner.unload()
        self.assertFalse(runner.supports_vision)
        self.assertIsNone(runner.projector_path)


if __name__ == "__main__":
    unittest.main()
