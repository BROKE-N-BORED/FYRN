"""Model runners: the engine behind Fyrn.

A Runner loads one GGUF model and streams text back. The real
runner uses the CPU-only llama.cpp backend; the fake runner stands
in for tests with known replies and known timing, so the suite
never needs a real model or a GPU.

The backend configures itself (U35): threads and context size come
from the machine, never from a settings screen.
"""

import json
import os
import re
import sys
import time

from .tools import WEB_TOOL_DEFINITIONS, parse_tool_calls

# A reply never runs past this many tokens: a model that never
# emits its stop token would otherwise run to context fill, which
# is up to ~10 minutes on this PC. 1024 is a full answer from a
# 2B model, and Stop still cuts it short. This is the DEFAULT
# cap; the reply-length setting (Settings) picks another one,
# and every generate call takes it as max_tokens (v0.9).
MAX_GENERATION_TOKENS = 1024


def reply_cap(max_tokens):
    """The token cap a generate call actually uses: the caller's
    max_tokens when it is a positive int, the default cap
    otherwise. One place decides, so the plain path, the tool
    path, and the bridge can never disagree about what an unset
    cap means."""
    if isinstance(max_tokens, int) and not isinstance(
            max_tokens, bool) and max_tokens > 0:
        return max_tokens
    return MAX_GENERATION_TOKENS

# How many tool rounds one reply may take (model asks for tools,
# gets the results, asks again) before it has to answer with what
# it has.
MAX_TOOL_ROUNDS = 5

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


# What the user is told when a chat outgrows the loaded model's
# context window (v0.13). The engine's own error names token
# counts ("Requested tokens (2111) exceed context window of
# 2048"), which the user cannot act on; this sentence says what
# happened and what to do. The runner raises it when a turn
# cannot fit even after trimming, and every failure path maps
# the engine's overflow text to it, so the raw engine string
# never reaches the transcript.
CHAT_TOO_LONG_MESSAGE = (
    "This chat has grown too long for this model's memory. "
    "Start a new chat, or clear this one, and ask again.")


def _looks_like_context_overflow(text):
    """Whether an error's text is the engine's context-window
    overflow (its wording has varied across versions; the two
    words together are the stable part)."""
    text = str(text).lower()
    return "context" in text and "exceed" in text


def failure_message(exc):
    """The plain-words failure for an engine error: a context
    overflow becomes CHAT_TOO_LONG_MESSAGE; anything else keeps
    the established shape, the plain sentence first and the
    engine's reason attached."""
    if _looks_like_context_overflow(exc):
        return CHAT_TOO_LONG_MESSAGE
    return "The model stopped with an error (%s)." % exc


def plain_chat_failure(message):
    """A failure string on its way to the transcript, mapped to
    plain words: a context overflow hiding inside a longer
    message becomes CHAT_TOO_LONG_MESSAGE. Anything else passes
    through unchanged."""
    if _looks_like_context_overflow(message):
        return CHAT_TOO_LONG_MESSAGE
    return message


class RunnerError(Exception):
    """The model engine failed, in plain words."""


class LoadCancelled(Exception):
    """The user cancelled a load."""


class GenerationCancelled(Exception):
    """The user stopped a reply."""


class ToolsUnsupportedError(RunnerError):
    """The loaded model cannot do tool calls after all: its chat
    template has no working tool support. The GUI falls back to a
    plain reply for the turn and says so in one plain line."""


# Architectures the pinned engine is KNOWN not to support,
# proven by a failed load, not guessed: when a load of one of
# these fails, the user is told the engine does not know the
# design yet, instead of being told the file may be broken
# (task 8). The mechanism stays for genuinely unknown
# architectures (the C++ error names them, but only on stderr,
# so the Python exception never carries it). LFM3 was the one
# member until v0.7: the engine bump to llama-cpp-python
# 0.3.37 added it, so it loads now and the set is empty.
UNSUPPORTED_ARCHITECTURES = frozenset()


def warm_engine():
    """Import the model engine now, off the load path, so the
    first load of a session starts loading at once instead of
    paying the engine import (70-90 s inside the app) inside
    the load worker (task 2). Safe to call from any thread:
    Python's import lock serializes it with a load that imports
    at the same moment, and a missing engine is not an error
    here (load() reports it when a load is attempted). Returns
    True when the engine imported."""
    try:
        from llama_cpp import Llama  # noqa: F401
        return True
    except ImportError:
        return False


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

    # True when the loaded model can call tools mid-chat (its own
    # chat template carries tool support). The UI offers the tool
    # path only then; every other model chats exactly as before.
    supports_tools = False

    def __init__(self):
        self.model_path = None
        # The context size the loaded model was loaded with, in
        # tokens (None while nothing is loaded). The bridge
        # compares a request's num_ctx against it and reports it
        # (/api/show), so every runner records it at load.
        self.context_tokens = None

    @property
    def is_loaded(self):
        return self.model_path is not None

    @property
    def model_name(self):
        if self.model_path is None:
            return ""
        return os.path.basename(str(self.model_path))

    def load(self, path, progress_cb=None, cancel_event=None,
             display_name=None, n_ctx=None, model_params=None,
             model_system=None, projector=None):
        """Load a model file. Returns {"load_time_s": float}.

        n_ctx is an optional context size in tokens (the
        bridge's num_ctx); None means the runner picks the size
        for this machine, as it always has.

        model_params, model_system, and projector carry what
        discovery learned from the model's own Ollama manifest
        (v0.13): its Modelfile sampling parameters, its system
        prompt, and its vision projector file. All are optional;
        a folder model passes none of them and behaves exactly
        as it always has.

        progress_cb(pct, msg) reports real progress only: pct is
        0-100 when the runner can measure its own progress, or -1
        when it cannot (the real backend loads in one long opaque
        step, so it reports -1 with honest stage text instead of a
        percent that measures nothing). The UI shows -1 as an
        indeterminate bar. display_name is the name the UI shows
        for the model (an Ollama model's name, for example);
        progress and errors use it, never the file name, so an
        Ollama blob hash never reaches the user (task 11).
        """
        raise NotImplementedError

    def unload(self):
        """Unload the model and free its RAM."""
        self.model_path = None
        self.context_tokens = None

    def generate_turn(self, messages, tools=None, images=None,
                      cancel_event=None, max_tokens=None):
        """ONE chat turn with the client's tools on offer (the
        bridge's pass-through, v0.10).

        messages is the whole conversation in engine shape
        (system, user, assistant — including assistant turns
        carrying tool_calls — and tool results as role "tool"
        entries). tools is the CLIENT's tool definitions, passed
        to the model untouched: the runner never executes a tool
        here. Returns {"content": str, "tool_calls": [{"id",
        "name", "arguments"}], "stats": {...}}: either the model
        answered in text, or it asked for tools and the calls are
        the client's to run. A model whose template cannot take
        tools answers in text, the way Ollama behaves.
        """
        raise NotImplementedError

    def generate(self, prompt, on_token=None, cancel_event=None,
                 history=None, images=None,
                 instructions=None, max_tokens=None):
        """Stream a reply. Returns a stats dict.

        on_token is called with each piece of text as it arrives.
        history is the earlier turns (a list of {"role": ...,
        "content": ...} dicts), sent with the new message so the
        model answers in context instead of forgetting every turn.
        images is a list of image file paths to send with the
        message; a model that cannot see raises RunnerError
        instead of pretending to look at them. max_tokens caps
        the reply length (the reply-length setting); None means
        the default cap (MAX_GENERATION_TOKENS).
        Stats: tokens, elapsed_s, ttft_s (time to first token),
        tokens_per_sec.
        """
        raise NotImplementedError

    def generate_tools(self, prompt, history=None, instructions=None,
                       images=None, execute_tool=None, on_token=None,
                       on_event=None, cancel_event=None,
                       max_tokens=None):
        """Reply with tools on offer (v0.8).

        The model may call tools mid-reply: each call is reported
        through on_event("call", {...}), run through execute_tool
        (a callable(name, arguments) -> (result_text, summary)),
        reported through on_event("result", {...}), and the result
        goes back to the model. After at most MAX_TOOL_ROUNDS
        rounds the final answer streams through on_token as usual.
        max_tokens caps each round and the final answer, the same
        reply-length cap as generate(). Returns the same stats
        dict as generate(). Runners whose model cannot do this
        raise ToolsUnsupportedError.
        """
        raise ToolsUnsupportedError(
            "This model cannot use tools.")


class FakeRunner(Runner):
    """A predictable stand-in for tests and demos.

    load_delay_s makes loads take a known time (so cancel and
    progress can be tested). generate() streams reply_text word by
    word, waiting token_delay_s between words and ttft_delay_s
    before the first word.
    """

    def __init__(self, reply_text=None, load_delay_s=0.0,
                 token_delay_s=0.0, ttft_delay_s=0.0,
                 supports_vision=False, supports_tools=False,
                 tool_script=None):
        super().__init__()
        self.reply_text = (
            reply_text
            if reply_text is not None
            else "Hello from the fake model. This is a test reply.")
        self.load_delay_s = load_delay_s
        self.token_delay_s = token_delay_s
        self.ttft_delay_s = ttft_delay_s
        self.supports_vision = supports_vision
        self.supports_tools = supports_tools
        # The scripted tool rounds for generate_tools: a list of
        # ("call", name, arguments) steps, a ("sleep", seconds)
        # step, and a final ("text", reply) step. Empty means the
        # model answers at once with reply_text.
        self.tool_script = list(tool_script or [])
        # What the last generate_tools() call saw and did.
        self.last_instructions = None
        self.last_tool_messages = []
        # The history the last generate() call received, so tests
        # can check the conversation actually reached the runner.
        self.last_history = None
        # The images the last generate() call received, same idea.
        self.last_images = None
        # The reply cap the last generate call actually used
        # (after reply_cap()): tests check the reply-length
        # setting reached the runner.
        self.last_max_tokens = None
        # The n_ctx the last load was asked for (None = the
        # runner's own pick), and what the last generate_turn
        # call saw, for the bridge tests.
        self.last_load_n_ctx = None
        self.last_turn_messages = None
        self.last_turn_tools = None
        # What the last load was handed from the model's own
        # manifest (v0.13), for the discovery/runner tests.
        self.last_model_params = None
        self.last_model_system = None
        self.last_projector = None

    def load(self, path, progress_cb=None, cancel_event=None,
             display_name=None, n_ctx=None, model_params=None,
             model_system=None, projector=None):
        started = time.monotonic()
        name = display_name or os.path.basename(str(path))
        steps = 10
        for step in range(steps + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise LoadCancelled("Load cancelled.")
            if progress_cb is not None:
                progress_cb(int(step * 100 / steps),
                            "Loading %s" % name)
            if step < steps and self.load_delay_s > 0:
                time.sleep(self.load_delay_s / steps)
        self.model_path = str(path)
        self.last_load_n_ctx = n_ctx
        self.last_model_params = model_params
        self.last_model_system = model_system
        self.last_projector = projector
        self.context_tokens = n_ctx if (
            isinstance(n_ctx, int) and n_ctx > 0) else 2048
        return {"load_time_s": time.monotonic() - started}

    def unload(self):
        super().unload()

    def generate(self, prompt, on_token=None, cancel_event=None,
                 history=None, images=None,
                 instructions=None, max_tokens=None):
        if not self.is_loaded:
            raise RunnerError("No model is loaded.")
        if images and not self.supports_vision:
            raise RunnerError(
                "This model cannot see images. Load a vision "
                "model (one with its projector file next to it) "
                "to ask about a picture.")
        self.last_history = list(history or [])
        self.last_images = list(images or [])
        self.last_instructions = instructions
        self.last_max_tokens = reply_cap(max_tokens)
        started = time.monotonic()
        if self.ttft_delay_s > 0:
            time.sleep(self.ttft_delay_s)
        ttft_s = time.monotonic() - started
        # The fake's words stand in for tokens: the cap cuts the
        # reply short the way the real engine's cap does.
        words = self.reply_text.split(" ")[:self.last_max_tokens]
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

    def generate_tools(self, prompt, history=None, instructions=None,
                       images=None, execute_tool=None, on_token=None,
                       on_event=None, cancel_event=None,
                       max_tokens=None):
        if not self.is_loaded:
            raise RunnerError("No model is loaded.")
        if not self.supports_tools:
            raise ToolsUnsupportedError(
                "This model cannot use tools.")
        self.last_history = list(history or [])
        self.last_images = list(images or [])
        self.last_instructions = instructions
        self.last_max_tokens = reply_cap(max_tokens)
        self.last_tool_messages = []
        started = time.monotonic()
        ttft_s = None
        final_text = self.reply_text
        for step in self.tool_script:
            if cancel_event is not None and cancel_event.is_set():
                raise GenerationCancelled("Reply stopped.")
            kind = step[0]
            if kind == "sleep":
                time.sleep(step[1])
            elif kind == "call":
                _, name, arguments = step
                if on_event is not None:
                    on_event("call", {"name": name,
                                      "arguments": arguments})
                if execute_tool is not None:
                    result, summary = execute_tool(name, arguments)
                else:
                    result = "Tools are not available."
                    summary = result
                self.last_tool_messages.append(
                    {"role": "tool", "name": name, "content": result})
                if on_event is not None:
                    on_event("result", {"name": name,
                                        "summary": summary,
                                        "result": result})
            elif kind == "text":
                final_text = step[1]
                break
        if cancel_event is not None and cancel_event.is_set():
            raise GenerationCancelled("Reply stopped.")
        if self.ttft_delay_s > 0:
            time.sleep(self.ttft_delay_s)
        ttft_s = time.monotonic() - started
        # The cap cuts the final answer short, words as tokens.
        final_text = " ".join(
            final_text.split(" ")[:self.last_max_tokens])
        if on_token is not None:
            on_token(final_text)
        elapsed_s = time.monotonic() - started
        tokens = max(1, len(final_text.split()))
        return {"tokens": tokens,
                "elapsed_s": elapsed_s,
                "ttft_s": ttft_s,
                "tokens_per_sec": compute_tokens_per_sec(tokens,
                                                         elapsed_s)}

    def generate_turn(self, messages, tools=None, images=None,
                      cancel_event=None, max_tokens=None):
        """One pass-through turn (v0.10), scripted like
        generate_tools but WITHOUT executing anything: a leading
        ("call", ...) step in tool_script is handed back as the
        turn's tool_calls, for the caller to run. Once the
        conversation carries a tool result, the fake answers in
        text, the way a real model finishes the turn."""
        if not self.is_loaded:
            raise RunnerError("No model is loaded.")
        if images and not self.supports_vision:
            raise RunnerError(
                "This model cannot see images. Load a vision "
                "model (one with its projector file next to it) "
                "to ask about a picture.")
        self.last_turn_messages = list(messages or [])
        self.last_turn_tools = tools
        self.last_images = list(images or [])
        self.last_max_tokens = reply_cap(max_tokens)
        if cancel_event is not None and cancel_event.is_set():
            raise GenerationCancelled("Reply stopped.")
        started = time.monotonic()
        content = ""
        calls = []
        has_tool_result = any(
            isinstance(m, dict) and m.get("role") == "tool"
            for m in self.last_turn_messages)
        scripted_call = next(
            (step for step in self.tool_script
             if step[0] == "call"), None)
        if (tools and self.supports_tools and scripted_call
                is not None and not has_tool_result):
            _kind, name, arguments = scripted_call
            calls = [{"id": "fake-call-1", "name": name,
                      "arguments": arguments}]
        else:
            content = " ".join(
                self.reply_text.split(" ")[:self.last_max_tokens])
        elapsed_s = time.monotonic() - started
        tokens = max(1, len(content.split())) if content else 1
        return {"content": content,
                "tool_calls": calls,
                "stats": {"tokens": tokens,
                          "elapsed_s": elapsed_s,
                          "ttft_s": elapsed_s,
                          "tokens_per_sec":
                              compute_tokens_per_sec(tokens,
                                                     elapsed_s)}}


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


def _template_supports_tools(llm):
    """Whether a loaded model's own chat template carries tool
    support. The template is the model's to declare: the ones with
    tool sections render the tool list and the tool replies, and
    the engine parses their calls. Read from the loaded engine
    object, not the file header — the header reader only scans
    the start of the file, and the template sits past the
    tokenizer tables."""
    try:
        metadata = getattr(llm, "metadata", None) or {}
        template = metadata.get("tokenizer.chat_template") or ""
    except Exception:
        return False
    return "tool" in template.lower()


def _looks_like_tool_problem(exc):
    """Whether an engine error from a tools call points at the
    template/tool machinery (as opposed to the model itself)."""
    text = str(exc).lower()
    return any(word in text for word in
               ("tool", "jinja", "template", "chat format"))


def _native_tool_calls(message):
    """Normalise a chat completion message's tool_calls into
    {"id", "name", "arguments", "args_error"} dicts. Returns []
    when the message carries none."""
    calls = []
    for entry in message.get("tool_calls") or []:
        function = entry.get("function") or {}
        name = function.get("name") or ""
        raw_args = function.get("arguments")
        args_error = None
        if isinstance(raw_args, dict):
            arguments = raw_args
        elif isinstance(raw_args, str) and raw_args.strip():
            try:
                arguments = json.loads(raw_args)
                if not isinstance(arguments, dict):
                    arguments = {}
                    args_error = raw_args
            except ValueError:
                arguments = {}
                args_error = raw_args
        else:
            arguments = {}
        if name:
            calls.append({"id": entry.get("id") or "",
                          "name": name,
                          "arguments": arguments,
                          "args_error": args_error})
    return calls


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
        self._tools = False
        self.projector_path = None
        # The loaded model's own Modelfile parameters and system
        # prompt, carried from its Ollama manifest by discovery
        # (v0.13). Empty/None for a folder model: it generates on
        # engine defaults, exactly as before.
        self._model_params = {}
        self._model_system = None

    @property
    def supports_vision(self):
        return self._vision

    @property
    def supports_tools(self):
        return self._tools

    def _free_ram(self):
        if self._free_ram_fn is not None:
            return self._free_ram_fn()
        from .core import read_ram_bytes
        return read_ram_bytes()[1]

    def load(self, path, progress_cb=None, cancel_event=None,
             display_name=None, n_ctx=None, model_params=None,
             model_system=None, projector=None):
        # The name the user sees: an Ollama model's name, or the
        # file name. Progress and errors use it, never the blob
        # filename (task 11).
        name = display_name or os.path.basename(str(path))
        # The engine import is the slow part of a first load
        # (task 2): say so honestly while it runs, instead of
        # claiming the model is loading. When the startup warm-up
        # already imported it, this step is instant and the
        # message never shows. If the warm-up is still importing,
        # this blocks on the import lock with the honest text
        # showing, which is still correct.
        if "llama_cpp" not in sys.modules and progress_cb is not None:
            progress_cb(-1, "Starting the model engine...")
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
                        % (name, size_text))
        if cancel_event is not None and cancel_event.is_set():
            raise LoadCancelled("Load cancelled.")
        threads = auto_threads()
        # A caller-chosen context (the bridge's num_ctx) wins
        # over the machine pick, clamped so a nonsense value can
        # never reach the engine: at least 128 tokens.
        if isinstance(n_ctx, int) and not isinstance(
                n_ctx, bool) and n_ctx > 0:
            context = max(128, n_ctx)
        else:
            context = auto_context_tokens(self._free_ram())
        # The architecture, read once: the vision check below and
        # the unsupported-design error both need it (task 8).
        from .core import (VISION_ARCHITECTURES, find_projector,
                           read_gguf_metadata)
        architecture = read_gguf_metadata(path).get(
            "general.architecture")
        # Vision: a projector file next to the model means the
        # model can see. It loads through the engine's mtmd
        # handler (CPU only). A projector that cannot be used
        # never blocks the load: the model still loads, as text
        # only, and the warning says why it cannot see.
        # A store model names its projector explicitly (v0.13):
        # in Ollama's store every blob is hash-named, so the
        # folder search beside the model can never find it. The
        # explicit file wins when it is really there; the folder
        # search is the fallback, unchanged for folder models.
        projector_file = None
        if projector is not None and os.path.isfile(str(projector)):
            projector_file = projector
        if projector_file is None:
            projector_file = find_projector(path)
        projector = projector_file
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
        elif architecture in VISION_ARCHITECTURES:
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
            text = str(exc)
            # An architecture the engine does not know is not a
            # broken file: say so plainly (task 8). The C++ error
            # names the architecture, but only on stderr, so the
            # Python exception never carries it: match the text
            # when it is there, and fall back on the header's
            # architecture against the known-unsupported set.
            if ("unknown model architecture" in text.lower()
                    or (architecture is not None
                        and architecture.lower()
                        in UNSUPPORTED_ARCHITECTURES)):
                arch = (architecture or "unknown").upper()
                raise RunnerError(
                    "%s uses the %s design, which Fyrn's engine "
                    "does not support yet. The file itself is "
                    "fine." % (name, arch))
            # Never show the file's path in the message: an
            # Ollama model is a blob hash, not a name (task 11).
            safe = text.replace(str(path), name)
            raise RunnerError(
                "The model could not be loaded (%s). The file may "
                "be broken or not a GGUF model." % safe)
        if cancel_event is not None and cancel_event.is_set():
            self._llm = None
            raise LoadCancelled("Load cancelled.")
        if progress_cb is not None:
            progress_cb(100, "Loaded")
        self.model_path = str(path)
        self.context_tokens = context
        self._vision = chat_handler is not None
        self._tools = _template_supports_tools(self._llm)
        self.projector_path = (str(projector)
                               if chat_handler is not None else None)
        # The model's own parameters and system prompt ride
        # with the load (v0.13): every generation below passes
        # them to the engine.
        self._model_params = dict(model_params or {})
        self._model_system = (str(model_system)
                              if model_system else None)
        result = {"load_time_s": time.monotonic() - started}
        if vision_warning:
            result["vision_warning"] = vision_warning
        return result

    def unload(self):
        # Dropping the reference frees the model; the backend holds
        # no other state. The vision state goes with it.
        self._llm = None
        self._vision = False
        self._tools = False
        self.projector_path = None
        self._model_params = {}
        self._model_system = None
        super().unload()

    # -- the model's own settings, the token budget (v0.13) --

    def _sampling_kwargs(self):
        """The loaded model's own sampling parameters as engine
        kwargs: only the ones its Modelfile actually specified
        (an unspecified parameter keeps the engine default,
        exactly as before). The reply cap is NOT among them:
        max_tokens stays Fyrn's setting. The model's stop
        sequences are passed as the call's stops; the engine's
        own end-of-turn handling still applies on top of them."""
        params = self._model_params or {}
        kwargs = {}
        for key in ("temperature", "top_p", "top_k",
                    "presence_penalty", "frequency_penalty",
                    "repeat_penalty"):
            if key in params:
                kwargs[key] = params[key]
        if params.get("stop"):
            kwargs["stop"] = list(params["stop"])
        return kwargs

    def _system_text(self, instructions):
        """The system prompt for a turn: the chat's own
        instructions when it has any (Fyrn's setting wins), else
        the model's own system prompt from its manifest, else
        None."""
        if instructions and str(instructions).strip():
            return str(instructions)
        if self._model_system and self._model_system.strip():
            return self._model_system
        return None

    def _tokenize(self, text):
        """Tokenize text with the loaded model's tokenizer, for
        counting. Special tokens in the text count as themselves
        (the rendered prompt is full of them)."""
        data = (text.encode("utf-8") if isinstance(text, str)
                else text)
        try:
            return self._llm.tokenize(data, add_bos=False,
                                      special=True)
        except TypeError:
            return self._llm.tokenize(data)

    def _count_prompt_tokens(self, messages, tools=None,
                             image_count=0):
        """How many tokens the engine will spend on this prompt:
        the chat template's own rendering of the messages,
        tokenized. The tools block is rendered in when tools are
        on offer (it is part of the prompt, and on a small window
        it is half of it). Each attached picture adds a flat
        allowance: the vision handler expands a picture into
        tokens the text count cannot see. When the template
        cannot be rendered (a stub engine, a broken template),
        the count falls back to the message texts plus a
        per-message framing allowance: an estimate, but one that
        still grows with the history, which is all the budget
        below needs to trim against."""
        count = None
        template = None
        try:
            metadata = getattr(self._llm, "metadata", None) or {}
            template = metadata.get("tokenizer.chat_template")
        except Exception:
            template = None
        if template:
            try:
                from llama_cpp.llama_chat_format import (
                    Jinja2ChatFormatter)
                eos = bos = ""
                try:
                    eos = self._llm.detokenize(
                        [self._llm.token_eos()]).decode(
                            "utf-8", "replace")
                    bos = self._llm.detokenize(
                        [self._llm.token_bos()]).decode(
                            "utf-8", "replace")
                except Exception:
                    pass
                formatter = Jinja2ChatFormatter(
                    template=template, eos_token=eos,
                    bos_token=bos)
                plain = []
                for message in messages:
                    content = message.get("content")
                    if isinstance(content, list):
                        content = "".join(
                            str(part.get("text", ""))
                            for part in content
                            if isinstance(part, dict))
                    entry = {"role": message.get("role"),
                             "content": content}
                    if message.get("tool_calls"):
                        entry["tool_calls"] = message["tool_calls"]
                    plain.append(entry)
                rendered = formatter(
                    messages=plain, tools=tools).prompt
                count = len(self._tokenize(rendered))
            except Exception:
                count = None
        if count is None:
            total = 0
            for message in messages:
                content = message.get("content")
                if isinstance(content, list):
                    content = "".join(
                        str(part.get("text", ""))
                        for part in content
                        if isinstance(part, dict))
                total += len(self._tokenize(str(content or "")))
                total += 8  # role markers and template framing
            if tools:
                total += len(self._tokenize(json.dumps(tools)))
            count = total
        if image_count:
            # One picture's tokens through the vision handler,
            # as a flat allowance: the guard only needs the
            # right order of magnitude.
            count += 512 * image_count
        return count

    # Room left at the top of the window after a fitted turn:
    # the count above is the template's rendering, and a token
    # or two of framing slop must never be the difference
    # between a reply and an engine error.
    _CONTEXT_MARGIN_TOKENS = 16

    def _fit_messages(self, messages, cap, tools=None,
                      image_count=0):
        """Fit a turn to the loaded window, by tokens (v0.13).

        messages is [system?] + history + [new user message].
        While the rendered prompt plus the full reply cap would
        overflow the window, the OLDEST history exchanges are
        dropped (the system message and the newest exchange are
        never dropped). What cannot be trimmed away is priced
        into a smaller reply cap instead. Returns (messages,
        effective_cap). Raises RunnerError with
        CHAT_TOO_LONG_MESSAGE when even the newest exchange
        alone leaves no room for a single reply token: the turn
        does not run, and the user is told in plain words. The
        trimming is per-turn only; the caller's history (and the
        saved chat) keeps everything.

        With no known window (a runner that never recorded one),
        the messages and cap pass through untouched.
        """
        n_ctx = self.context_tokens
        if not n_ctx:
            return messages, cap
        system = []
        body = list(messages)
        if body and body[0].get("role") == "system":
            system = body[:1]
            body = body[1:]
        if not body:
            return messages, cap
        history, current = body[:-1], body[-1]
        # The newest exchange starts at the last user message in
        # the history; everything from there on is untouchable.
        # Drops come off the front, so the protected tail is a
        # length, not an index: the last protected_len messages.
        newest_start = 0
        for index, message in enumerate(history):
            if message.get("role") == "user":
                newest_start = index
        protected_len = len(history) - newest_start

        def prompt_tokens(hist):
            return self._count_prompt_tokens(
                system + hist + [current], tools=tools,
                image_count=image_count)

        count = prompt_tokens(history)
        while count + cap > n_ctx and len(history) > protected_len:
            # Drop the oldest exchange: its first message, and
            # the replies that belonged to it (everything up to
            # the next user message or the protected tail).
            drop = 1
            while (drop < len(history) - protected_len
                   and history[drop].get("role") != "user"):
                drop += 1
            history = history[drop:]
            count = prompt_tokens(history)
        room = n_ctx - count - self._CONTEXT_MARGIN_TOKENS
        if room < 1:
            raise RunnerError(CHAT_TOO_LONG_MESSAGE)
        return system + history + [current], min(cap, room)

    def _response_tokens(self, response, message):
        """The tokens one engine round generated: the engine's
        own completion count when it reports one, else the
        message it returned, tokenized (its text plus any tool
        calls it made)."""
        usage = (response or {}).get("usage") or {}
        counted = usage.get("completion_tokens")
        if isinstance(counted, int) and counted >= 0:
            return counted
        text = (message or {}).get("content") or ""
        for call in _native_tool_calls(message or {}):
            text += json.dumps(call.get("arguments") or {})
        try:
            return len(self._tokenize(text))
        except Exception:
            return max(1, len(text) // 4)

    def generate(self, prompt, on_token=None, cancel_event=None,
                 history=None, images=None,
                 instructions=None, max_tokens=None):
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
        # The system prompt leads: the chat's instructions when
        # it has any, else the model's own (v0.8, v0.13).
        messages = []
        system_text = self._system_text(instructions)
        if system_text is not None:
            messages.append({"role": "system",
                             "content": system_text})
        messages.extend(trim_history(history or []))
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
        # The token budget (v0.13): the rendered prompt is
        # counted in tokens, the oldest exchanges dropped, and
        # the cap shrunk to what fits, so a long chat can no
        # longer die of overflow. Raises the plain-words
        # too-long error when nothing fits at all.
        messages, cap = self._fit_messages(
            messages, reply_cap(max_tokens),
            image_count=len(images or []))
        try:
            stream = self._llm.create_chat_completion(
                messages=messages,
                stream=True,
                max_tokens=cap,
                **self._sampling_kwargs(),
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
            raise RunnerError(failure_message(exc))
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

    def generate_tools(self, prompt, history=None, instructions=None,
                       images=None, execute_tool=None, on_token=None,
                       on_event=None, cancel_event=None,
                       max_tokens=None):
        """A reply with tools on offer (v0.8).

        Rounds of chat completion with the tool definitions: the
        model's calls run through execute_tool and the results go
        back as tool messages, for at most MAX_TOOL_ROUNDS rounds;
        the final answer is then handed to on_token in pieces.
        Tool rounds do not stream (a round is a call, not prose),
        so the reply appears once the model answers — the tool
        lines stream live through on_event meanwhile.
        """
        if not self.is_loaded or self._llm is None:
            raise RunnerError("No model is loaded.")
        if images and not self._vision:
            raise RunnerError(
                "This model cannot see images. Load a vision "
                "model (one with its projector file next to it) "
                "to ask about a picture.")
        if execute_tool is None:
            raise ToolsUnsupportedError(
                "This model cannot use tools.")
        started = time.monotonic()
        messages = []
        system_text = self._system_text(instructions)
        if system_text is not None:
            messages.append({"role": "system",
                             "content": system_text})
        messages.extend(trim_history(history or []))
        if images:
            parts = [{"type": "text", "text": prompt}]
            for image_path in images:
                try:
                    url = _image_data_url(image_path)
                except OSError as exc:
                    raise RunnerError(
                        "That picture could not be read (%s)."
                        % exc)
                parts.append({"type": "image_url",
                              "image_url": {"url": url}})
            messages.append({"role": "user", "content": parts})
        else:
            messages.append({"role": "user", "content": prompt})
        # The token budget (v0.13), with the tools block priced
        # into the rendered prompt: it is part of what the
        # engine is sent, and on a small window it is half of
        # it. Same rule as the plain path: trim oldest first,
        # shrink the cap to what fits, never die of overflow.
        messages, cap = self._fit_messages(
            messages, reply_cap(max_tokens), tools=WEB_TOOL_DEFINITIONS,
            image_count=len(images or []))
        sampling = self._sampling_kwargs()

        def cancelled():
            return cancel_event is not None and cancel_event.is_set()

        # Stats honesty (v0.13): the numbers describe the WHOLE
        # turn. "tokens" counts every token the engine generated
        # across all rounds (tool rounds included), "speed" is
        # that total over the turn's whole elapsed time, and
        # "first token" is stamped when the final answer's first
        # piece is actually handed to the UI (tool rounds do not
        # stream, so nothing can be stamped earlier). Before
        # v0.13 the count covered only the final round's text
        # while the time covered every round, so a long tool
        # turn showed a speed several times slower than the
        # engine's real decode rate.
        generated_tokens = 0

        def finish(text):
            """Emit the final answer in pieces; build the stats."""
            ttft_s = None
            if text and on_token is not None:
                for piece in re.findall(r"\S+\s*|\s+", text):
                    if cancelled():
                        raise GenerationCancelled("Reply stopped.")
                    if ttft_s is None:
                        ttft_s = time.monotonic() - started
                    on_token(piece)
            elapsed_s = time.monotonic() - started
            if ttft_s is None:
                ttft_s = elapsed_s
            try:
                tokens = len(self._llm.tokenize(
                    text.encode("utf-8")))
            except Exception:
                tokens = max(1, len(text) // 4)
            tokens += generated_tokens
            return {"tokens": tokens,
                    "elapsed_s": elapsed_s,
                    "ttft_s": ttft_s,
                    "tokens_per_sec": compute_tokens_per_sec(
                        tokens, elapsed_s)}

        rounds = 0
        while True:
            if cancelled():
                raise GenerationCancelled("Reply stopped.")
            try:
                response = self._llm.create_chat_completion(
                    messages=messages,
                    tools=WEB_TOOL_DEFINITIONS,
                    tool_choice="auto",
                    stream=False,
                    max_tokens=cap,
                    **sampling,
                )
            except GenerationCancelled:
                raise
            except Exception as exc:
                if rounds == 0 and _looks_like_tool_problem(exc):
                    raise ToolsUnsupportedError(
                        "This model cannot use tools.")
                raise RunnerError(failure_message(exc))
            choices = response.get("choices") or []
            message = (choices[0].get("message")
                       if choices else None) or {}
            calls = _native_tool_calls(message)
            content = message.get("content") or ""
            parsed_text = False
            if not calls and content:
                text_calls, has_calls = parse_tool_calls(content)
                if has_calls:
                    calls = [{"id": "", "name": call["name"],
                              "arguments": call["arguments"],
                              "args_error": None}
                             for call in text_calls]
                    parsed_text = True
            if not calls:
                return finish(content)
            # This round generated tokens too (v0.13): they count
            # toward the turn's stats, not just the final answer.
            generated_tokens += self._response_tokens(response,
                                                      message)
            rounds += 1
            if rounds > MAX_TOOL_ROUNDS:
                # The model kept asking: one last round with no
                # tools on offer, so the turn still ends in an
                # answer built from the results it already has.
                try:
                    response = self._llm.create_chat_completion(
                        messages=messages,
                        stream=False,
                        max_tokens=cap,
                        **sampling,
                    )
                    choices = response.get("choices") or []
                    message = (choices[0].get("message")
                               if choices else None) or {}
                    return finish(message.get("content") or "")
                except Exception:
                    return finish(
                        "I used my tools %d times and still could "
                        "not finish an answer. The tool results "
                        "above are what I found." % MAX_TOOL_ROUNDS)
            if parsed_text:
                messages.append({"role": "assistant",
                                 "content": content})
            else:
                messages.append({
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": message.get("tool_calls"),
                })
            for call in calls:
                if cancelled():
                    raise GenerationCancelled("Reply stopped.")
                if on_event is not None:
                    on_event("call", {"name": call["name"],
                                      "arguments":
                                          call["arguments"]})
                if call["args_error"] is not None:
                    result = ("The tool call's arguments could not "
                              "be read as JSON, so the tool was "
                              "not run.")
                    summary = result
                else:
                    result, summary = execute_tool(
                        call["name"], call["arguments"])
                if on_event is not None:
                    on_event("result", {"name": call["name"],
                                        "summary": summary,
                                        "result": result})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": result,
                })

    def generate_turn(self, messages, tools=None, images=None,
                      cancel_event=None, max_tokens=None):
        """ONE chat turn with the CLIENT's tools on offer (the
        bridge's pass-through, v0.10).

        The same completion machinery generate_tools uses —
        native tool_calls first, the text forms parsed from the
        reply when the model wrote them as text — but the loop
        stops after one round: the calls are returned to the
        caller unexecuted, and the caller runs them and comes
        back with the results as role "tool" messages. Nothing
        here touches a file, a command, or Fyrn's own tool
        definitions.

        A model whose template cannot take tools gets one plain
        completion instead and answers in text, the way Ollama
        behaves with such a model. The conversation is the
        client's to manage: unlike generate(), nothing is
        trimmed here, so a tool call and its result can never be
        split apart by a trim.
        """
        if not self.is_loaded or self._llm is None:
            raise RunnerError("No model is loaded.")
        if images and not self._vision:
            raise RunnerError(
                "This model cannot see images. Load a vision "
                "model (one with its projector file next to it) "
                "to ask about a picture.")
        if cancel_event is not None and cancel_event.is_set():
            raise GenerationCancelled("Reply stopped.")
        started = time.monotonic()
        engine_messages = [dict(m) for m in (messages or [])
                           if isinstance(m, dict)]
        if images:
            parts = []
            attached = False
            for index in range(len(engine_messages) - 1, -1, -1):
                if engine_messages[index].get("role") == "user":
                    text = engine_messages[index].get(
                        "content") or ""
                    parts = [{"type": "text", "text": text}]
                    engine_messages[index] = {
                        "role": "user", "content": parts}
                    attached = True
                    break
            if not attached:
                parts = [{"type": "text", "text": ""}]
                engine_messages.append(
                    {"role": "user", "content": parts})
            for image_path in images:
                try:
                    url = _image_data_url(image_path)
                except OSError as exc:
                    raise RunnerError(
                        "That picture could not be read (%s)."
                        % exc)
                parts.append({"type": "image_url",
                              "image_url": {"url": url}})

        def complete(with_tools):
            kwargs = {"messages": engine_messages,
                      "stream": False,
                      "max_tokens": reply_cap(max_tokens)}
            if with_tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            response = self._llm.create_chat_completion(**kwargs)
            choices = response.get("choices") or []
            return (choices[0].get("message")
                    if choices else None) or {}

        try:
            message = complete(bool(tools))
        except GenerationCancelled:
            raise
        except Exception as exc:
            if tools and _looks_like_tool_problem(exc):
                # The template cannot take the tool surface:
                # one plain completion, answered as text.
                try:
                    message = complete(False)
                except Exception as exc2:
                    raise RunnerError(
                        "The model stopped with an error (%s)."
                        % exc2)
            else:
                raise RunnerError(
                    "The model stopped with an error (%s)." % exc)
        if cancel_event is not None and cancel_event.is_set():
            raise GenerationCancelled("Reply stopped.")
        calls = _native_tool_calls(message)
        content = message.get("content") or ""
        if not calls and content:
            text_calls, has_calls = parse_tool_calls(content)
            if has_calls:
                calls = [{"id": "", "name": call["name"],
                          "arguments": call["arguments"],
                          "args_error": None}
                         for call in text_calls]
                # The calls were the reply: in Ollama's shape
                # they ride as tool_calls, not as text.
                content = ""
        elapsed_s = time.monotonic() - started
        counted = content
        if calls:
            counted += json.dumps(
                [c.get("arguments") or {} for c in calls])
        try:
            tokens = len(self._llm.tokenize(
                counted.encode("utf-8")))
        except Exception:
            tokens = max(1, len(counted) // 4)
        return {"content": content,
                "tool_calls": [{"id": c["id"], "name": c["name"],
                                "arguments": c["arguments"]}
                               for c in calls],
                "stats": {"tokens": tokens,
                          "elapsed_s": elapsed_s,
                          "ttft_s": elapsed_s,
                          "tokens_per_sec":
                              compute_tokens_per_sec(tokens,
                                                     elapsed_s)}}
