"""Model runners: the engine behind Fyrn.

A Runner loads one GGUF model and streams text back. The real
runner uses the CPU-only llama.cpp backend; the fake runner stands
in for tests with known replies and known timing, so the suite
never needs a real model or a GPU.

The backend configures itself (U35): threads and context size come
from the machine, never from a settings screen.
"""

import os
import time


class RunnerError(Exception):
    """The model engine failed, in plain words."""


class LoadCancelled(Exception):
    """The user cancelled a load."""


class GenerationCancelled(Exception):
    """The user stopped a reply."""


def compute_tokens_per_sec(tokens, elapsed_s):
    """Exact decode speed. Zero elapsed means no measurement yet."""
    if elapsed_s is None or elapsed_s <= 0:
        return 0.0
    return tokens / elapsed_s


def auto_threads():
    """Threads for this machine: all cores, at least 1."""
    return max(1, os.cpu_count() or 2)


def auto_context_tokens(free_bytes):
    """Context size for this machine.

    4096 tokens when free RAM is roomy (6 GB or more), 2048 when
    tight. The user never sees this number.
    """
    if free_bytes is not None and free_bytes >= 6 * 1024 ** 3:
        return 4096
    return 2048


class Runner:
    """One loaded model. Subclasses do the real work."""

    def __init__(self):
        self.model_path = None

    @property
    def is_loaded(self):
        return self.model_path is not None

    @property
    def model_name(self):
        if self.model_path is None:
            return ""
        return os.path.basename(str(self.model_path))

    def load(self, path, progress_cb=None, cancel_event=None):
        """Load a model file. Returns {"load_time_s": float}.

        progress_cb(pct, msg) reports real progress only: pct is
        0-100 when the runner can measure its own progress, or -1
        when it cannot (the real backend loads in one long opaque
        step, so it reports -1 with honest stage text instead of a
        percent that measures nothing). The UI shows -1 as an
        indeterminate bar.
        """
        raise NotImplementedError

    def unload(self):
        """Unload the model and free its RAM."""
        self.model_path = None

    def generate(self, prompt, on_token=None, cancel_event=None):
        """Stream a reply. Returns a stats dict.

        on_token is called with each piece of text as it arrives.
        Stats: tokens, elapsed_s, ttft_s (time to first token),
        tokens_per_sec.
        """
        raise NotImplementedError


class FakeRunner(Runner):
    """A predictable stand-in for tests and demos.

    load_delay_s makes loads take a known time (so cancel and
    progress can be tested). generate() streams reply_text word by
    word, waiting token_delay_s between words and ttft_delay_s
    before the first word.
    """

    def __init__(self, reply_text=None, load_delay_s=0.0,
                 token_delay_s=0.0, ttft_delay_s=0.0):
        super().__init__()
        self.reply_text = (
            reply_text
            if reply_text is not None
            else "Hello from the fake model. This is a test reply.")
        self.load_delay_s = load_delay_s
        self.token_delay_s = token_delay_s
        self.ttft_delay_s = ttft_delay_s

    def load(self, path, progress_cb=None, cancel_event=None):
        started = time.monotonic()
        steps = 10
        for step in range(steps + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise LoadCancelled("Load cancelled.")
            if progress_cb is not None:
                progress_cb(int(step * 100 / steps),
                            "Loading %s" % os.path.basename(str(path)))
            if step < steps and self.load_delay_s > 0:
                time.sleep(self.load_delay_s / steps)
        self.model_path = str(path)
        return {"load_time_s": time.monotonic() - started}

    def unload(self):
        super().unload()

    def generate(self, prompt, on_token=None, cancel_event=None):
        if not self.is_loaded:
            raise RunnerError("No model is loaded.")
        started = time.monotonic()
        if self.ttft_delay_s > 0:
            time.sleep(self.ttft_delay_s)
        ttft_s = time.monotonic() - started
        words = self.reply_text.split(" ")
        for index, word in enumerate(words):
            if cancel_event is not None and cancel_event.is_set():
                raise GenerationCancelled("Reply stopped.")
            piece = word if index == len(words) - 1 else word + " "
            if on_token is not None:
                on_token(piece)
            if self.token_delay_s > 0 and index < len(words) - 1:
                time.sleep(self.token_delay_s)
        elapsed_s = time.monotonic() - started
        tokens = len(words)
        return {"tokens": tokens,
                "elapsed_s": elapsed_s,
                "ttft_s": ttft_s,
                "tokens_per_sec": compute_tokens_per_sec(tokens,
                                                         elapsed_s)}


class LlamaRunner(Runner):
    """The real runner: GGUF models on the CPU via llama.cpp.

    llama_cpp is imported lazily, inside load(), so the window opens
    and the tests run on a machine without the backend installed.
    """

    def __init__(self, free_ram_fn=None):
        super().__init__()
        self._llm = None
        self._free_ram_fn = free_ram_fn

    def _free_ram(self):
        if self._free_ram_fn is not None:
            return self._free_ram_fn()
        from .core import read_ram_bytes
        return read_ram_bytes()[1]

    def load(self, path, progress_cb=None, cancel_event=None):
        try:
            from llama_cpp import Llama
        except ImportError:
            raise RunnerError(
                "The model engine is not installed. Run bash "
                "install.sh again with a network connection, then "
                "try loading the model again.")
        started = time.monotonic()
        # The backend gives no progress while it loads: one long
        # opaque step. Report -1 (indeterminate) with honest stage
        # text and the real file size, never a percent that
        # measures nothing. (An earlier version reported 10 here
        # and 100 at the end, so the bar sat at 10% for the whole
        # load and then jumped.)
        if progress_cb is not None:
            from .core import format_size
            try:
                size_text = format_size(
                    os.path.getsize(str(path)))
            except OSError:
                size_text = "unknown size"
            progress_cb(-1, "Loading %s (%s) into RAM..."
                        % (os.path.basename(str(path)), size_text))
        if cancel_event is not None and cancel_event.is_set():
            raise LoadCancelled("Load cancelled.")
        threads = auto_threads()
        context = auto_context_tokens(self._free_ram())
        try:
            self._llm = Llama(
                model_path=str(path),
                n_threads=threads,
                n_ctx=context,
                verbose=False,
            )
        except Exception as exc:
            self._llm = None
            raise RunnerError(
                "The model could not be loaded (%s). The file may "
                "be broken or not a GGUF model." % exc)
        if cancel_event is not None and cancel_event.is_set():
            self._llm = None
            raise LoadCancelled("Load cancelled.")
        if progress_cb is not None:
            progress_cb(100, "Loaded")
        self.model_path = str(path)
        return {"load_time_s": time.monotonic() - started}

    def unload(self):
        # Dropping the reference frees the model; the backend holds
        # no other state.
        self._llm = None
        super().unload()

    def generate(self, prompt, on_token=None, cancel_event=None):
        if not self.is_loaded or self._llm is None:
            raise RunnerError("No model is loaded.")
        started = time.monotonic()
        ttft_s = None
        pieces = []
        try:
            stream = self._llm.create_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                stream=True,
            )
            for chunk in stream:
                if cancel_event is not None and cancel_event.is_set():
                    raise GenerationCancelled("Reply stopped.")
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content") or ""
                if not text:
                    continue
                if ttft_s is None:
                    ttft_s = time.monotonic() - started
                pieces.append(text)
                if on_token is not None:
                    on_token(text)
        except GenerationCancelled:
            raise
        except Exception as exc:
            raise RunnerError(
                "The model stopped with an error (%s)." % exc)
        elapsed_s = time.monotonic() - started
        if ttft_s is None:
            ttft_s = elapsed_s
        full_text = "".join(pieces)
        try:
            tokens = len(self._llm.tokenize(full_text.encode("utf-8")))
        except Exception:
            tokens = max(1, len(full_text) // 4)
        return {"tokens": tokens,
                "elapsed_s": elapsed_s,
                "ttft_s": ttft_s,
                "tokens_per_sec": compute_tokens_per_sec(tokens,
                                                         elapsed_s)}
