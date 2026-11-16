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

# A reply never runs past this many tokens: a model that never
# emits its stop token would otherwise run to context fill, which
# is up to ~10 minutes on this PC. 1024 is a full answer from a
# 2B model, and Stop still cuts it short.
MAX_GENERATION_TOKENS = 1024

# The conversation history sent with each message is trimmed to
# this many characters (about 1000 tokens): half the smallest
# context window, so the history plus the reply always fit.
MAX_HISTORY_CHARS = 4096


def trim_history(history, max_chars=MAX_HISTORY_CHARS):
    """Trim a message list to the most recent turns that fit the
    budget, dropping the oldest first. The newest exchange is
    always kept, even over budget: a reply with no question in
    front of it answers nothing."""
    history = list(history)
    total = sum(len(str(m.get("content", ""))) for m in history)
    while total > max_chars and len(history) > 2:
        dropped = history.pop(0)
        total -= len(str(dropped.get("content", "")))
    return history


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

    # True when a load in progress can actually be stopped. The
    # real backend loads in one long opaque step that never checks
    # for a cancel, so it says False and the UI stops promising a
    # cancel it cannot deliver.
    cancellable_load = True

    # True when the loaded model can see images: a vision model
    # whose projector (mmproj) file was found and loaded. The UI
    # enables Attach only then, so a text model never promises
    # sight it does not have.
    supports_vision = False

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

    def generate(self, prompt, on_token=None, cancel_event=None,
                 history=None, images=None):
        """Stream a reply. Returns a stats dict.

        on_token is called with each piece of text as it arrives.
        history is the earlier turns (a list of {"role": ...,
        "content": ...} dicts), sent with the new message so the
        model answers in context instead of forgetting every turn.
        images is a list of image file paths to send with the
        message; a model that cannot see raises RunnerError
        instead of pretending to look at them.
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
                 token_delay_s=0.0, ttft_delay_s=0.0,
                 supports_vision=False):
        super().__init__()
        self.reply_text = (
            reply_text
            if reply_text is not None
            else "Hello from the fake model. This is a test reply.")
        self.load_delay_s = load_delay_s
        self.token_delay_s = token_delay_s
        self.ttft_delay_s = ttft_delay_s
        self.supports_vision = supports_vision
        # The history the last generate() call received, so tests
        # can check the conversation actually reached the runner.
        self.last_history = None
        # The images the last generate() call received, same idea.
        self.last_images = None

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

    def generate(self, prompt, on_token=None, cancel_event=None,
                 history=None, images=None):
        if not self.is_loaded:
            raise RunnerError("No model is loaded.")
        if images and not self.supports_vision:
            raise RunnerError(
                "This model cannot see images. Load a vision "
                "model (one with its projector file next to it) "
                "to ask about a picture.")
        self.last_history = list(history or [])
        self.last_images = list(images or [])
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


# Image types the attach dialog offers: PNG and JPEG are what
# the engine's image loader is known to handle.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg",
               ".jpeg": "image/jpeg"}


def _image_data_url(path):
    """An image file as a data: URL for the engine's image loader
    (it takes data: URLs and web addresses, not local paths)."""
    import base64

    suffix = os.path.splitext(str(path))[1].lower()
    mime = _IMAGE_MIME.get(suffix)
    if mime is None:
        raise RunnerError(
            "That image type cannot be used. Pick a PNG or JPEG "
            "picture.")
    with open(path, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("ascii")
    return "data:%s;base64,%s" % (mime, encoded)


class LlamaRunner(Runner):
    """The real runner: GGUF models on the CPU via llama.cpp.

    llama_cpp is imported lazily, inside load(), so the window opens
    and the tests run on a machine without the backend installed.

    Vision: a model with its projector (mmproj) file next to it
    loads through the engine's mtmd handler, so it can answer
    about pictures. The handler runs on the CPU (use_gpu=False):
    this PC has no GPU to offload to.
    """

    # The Llama() constructor is one long opaque step: it never
    # checks for a cancel, so a load in progress cannot be stopped.
    cancellable_load = False

    def __init__(self, free_ram_fn=None):
        super().__init__()
        self._llm = None
        self._free_ram_fn = free_ram_fn
        self._vision = False
        self.projector_path = None

    @property
    def supports_vision(self):
        return self._vision

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
        # Vision: a projector file next to the model means the
        # model can see. It loads through the engine's mtmd
        # handler (CPU only). A projector that cannot be used
        # never blocks the load: the model still loads, as text
        # only, and the warning says why it cannot see.
        from .core import (VISION_ARCHITECTURES, find_projector,
                           read_gguf_metadata)
        projector = find_projector(path)
        chat_handler = None
        vision_warning = ""
        if projector is not None:
            try:
                from llama_cpp.llama_chat_format import (
                    MTMDChatHandler)
                chat_handler = MTMDChatHandler(
                    str(projector), verbose=False, use_gpu=False)
            except Exception as exc:
                chat_handler = None
                vision_warning = (
                    "Its projector file could not be used (%s), so "
                    "it loads as text only." % exc)
        elif (read_gguf_metadata(path).get("general.architecture")
                in VISION_ARCHITECTURES):
            vision_warning = (
                "This is a vision model, but its projector file "
                "is not next to it, so it loads as text only. "
                "Put the mmproj file in the same folder to let "
                "it see.")
        try:
            self._llm = Llama(
                model_path=str(path),
                n_threads=threads,
                n_ctx=context,
                verbose=False,
                chat_handler=chat_handler,
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
        self._vision = chat_handler is not None
        self.projector_path = (str(projector)
                               if chat_handler is not None else None)
        result = {"load_time_s": time.monotonic() - started}
        if vision_warning:
            result["vision_warning"] = vision_warning
        return result

    def unload(self):
        # Dropping the reference frees the model; the backend holds
        # no other state. The vision state goes with it.
        self._llm = None
        self._vision = False
        self.projector_path = None
        super().unload()

    def generate(self, prompt, on_token=None, cancel_event=None,
                 history=None, images=None):
        if not self.is_loaded or self._llm is None:
            raise RunnerError("No model is loaded.")
        if images and not self._vision:
            raise RunnerError(
                "This model cannot see images. Load a vision "
                "model (one with its projector file next to it) "
                "to ask about a picture.")
        started = time.monotonic()
        ttft_s = None
        pieces = []
        # The earlier turns go first, trimmed to the budget, so
        # the model answers in context; the new message is last.
        # With pictures, the new message carries them as image
        # parts the engine's vision handler turns into sight.
        messages = trim_history(history or [])
        if images:
            parts = [{"type": "text", "text": prompt}]
            for image_path in images:
                try:
                    url = _image_data_url(image_path)
                except OSError as exc:
                    raise RunnerError(
                        "That picture could not be read (%s)." % exc)
                parts.append({"type": "image_url",
                              "image_url": {"url": url}})
            messages.append({"role": "user", "content": parts})
        else:
            messages.append({"role": "user", "content": prompt})
        try:
            stream = self._llm.create_chat_completion(
                messages=messages,
                stream=True,
                max_tokens=MAX_GENERATION_TOKENS,
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
