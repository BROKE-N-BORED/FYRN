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
                               compute_tokens_per_sec, trim_history,
                               warm_engine)

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


class WarmEngineTests(unittest.TestCase):
    def test_warm_engine_imports_the_engine(self):
        if importlib.util.find_spec("llama_cpp") is None:
            self.skipTest("llama_cpp is not installed here")
        self.assertTrue(warm_engine())
        self.assertIn("llama_cpp", sys.modules)
        # Warming twice is harmless.
        self.assertTrue(warm_engine())

    def test_warm_engine_reports_a_missing_engine(self):
        saved = sys.modules.pop("llama_cpp", None)
        real_import = __import__

        def failing_import(name, *args, **kwargs):
            if name == "llama_cpp" or name.startswith("llama_cpp."):
                raise ImportError("No module named 'llama_cpp'")
            return real_import(name, *args, **kwargs)

        try:
            with mock.patch("builtins.__import__",
                            side_effect=failing_import):
                self.assertFalse(warm_engine())
        finally:
            if saved is not None:
                sys.modules["llama_cpp"] = saved

    def test_warm_engine_is_safe_from_a_thread(self):
        # The startup warm-up runs on a daemon thread: warming
        # from two threads at once must not raise or deadlock.
        if importlib.util.find_spec("llama_cpp") is None:
            self.skipTest("llama_cpp is not installed here")
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            warm_engine())) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, [True, True])


class EngineStartMessageTests(unittest.TestCase):
    """Task 2: the first load of a session says the engine is
    starting while the import runs, instead of claiming the
    model is loading."""

    def _fake_llama_module(self):
        class FakeLlama:
            def __init__(self, model_path, n_threads, n_ctx,
                         verbose, chat_handler=None):
                pass

        module = types.ModuleType("llama_cpp")
        module.Llama = FakeLlama
        return module

    def test_first_load_names_the_engine_start(self):
        saved = sys.modules.pop("llama_cpp", None)
        module = self._fake_llama_module()
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "llama_cpp" or name.startswith("llama_cpp."):
                return module
            return real_import(name, *args, **kwargs)

        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "small.gguf"
                path.write_bytes(b"x" * 2048)
                seen = []
                runner = LlamaRunner()
                with mock.patch("builtins.__import__",
                                side_effect=fake_import):
                    runner.load(
                        str(path),
                        progress_cb=lambda pct, msg: seen.append(
                            (pct, msg)))
        finally:
            if saved is not None:
                sys.modules["llama_cpp"] = saved
            else:
                sys.modules.pop("llama_cpp", None)
        self.assertTrue(runner.is_loaded)
        self.assertEqual(seen[0],
                         (-1, "Starting the model engine..."))
        self.assertIn("Loading small.gguf (2 KB) into RAM...",
                      [msg for _pct, msg in seen])

    def test_warmed_load_skips_the_engine_start(self):
        # When the engine is already imported (the startup
        # warm-up ran), the load goes straight to loading.
        module = self._fake_llama_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "small.gguf"
            path.write_bytes(b"x" * 2048)
            seen = []
            runner = LlamaRunner()
            with mock.patch.dict(sys.modules,
                                 {"llama_cpp": module}):
                runner.load(
                    str(path),
                    progress_cb=lambda pct, msg: seen.append(
                        (pct, msg)))
        self.assertNotIn("Starting the model engine...",
                         [msg for _pct, msg in seen])
        self.assertIn("Loading small.gguf (2 KB) into RAM...",
                      [msg for _pct, msg in seen])


class UnsupportedArchitectureTests(unittest.TestCase):
    """Task 8: an architecture the engine does not know is not
    a broken file. The engine fails with a bare "Failed to load
    model from file", and the architecture text only ever
    reaches C++ stderr. LFM3 was the real case until v0.7, when
    the engine bump made it load: the test now uses a synthetic
    architecture pinned into the unsupported set for the test,
    so the mechanism is covered without pinning a fake
    architecture in the product."""

    def _arch_file(self, tmp, arch, name):
        from helpers import write_gguf_file
        path = Path(tmp) / name
        write_gguf_file(path, {"general.architecture": arch})
        return path

    def _raising_module(self, message):
        class FailingLlama:
            def __init__(self, model_path, n_threads, n_ctx,
                         verbose, chat_handler=None):
                raise ValueError(message)

        module = types.ModuleType("llama_cpp")
        module.Llama = FailingLlama
        return module

    def test_unsupported_failure_names_the_design_not_the_file(
            self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._arch_file(tmp, "qwen99", "qwen99.gguf")
            module = self._raising_module(
                "Failed to load model from file: %s" % path)
            runner = LlamaRunner()
            with mock.patch.dict(sys.modules,
                                 {"llama_cpp": module}), \
                    mock.patch("fyrn.runner."
                               "UNSUPPORTED_ARCHITECTURES",
                               frozenset({"qwen99"})):
                with self.assertRaises(RunnerError) as ctx:
                    runner.load(str(path), display_name="Qwen99")
            text = str(ctx.exception)
            self.assertIn("QWEN99", text)
            self.assertIn("does not support yet", text)
            self.assertIn("file itself is fine", text)
            self.assertNotIn("may be broken", text)
            self.assertNotIn(str(path), text)
            self.assertIn("Qwen99", text)
            self.assertFalse(runner.is_loaded)

    def test_unknown_architecture_text_is_matched_too(self):
        # When the exception does carry the architecture text,
        # it is matched even for an architecture outside the
        # known-unsupported set.
        with tempfile.TemporaryDirectory() as tmp:
            from helpers import write_gguf_file
            path = Path(tmp) / "newarch.gguf"
            write_gguf_file(
                path, {"general.architecture": "qwen9"})
            module = self._raising_module(
                "unknown model architecture: 'qwen9'")
            runner = LlamaRunner()
            with mock.patch.dict(sys.modules,
                                 {"llama_cpp": module}):
                with self.assertRaises(RunnerError) as ctx:
                    runner.load(str(path))
            text = str(ctx.exception)
            self.assertIn("QWEN9", text)
            self.assertIn("does not support yet", text)
            self.assertNotIn("may be broken", text)

    def test_generic_failure_still_says_may_be_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            from helpers import write_gguf_file
            path = Path(tmp) / "plain.gguf"
            write_gguf_file(
                path, {"general.architecture": "llama"})
            module = self._raising_module("out of memory")
            runner = LlamaRunner()
            with mock.patch.dict(sys.modules,
                                 {"llama_cpp": module}):
                with self.assertRaises(RunnerError) as ctx:
                    runner.load(str(path))
            self.assertIn("may be broken", str(ctx.exception))

    def test_generic_failure_never_shows_the_blob_path(self):
        # Task 11: the exception's path is the blob filename for
        # an Ollama model, so it is replaced by the display name.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sha256-abcdef0123456789"
            path.write_bytes(b"x" * 2048)
            module = self._raising_module(
                "Failed to load model from file: %s" % path)
            runner = LlamaRunner()
            with mock.patch.dict(sys.modules,
                                 {"llama_cpp": module}):
                with self.assertRaises(RunnerError) as ctx:
                    runner.load(str(path), display_name="Tiny 1B")
            text = str(ctx.exception)
            self.assertNotIn("sha256-abcdef", text)
            self.assertIn("Tiny 1B", text)



# The real LFM3 blob, from the roster model store. The test
# below needs it, the 0.3.37 engine, and the opt-in flag: a
# real load takes minutes on a throttled sandbox, so it never
# runs in the default suite.
_LFM3_BLOB = (
    "/home/hatch/workspace/ollama/models/blobs/sha256-3793e1e6"
    "e2266919d6e015287221b41100175a98eddaba92e111aed3b05c93c4")


def _real_engine_available():
    """True when the real-engine test can run: opted in, the
    0.3.37 engine installed, and the roster blob present."""
    import os
    if os.environ.get("FYRN_REAL_ENGINE_TESTS") != "1":
        return False
    try:
        import llama_cpp
    except ImportError:
        return False
    try:
        major, minor, patch = (
            int(part) for part in
            llama_cpp.__version__.split(".")[:3])
    except (ValueError, AttributeError):
        return False
    if (major, minor, patch) < (0, 3, 37):
        return False
    return Path(_LFM3_BLOB).is_file()


class RealLfm3LoadTests(unittest.TestCase):
    """v0.7 (F1): the real LFM3 model loads under the bumped
    engine and generates text. Opt-in only
    (FYRN_REAL_ENGINE_TESTS=1): a real load takes minutes, and
    the roster store is not on every machine. Skips cleanly
    otherwise, with the reason in the skip message."""

    def test_real_lfm3_loads_and_generates(self):
        if not _real_engine_available():
            import os
            reasons = []
            if os.environ.get("FYRN_REAL_ENGINE_TESTS") != "1":
                reasons.append("FYRN_REAL_ENGINE_TESTS != 1")
            try:
                import llama_cpp
                version = llama_cpp.__version__
            except ImportError:
                version = "not installed"
            reasons.append("llama-cpp-python %s" % version)
            if not Path(_LFM3_BLOB).is_file():
                reasons.append("roster blob absent")
            self.skipTest("real-engine test skipped (%s)"
                          % "; ".join(reasons))
        runner = LlamaRunner(
            free_ram_fn=lambda: 8 * GB)
        result = runner.load(_LFM3_BLOB, display_name="lfm3:1.7b")
        self.assertTrue(runner.is_loaded)
        self.assertGreater(result.get("load_time_s", 0), 0)
        try:
            # A short generation, with a cancel backstop: the
            # model should stop at its end token after a few
            # words, but a rambling model must not hold the
            # suite open. Either a finished reply or a
            # cancelled one proves generation works, as long
            # as tokens arrived.
            received = []
            cancel = threading.Event()
            timer = threading.Timer(120.0, cancel.set)
            timer.daemon = True
            timer.start()
            try:
                runner.generate(
                    "Say hello in one sentence.",
                    on_token=received.append,
                    cancel_event=cancel)
            except GenerationCancelled:
                pass
            finally:
                timer.cancel()
            self.assertTrue(received,
                            "no tokens arrived from LFM3")
            self.assertTrue("".join(received).strip())
        finally:
            runner.unload()
        self.assertFalse(runner.is_loaded)


class DisplayNameTests(unittest.TestCase):
    """Task 11: progress and errors name the model, never the
    blob filename."""

    def test_fake_runner_progress_uses_the_display_name(self):
        runner = FakeRunner()
        seen = []
        runner.load("/tmp/sha256-abcdef0123456789",
                    progress_cb=lambda pct, msg: seen.append(msg),
                    display_name="Tiny 1B")
        self.assertTrue(seen)
        self.assertTrue(all("Tiny 1B" in msg for msg in seen))
        self.assertFalse(any("sha256" in msg for msg in seen))

    def test_fake_runner_falls_back_to_the_file_name(self):
        runner = FakeRunner()
        seen = []
        runner.load("/tmp/plain.gguf",
                    progress_cb=lambda pct, msg: seen.append(msg))
        self.assertTrue(all("plain.gguf" in msg for msg in seen))

    def test_real_runner_progress_uses_the_display_name(self):
        class FakeLlama:
            def __init__(self, model_path, n_threads, n_ctx,
                         verbose, chat_handler=None):
                pass

        module = types.ModuleType("llama_cpp")
        module.Llama = FakeLlama
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sha256-abcdef0123456789"
            path.write_bytes(b"x" * 2048)
            seen = []
            runner = LlamaRunner()
            with mock.patch.dict(sys.modules,
                                 {"llama_cpp": module}):
                runner.load(
                    str(path),
                    progress_cb=lambda pct, msg: seen.append(msg),
                    display_name="Tiny 1B")
        self.assertTrue(any("Tiny 1B" in msg for msg in seen))
        self.assertFalse(any("sha256" in msg for msg in seen))

    def test_controller_passes_the_display_name_through(self):
        from fyrn.core import ModelController

        class RecordingRunner(FakeRunner):
            def __init__(self):
                super().__init__()
                self.seen_name = None

            def load(self, path, progress_cb=None,
                     cancel_event=None, display_name=None, n_ctx=None):
                self.seen_name = display_name
                return super().load(path, progress_cb,
                                    cancel_event, display_name)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.gguf"
            path.write_bytes(b"x" * 2048)
            runner = RecordingRunner()
            controller = ModelController(runner)
            controller._free_ram_fn = lambda: 8 * GB
            controller.load(path, display_name="Tiny 1B")
            self.assertEqual(runner.seen_name, "Tiny 1B")


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




class FakeRunnerToolTests(unittest.TestCase):
    """The tool loop with the fake runner (v0.8): scripted calls
    run through a real executor, events fire in order, stop works
    mid-loop, and a model without tool support refuses plainly."""

    def _loaded(self, **kwargs):
        runner = FakeRunner(**kwargs)
        runner.load("model.gguf")
        return runner

    def test_multi_round_loop(self):
        # The loop's subject is the runner: rounds of calls
        # whose results feed the next round. The executor here
        # is a stand-in keyed by call name (the real web
        # executor has its own tests in test_tools.py).
        canned = {"web_search": ("alpha", "alpha"),
                  "web_read": ("6 * 7 = 42", "read")}

        def fake_execute(name, arguments):
            return canned[name]

        runner = self._loaded(
            supports_tools=True,
            tool_script=[
                ("call", "web_search", {"query": "alpha"}),
                ("call", "web_read",
                 {"url": "https://example.com"}),
                ("text", "Done: alpha and 42."),
            ])
        events = []
        pieces = []
        stats = runner.generate_tools(
            "search and read",
            execute_tool=fake_execute,
            on_token=pieces.append,
            on_event=lambda kind, data: events.append(
                (kind, data["name"])))
        self.assertEqual("".join(pieces), "Done: alpha and 42.")
        self.assertEqual(events, [
            ("call", "web_search"), ("result", "web_search"),
            ("call", "web_read"), ("result", "web_read")])
        self.assertEqual(
            [m["content"] for m in runner.last_tool_messages],
            ["alpha", "6 * 7 = 42"])
        self.assertGreater(stats["tokens"], 0)

    def test_instructions_reach_the_runner(self):
        runner = self._loaded(supports_tools=True)
        runner.generate_tools(
            "hi", instructions="Be brief.",
            execute_tool=lambda n, a: ("", ""))
        self.assertEqual(runner.last_instructions, "Be brief.")

    def test_stop_mid_loop(self):
        runner = self._loaded(
            supports_tools=True,
            tool_script=[("call", "calculate",
                          {"expression": "1+1"}),
                         ("sleep", 0.3),
                         ("text", "late")])
        cancel = threading.Event()
        timer = threading.Timer(0.1, cancel.set)
        timer.daemon = True
        timer.start()
        try:
            with self.assertRaises(GenerationCancelled):
                runner.generate_tools(
                    "hi", execute_tool=lambda n, a: ("2", "2"),
                    cancel_event=cancel)
        finally:
            timer.cancel()

    def test_tool_refusal_is_a_result_not_a_crash(self):
        # A web call that cannot run (no Forge Browser here)
        # comes back as a result text and the loop carries
        # on: the refusal is data for the model, not a crash.
        from fyrn.tools import BrowserToolExecutor
        with mock.patch("fyrn.tools.find_forge_browser_command",
                        return_value=None):
            executor = BrowserToolExecutor()
        runner = self._loaded(
            supports_tools=True,
            tool_script=[
                ("call", "web_search", {"query": "anything"}),
                ("text", "I could not search.")])
        pieces = []
        runner.generate_tools(
            "search it", execute_tool=executor.execute,
            on_token=pieces.append)
        self.assertEqual("".join(pieces), "I could not search.")
        self.assertTrue(
            runner.last_tool_messages[0]["content"].startswith(
                "Forge Browser is not installed"))

    def test_no_tool_support_raises(self):
        from fyrn.runner import ToolsUnsupportedError
        runner = self._loaded(supports_tools=False)
        with self.assertRaises(ToolsUnsupportedError):
            runner.generate_tools(
                "hi", execute_tool=lambda n, a: ("", ""))

    def test_base_runner_has_no_tools(self):
        from fyrn.runner import Runner, ToolsUnsupportedError
        with self.assertRaises(ToolsUnsupportedError):
            Runner().generate_tools("hi")


class _StubLlm:
    """A stand-in for the engine object: answers create_chat_
    completion from a script and records what it was sent."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.metadata = {}

    def create_chat_completion(self, messages=None, **kwargs):
        self.calls.append((list(messages), kwargs))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def tokenize(self, blob):
        return blob.split()


def _tool_response(name, arguments):
    return {"choices": [{"message": {
        "role": "assistant", "content": "",
        "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": name, "arguments": arguments}}],
    }}], "usage": {"completion_tokens": 5}}


def _text_response(text):
    return {"choices": [{"message": {
        "role": "assistant", "content": text}}],
        "usage": {"completion_tokens": 5}}


class LlamaRunnerToolLoopTests(unittest.TestCase):
    """LlamaRunner.generate_tools against a stub engine: the
    loop's shape (definitions offered, results fed back, rounds
    capped) without a real model."""

    def _runner(self, script):
        runner = LlamaRunner(free_ram_fn=lambda: 8 * GB)
        runner._llm = _StubLlm(script)
        runner.model_path = "stub.gguf"
        runner._tools = True
        return runner

    def test_native_tool_call_round(self):
        runner = self._runner([
            _tool_response("calculate", '{"expression": "2+2"}'),
            _text_response("The answer is 4."),
        ])
        events = []
        pieces = []
        stats = runner.generate_tools(
            "what is 2+2", instructions="Be brief.",
            execute_tool=lambda n, a: ("2+2 = 4", "2+2 = 4"),
            on_token=pieces.append,
            on_event=lambda k, d: events.append(k))
        self.assertEqual("".join(pieces), "The answer is 4.")
        self.assertEqual(events, ["call", "result"])
        # The definitions were offered, the system prompt went
        # first, and the tool result went back as a tool message.
        first_messages, first_kwargs = runner._llm.calls[0]
        self.assertIn("tools", first_kwargs)
        self.assertEqual(first_messages[0]["role"], "system")
        second_messages, _ = runner._llm.calls[1]
        tool_msgs = [m for m in second_messages
                     if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["content"], "2+2 = 4")
        self.assertGreater(stats["tokens"], 0)

    def test_text_tool_call_fallback(self):
        runner = self._runner([
            _text_response(
                "<tool_call>\n<function=calculate>\n"
                "<parameter=expression>3*3</parameter>\n"
                "</function>\n</tool_call>"),
            _text_response("Nine."),
        ])
        pieces = []
        runner.generate_tools(
            "3*3?", execute_tool=lambda n, a: ("3*3 = 9", "ok"),
            on_token=pieces.append)
        self.assertEqual("".join(pieces), "Nine.")
        second_messages, _ = runner._llm.calls[1]
        self.assertTrue(any(
            m.get("role") == "tool" and m["content"] == "3*3 = 9"
            for m in second_messages))

    def test_template_tool_error_is_unsupported(self):
        from fyrn.runner import ToolsUnsupportedError
        runner = self._runner([
            RuntimeError("Failed to apply jinja template: tools "
                         "are not supported")])
        with self.assertRaises(ToolsUnsupportedError):
            runner.generate_tools(
                "hi", execute_tool=lambda n, a: ("", ""))

    def test_rounds_are_capped(self):
        from fyrn.runner import MAX_TOOL_ROUNDS
        # MAX_TOOL_ROUNDS executed rounds, one more tool request
        # that trips the cap, then the final no-tools answer.
        script = [_tool_response("calculate",
                                 '{"expression": "1+1"}')
                  for _ in range(MAX_TOOL_ROUNDS + 1)]
        script.append(_text_response("Final."))
        runner = self._runner(script)
        pieces = []
        runner.generate_tools(
            "loop", execute_tool=lambda n, a: ("2", "2"),
            on_token=pieces.append)
        self.assertEqual("".join(pieces), "Final.")
        # MAX_TOOL_ROUNDS tool rounds + the final no-tools call.
        self.assertEqual(len(runner._llm.calls),
                         MAX_TOOL_ROUNDS + 2)
        self.assertNotIn("tools", runner._llm.calls[-1][1])

    def test_bad_arguments_are_reported_not_run(self):
        runner = self._runner([
            _tool_response("calculate", "{not json"),
            _text_response("Sorry."),
        ])
        executed = []
        runner.generate_tools(
            "hi",
            execute_tool=lambda n, a: executed.append(n) or ("", ""),
            on_token=lambda piece: None)
        self.assertEqual(executed, [])
        second_messages, _ = runner._llm.calls[1]
        self.assertTrue(any(
            "could not be read as JSON" in str(m.get("content", ""))
            for m in second_messages if m.get("role") == "tool"))


class PlainGenerateInstructionsTests(unittest.TestCase):
    """The plain (non-tool) generate path sends the chat's
    instructions as a leading system message (v0.8)."""

    def test_instructions_lead_the_messages(self):
        class StreamLlm:
            def __init__(self):
                self.calls = []

            def create_chat_completion(self, messages=None,
                                       **kwargs):
                self.calls.append(list(messages))
                return [{"choices": [{"delta": {
                    "content": "Bonjour."}}]}]

            def tokenize(self, blob):
                return blob.split()

        runner = LlamaRunner(free_ram_fn=lambda: 8 * GB)
        runner._llm = StreamLlm()
        runner.model_path = "stub.gguf"
        pieces = []
        runner.generate("Hello", on_token=pieces.append,
                        instructions="Always answer in French.")
        self.assertEqual("".join(pieces), "Bonjour.")
        messages = runner._llm.calls[0]
        self.assertEqual(messages[0], {
            "role": "system",
            "content": "Always answer in French."})
        self.assertEqual(messages[-1]["role"], "user")

    def test_no_instructions_no_system_message(self):
        class StreamLlm:
            def __init__(self):
                self.calls = []

            def create_chat_completion(self, messages=None,
                                       **kwargs):
                self.calls.append(list(messages))
                return [{"choices": [{"delta": {"content": "Hi."}}]}]

            def tokenize(self, blob):
                return blob.split()

        runner = LlamaRunner(free_ram_fn=lambda: 8 * GB)
        runner._llm = StreamLlm()
        runner.model_path = "stub.gguf"
        runner.generate("Hello", on_token=lambda piece: None)
        self.assertEqual(runner._llm.calls[0][0]["role"], "user")


class ToolDetectionTests(unittest.TestCase):
    def test_template_with_tools_supports_them(self):
        from fyrn.runner import _template_supports_tools

        class FakeLlm:
            metadata = {"tokenizer.chat_template":
                        "{% if tools %}...{% endif %}"}
        self.assertTrue(_template_supports_tools(FakeLlm()))

    def test_template_without_tools_does_not(self):
        from fyrn.runner import _template_supports_tools

        class FakeLlm:
            metadata = {"tokenizer.chat_template":
                        "{% for message in messages %}hi"}
        self.assertFalse(_template_supports_tools(FakeLlm()))

    def test_missing_template_does_not(self):
        from fyrn.runner import _template_supports_tools

        class FakeLlm:
            metadata = {}
        self.assertFalse(_template_supports_tools(FakeLlm()))
        self.assertFalse(_template_supports_tools(object()))


class RealToolCallTests(unittest.TestCase):
    """One REAL web call end to end: the real LFM3 model asks
    for a web search (through a stub Forge Browser CLI the
    test creates) and answers from its result. Opt-in only
    (FYRN_REAL_ENGINE_TESTS=1), like the load test above: a
    real load takes minutes and RAM the sandbox may not have.
    (v0.8-v0.12 this drove the calculator; the built-in tools
    were removed in v0.13 and the web connection is what
    remains.)"""

    def test_real_lfm3_web_search(self):
        if not _real_engine_available():
            self.skipTest("real-engine test not opted in")
        from test_tools import make_stub_forge_browser

        from fyrn.tools import BrowserToolExecutor
        runner = LlamaRunner(free_ram_fn=lambda: 8 * GB)
        runner.load(_LFM3_BLOB, display_name="lfm3:1.7b")
        try:
            self.assertTrue(
                runner.supports_tools,
                "LFM3's template should carry tool support")
            with tempfile.TemporaryDirectory() as tmp:
                app_dir = make_stub_forge_browser(Path(tmp))
                executor = BrowserToolExecutor(
                    forge_override=str(app_dir))
                pieces = []
                events = []
                runner.generate_tools(
                    "Use the web_search tool to search for "
                    "'stub query', then tell me the title of "
                    "the first result you found.",
                    execute_tool=executor.execute,
                    on_token=pieces.append,
                    on_event=lambda k, d: events.append(k))
            self.assertIn("call", events)
            self.assertIn("Stub Result", "".join(pieces))
        finally:
            runner.unload()


class ReplyCapTests(unittest.TestCase):
    """The reply-length cap (v0.9): one helper decides what an
    unset or bad cap means, and the cap reaches the engine on
    the plain path and the tool path alike."""

    def test_reply_cap_defaults(self):
        from fyrn.runner import reply_cap
        self.assertEqual(reply_cap(None), MAX_GENERATION_TOKENS)
        self.assertEqual(reply_cap(0), MAX_GENERATION_TOKENS)
        self.assertEqual(reply_cap(-3), MAX_GENERATION_TOKENS)
        self.assertEqual(reply_cap(True), MAX_GENERATION_TOKENS)
        self.assertEqual(reply_cap("2048"),
                         MAX_GENERATION_TOKENS)
        self.assertEqual(reply_cap(2048), 2048)
        self.assertEqual(reply_cap(1), 1)

    def test_fake_runner_honours_and_records_the_cap(self):
        runner = FakeRunner(
            reply_text="one two three four five")
        runner.load("/tmp/fake.gguf")
        pieces = []
        collect = pieces.append
        runner.generate("hi", on_token=collect,
                        max_tokens=2)
        self.assertEqual("".join(pieces), "one two")
        self.assertEqual(runner.last_max_tokens, 2)

    def test_fake_runner_default_cap_is_the_old_default(self):
        runner = FakeRunner(reply_text="one two three")
        runner.load("/tmp/fake.gguf")
        runner.generate("hi")
        self.assertEqual(runner.last_max_tokens,
                         MAX_GENERATION_TOKENS)

    def test_fake_tool_path_honours_the_cap(self):
        runner = FakeRunner(
            supports_tools=True,
            tool_script=[("text", "one two three four")])
        runner.load("/tmp/fake.gguf")
        pieces = []
        collect = pieces.append
        runner.generate_tools(
            "hi", execute_tool=lambda name, args: ("", ""),
            on_token=collect, max_tokens=3)
        self.assertEqual("".join(pieces), "one two three")
        self.assertEqual(runner.last_max_tokens, 3)

    def test_llama_runner_passes_the_cap_to_the_engine(self):
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
        runner.generate("hello", max_tokens=77)
        _messages, max_tokens = FakeChatLlama.CREATED[0].calls[0]
        self.assertEqual(max_tokens, 77)
        # And with no cap asked for, the engine gets the
        # default, exactly as before the setting existed.
        runner.generate("hello again")
        _messages, max_tokens = FakeChatLlama.CREATED[0].calls[1]
        self.assertEqual(max_tokens, MAX_GENERATION_TOKENS)


if __name__ == "__main__":
    unittest.main()
