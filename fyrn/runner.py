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

from .tools import TOOL_DEFINITIONS, parse_tool_calls

# A reply never runs past this many tokens: a model that never
# emits its stop token would otherwise run to context fill, which
# is up to ~10 minutes on this PC. 1024 is a full answer from a
# 2B model, and Stop still cuts it short.
MAX_GENERATION_TOKENS = 1024

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

    @property
    def is_loaded(self):
        return self.model_path is not None

    @property
    def model_name(self):
        if self.model_path is None:
            return ""
        return os.path.basename(str(self.model_path))

    def load(self, path, progress_cb=None, cancel_event=None,
             display_name=None):
        """Load a model file. Returns {"load_time_s": float}.

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

    def generate(self, prompt, on_token=None, cancel_event=None,
                 history=None, images=None,
                 instructions=None):
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

    def generate_tools(self, prompt, history=None, instructions=None,
                       images=None, execute_tool=None, on_token=None,
                       on_event=None, cancel_event=None):
        """Reply with tools on offer (v0.8).

        The model may call tools mid-reply: each call is reported
        through on_event("call", {...}), run through execute_tool
        (a callable(name, arguments) -> (result_text, summary)),
        reported through on_event("result", {...}), and the result
        goes back to the model. After at most MAX_TOOL_ROUNDS
        rounds the final answer streams through on_token as usual.
        Returns the same stats dict as generate(). Runners whose
        model cannot do this raise ToolsUnsupportedError.
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

    def load(self, path, progress_cb=None, cancel_event=None,
             display_name=None):
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
        return {"load_time_s": time.monotonic() - started}

    def unload(self):
        super().unload()

    def generate(self, prompt, on_token=None, cancel_event=None,
                 history=None, images=None,
                 instructions=None):
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

    def generate_tools(self, prompt, history=None, instructions=None,
                       images=None, execute_tool=None, on_token=None,
                       on_event=None, cancel_event=None):
        if not self.is_loaded:
            raise RunnerError("No model is loaded.")
        if not self.supports_tools:
            raise ToolsUnsupportedError(
                "This model cannot use tools.")
        self.last_history = list(history or [])
        self.last_images = list(images or [])
        self.last_instructions = instructions
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
        if on_token is not None:
            on_token(final_text)
        elapsed_s = time.monotonic() - started
        tokens = max(1, len(final_text.split()))
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
             display_name=None):
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
        self._vision = chat_handler is not None
        self._tools = _template_supports_tools(self._llm)
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
        self._tools = False
        self.projector_path = None
        super().unload()

    def generate(self, prompt, on_token=None, cancel_event=None,
                 history=None, images=None,
                 instructions=None):
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
        # The chat's instructions, when it has any, lead as the
        # system prompt (v0.8).
        messages = []
        if instructions and str(instructions).strip():
            messages.append({"role": "system",
                             "content": str(instructions)})
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

    def generate_tools(self, prompt, history=None, instructions=None,
                       images=None, execute_tool=None, on_token=None,
                       on_event=None, cancel_event=None):
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
        if instructions and str(instructions).strip():
            messages.append({"role": "system",
                             "content": str(instructions)})
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

        def cancelled():
            return cancel_event is not None and cancel_event.is_set()

        def finish(text):
            """Emit the final answer in pieces; build the stats."""
            ttft_s = time.monotonic() - started
            if text and on_token is not None:
                for piece in re.findall(r"\S+\s*|\s+", text):
                    if cancelled():
                        raise GenerationCancelled("Reply stopped.")
                    on_token(piece)
            elapsed_s = time.monotonic() - started
            try:
                tokens = len(self._llm.tokenize(
                    text.encode("utf-8")))
            except Exception:
                tokens = max(1, len(text) // 4)
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
                    tools=TOOL_DEFINITIONS,
                    tool_choice="auto",
                    stream=False,
                    max_tokens=MAX_GENERATION_TOKENS,
                )
            except GenerationCancelled:
                raise
            except Exception as exc:
                if rounds == 0 and _looks_like_tool_problem(exc):
                    raise ToolsUnsupportedError(
                        "This model cannot use tools.")
                raise RunnerError(
                    "The model stopped with an error (%s)." % exc)
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
            rounds += 1
            if rounds > MAX_TOOL_ROUNDS:
                # The model kept asking: one last round with no
                # tools on offer, so the turn still ends in an
                # answer built from the results it already has.
                try:
                    response = self._llm.create_chat_completion(
                        messages=messages,
                        stream=False,
                        max_tokens=MAX_GENERATION_TOKENS,
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
