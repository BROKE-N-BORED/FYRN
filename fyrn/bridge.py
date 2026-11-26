"""The bridge: Fyrn's engine, offered to other programs
(v0.9, tools pass-through v0.10).

An optional local adapter, off by default. When the Settings
toggle is on, Fyrn serves a small HTTP API on 127.0.0.1 (this
PC only, never the network) shaped like Ollama's, because the
programs that will use it (Code Forge, STELLA) already speak
that shape to Ollama. Fyrn itself is still its own thing: this
module is an adapter on the side, translating Ollama-shaped
requests into Fyrn's own runner calls. The full contract for
the other programs' builders is BRIDGE.md at the repo root.

The rules are Fyrn's rules, unchanged:

- One model at a time. A request naming a different model
  loads it (unloading the current one), after the same RAM fit
  check the window uses.
- The window wins. While the GUI is mid-reply, mid-tool-loop,
  or mid-load, a bridge generation is refused with a clear
  error instead of yanking the model out from under the user.
  Two bridge generations never run at once either.
- Tools PASS THROUGH (v0.10): a chat request's tools are
  offered to the model and its calls come back in the reply,
  exactly as Ollama does it. The bridge never executes a tool:
  running them is the client's job, with the client's own
  files and approvals. Fyrn's own tool executor is a GUI-chat
  feature and is never involved here.

Standard library only, no Qt: the HTTP handlers run on their
own threads, and everything GUI-shaped (the model list, the
busy check) arrives as callbacks from the window.
"""

import base64
import json
import queue as _queue
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import (ModelNotFoundError, ModelTooBigError,
                   get_home, read_gguf_facts)
from .runner import GenerationCancelled, RunnerError, reply_cap

# Ollama's port, on purpose: the client programs are already
# pointed at 127.0.0.1:11434 for Ollama, so they find Fyrn
# where they expect a local model server to be. Only one
# program can hold the port: if Ollama itself is running, the
# bridge cannot start, and says so (see BridgeService.start).
DEFAULT_PORT = 11434

# The largest request body the bridge reads. A chat request is
# text; anything bigger is not a chat request.
MAX_BODY_BYTES = 8 * 1024 * 1024


class BridgeError(Exception):
    """A request the bridge refuses, with its HTTP status."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _now_iso():
    """A timestamp the way Ollama writes it: UTC, microseconds,
    trailing Z."""
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _mtime_iso(path):
    """A file's modified time in the same format, or None."""
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(
        stamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def iso_seconds_from_now(seconds):
    """An ISO timestamp `seconds` from now, in _now_iso's
    format. The window uses it to project the idle auto-unload
    moment for /api/ps's expires_at."""
    return (datetime.now(timezone.utc)
            + timedelta(seconds=seconds)).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ")


def _digest_for(path):
    """The Ollama-store digest for a model file whose name is
    its blob hash ("sha256-..."), else None."""
    blob = Path(path).name
    if blob.startswith("sha256-"):
        return "sha256:" + blob[len("sha256-"):]
    return None


class BridgeService:
    """The bridge's brain: model list, model switching, and
    generation, with the HTTP layer as a thin shell over it.

    controller is Fyrn's ModelController (the one-model rule
    and the RAM check live there). list_models_fn() returns the
    models Fyrn can see, as dicts with name, path, and size
    (the scan/discovery dicts). is_busy_fn() returns True while
    the GUI is mid-reply, mid-tool-loop, or mid-load.
    max_tokens_fn() returns the reply-length cap for requests
    that do not carry their own. on_activity() is called for
    requests that use the engine (the idle auto-unload timer
    listens to it). expires_at_fn() returns the ISO moment the
    loaded model would idle-unload, or None when no projection
    applies (/api/ps).
    """

    def __init__(self, controller, list_models_fn, is_busy_fn=None,
                 max_tokens_fn=None, on_activity=None,
                 expires_at_fn=None,
                 port=DEFAULT_PORT, version=""):
        self.controller = controller
        self._list_models = list_models_fn
        self._is_busy = is_busy_fn or (lambda: False)
        self._max_tokens_fn = max_tokens_fn
        self._on_activity = on_activity or (lambda: None)
        self._expires_at_fn = expires_at_fn
        self.port = port
        self.version = version
        self._server = None
        self._thread = None
        # Why the last start() failed, in plain words ("" when
        # it started, or was never tried).
        self.start_error = ""
        # True while a bridge generation runs. The window reads
        # it to hold off its own load/unload/send, so a model is
        # never yanked out from under a bridge reply either.
        self.generating = False

    # ------------------------------------------------------------ server

    @property
    def running(self):
        return self._server is not None

    def start(self):
        """Start serving on 127.0.0.1:self.port. Returns True
        when serving. When the port is taken (Ollama running,
        or another Fyrn), returns False with start_error set,
        in plain words: a failed bridge is a status line, never
        a crash."""
        if self._server is not None:
            return True
        handler = _make_handler(self)
        try:
            self._server = ThreadingHTTPServer(
                ("127.0.0.1", self.port), handler)
        except OSError:
            self._server = None
            self.start_error = (
                "port %d is already in use. If Ollama is "
                "running, close it, or turn the bridge off and "
                "on again." % self.port)
            return False
        self.start_error = ""
        # The port actually bound (a test may pass port 0 and
        # read the real one back here).
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="fyrn-bridge", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """Stop serving and free the port."""
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------ endpoints

    def tags(self):
        """GET /api/tags: every model Fyrn can see, Ollama's
        shape: name, model, modified_at, size (and digest for
        Ollama-store models, whose blob name is the digest)."""
        self._on_activity()
        models = []
        for entry in self._list_models():
            item = {"name": entry["name"],
                    "model": entry["name"],
                    "size": entry.get("size", 0)}
            modified = _mtime_iso(entry["path"])
            if modified is not None:
                item["modified_at"] = modified
            digest = _digest_for(entry["path"])
            if digest is not None:
                item["digest"] = digest
            models.append(item)
        return {"models": models}

    def ps(self):
        """GET /api/ps: the model Fyrn currently holds, in
        Ollama's shape, or an empty list. A pure status read: it
        does not count as activity, so polling it never keeps a
        model alive past its idle unload — and expires_at is
        that honest projection (present only when idle
        auto-unload is on)."""
        models = []
        controller = self.controller
        if controller.is_loaded and controller.current_path:
            path = Path(controller.current_path)
            item = {"name": controller.current_name,
                    "model": controller.current_name}
            try:
                item["size"] = path.stat().st_size
            except OSError:
                item["size"] = 0
            digest = _digest_for(path)
            if digest is not None:
                item["digest"] = digest
            modified = _mtime_iso(path)
            if modified is not None:
                item["modified_at"] = modified
            if self._expires_at_fn is not None:
                expires = self._expires_at_fn()
                if expires:
                    item["expires_at"] = expires
            models.append(item)
        return {"models": models}

    def show(self, payload):
        """POST /api/show: what Fyrn honestly knows about one
        model, in Ollama's shape as far as Fyrn can fill it:
        name, size, digest (Ollama-store models), details with
        format "gguf" and the family from the GGUF header, and
        model_info with the trained context length. When this
        model is the loaded one, loaded_context_tokens names
        the context it is actually running with. Also a pure
        status read (no activity counted)."""
        if not isinstance(payload, dict):
            raise BridgeError(400, "The request was not a JSON "
                                   "object.")
        name = payload.get("model") or payload.get("name")
        if not name or not isinstance(name, str):
            raise BridgeError(400, "The request names no model.")
        entry = self._find_model(name)
        if entry is None:
            raise BridgeError(
                404, "model '%s' not found" % name)
        path = Path(entry["path"])
        item = {"name": entry["name"], "model": entry["name"],
                "size": entry.get("size", 0)}
        modified = _mtime_iso(path)
        if modified is not None:
            item["modified_at"] = modified
        digest = _digest_for(path)
        if digest is not None:
            item["digest"] = digest
        facts = read_gguf_facts(path)
        arch = facts.get("architecture")
        details = {"format": "gguf", "family": arch or ""}
        if facts.get("params_text"):
            details["parameter_size"] = facts["params_text"]
        if facts.get("quantization"):
            details["quantization_level"] = facts["quantization"]
        if arch:
            details["families"] = [arch]
        item["details"] = details
        model_info = {}
        if arch:
            model_info["general.architecture"] = arch
            if facts.get("context_length"):
                model_info["%s.context_length" % arch] = (
                    facts["context_length"])
        item["model_info"] = model_info
        controller = self.controller
        if (controller.is_loaded and controller.current_path
                and str(controller.current_path) == str(path)):
            context = getattr(controller.runner,
                              "context_tokens", None)
            if context:
                item["loaded_context_tokens"] = context
        return item

    def version_info(self):
        """GET /api/version: Fyrn's own version (the bridge is
        Fyrn, not Ollama; the shape is Ollama's)."""
        self._on_activity()
        return {"version": self.version}

    def generate_events(self, payload, chat=False):
        """Run one generation and yield its events.

        Each event is a dict: zero or more {"piece": text}
        events, then exactly one final event with the whole
        reply under "text" plus "stats" (and "tool_calls" when
        the model asked for tools — the calls are the client's
        to run; the bridge never executes them). The HTTP layer
        turns the events into Ollama's streaming lines or its
        single non-streaming object. Raises BridgeError for
        every refusal (busy, unknown model, bad request, fit)
        and RunnerError for an engine failure.
        """
        self._on_activity()
        if not isinstance(payload, dict):
            raise BridgeError(400, "The request was not a JSON "
                                   "object.")
        name = payload.get("model")
        if not name or not isinstance(name, str):
            raise BridgeError(400, "The request names no model.")
        unload_after = _keep_alive_zero(payload)
        if chat:
            request = _chat_request(payload)
            empty = not request["has_messages"]
        else:
            request = None
            prompt = payload.get("prompt")
            empty = not (isinstance(prompt, str) and prompt)
        if empty and unload_after:
            # Ollama's unload idiom: an empty request with
            # keep_alive 0 unloads the named model and answers
            # done. It never loads anything.
            yield from self._unload_now(name)
            return
        if chat:
            prompt = request["prompt"]
            history = request["history"]
            instructions = request["instructions"]
            tools = request["tools"]
            turn_messages = request["turn_messages"]
            images_b64 = request["images"]
            raw_images = payload.get("images")
            if isinstance(raw_images, list):
                images_b64 = images_b64 + [
                    item for item in raw_images
                    if isinstance(item, str)]
        else:
            if empty:
                raise BridgeError(
                    400, "The request has no prompt.")
            prompt = payload.get("prompt")
            history = []
            instructions = None
            system = payload.get("system")
            if isinstance(system, str) and system.strip():
                instructions = system
            tools = None
            turn_messages = None
            images_b64 = []
            raw_images = payload.get("images")
            if isinstance(raw_images, list):
                images_b64 = [item for item in raw_images
                              if isinstance(item, str)]
        if self._is_busy():
            raise BridgeError(
                409, "Fyrn is busy: a reply is being written in "
                     "the Fyrn window. Try again when it "
                     "finishes.")
        if self.generating:
            raise BridgeError(
                409, "Fyrn is busy answering another program "
                     "through the bridge. Try again when it "
                     "finishes.")
        image_paths = _write_temp_images(images_b64)
        try:
            self._ensure_model(
                name, num_ctx=_request_num_ctx(payload))
            if image_paths and not getattr(
                    self.controller.runner, "supports_vision",
                    False):
                raise BridgeError(
                    400, "The model '%s' cannot see pictures. "
                         "Load a vision model (one with a "
                         "projector file) to send it pictures."
                         % name)
            cap = _request_cap(payload, self._max_tokens_fn)
            cancel_event = threading.Event()
            queue = _queue.Queue()
            runner = self.controller.runner

            def run():
                try:
                    if tools is not None:
                        result = runner.generate_turn(
                            turn_messages, tools=tools,
                            images=image_paths or None,
                            cancel_event=cancel_event,
                            max_tokens=cap)
                        queue.put(("turn", result))
                    else:
                        stats = runner.generate(
                            prompt,
                            on_token=lambda piece: queue.put(
                                ("piece", piece)),
                            cancel_event=cancel_event,
                            history=history,
                            images=image_paths or None,
                            instructions=instructions,
                            max_tokens=cap)
                        queue.put(("done", stats))
                except GenerationCancelled:
                    # The client went away mid-reply: what was
                    # written still gets its final event, marked
                    # stopped.
                    queue.put(("done", {"tokens": 0,
                                        "stopped": True}))
                except RunnerError as exc:
                    queue.put(("error", exc))
                except Exception as exc:  # never leave the consumer hanging
                    queue.put(("error", RunnerError(
                        "The reply stopped with an error (%s)."
                        % exc)))
                finally:
                    self.generating = False

            self.generating = True
            started = time.monotonic()
            thread = threading.Thread(
                target=run, daemon=True,
                name="fyrn-bridge-generate")
            thread.start()
            pieces = []
            try:
                while True:
                    kind, value = queue.get()
                    if kind == "piece":
                        pieces.append(value)
                        yield {"piece": value}
                    elif kind == "turn":
                        if unload_after:
                            self._unload_loaded()
                        yield {"text": value.get("content", ""),
                               "tool_calls": _ollama_tool_calls(
                                   value.get("tool_calls")),
                               "stats": value.get("stats") or {},
                               "elapsed_s":
                                   time.monotonic() - started,
                               "model": name}
                        return
                    elif kind == "done":
                        if unload_after:
                            self._unload_loaded()
                        yield {"text": "".join(pieces),
                               "stats": value,
                               "elapsed_s":
                                   time.monotonic() - started,
                               "model": name}
                        return
                    else:  # "error"
                        raise value
            finally:
                # The consumer stopped early (a client that went
                # away): stop the generation instead of letting
                # it run out its cap for nobody.
                cancel_event.set()
        finally:
            _remove_temp_images(image_paths)

    def _unload_now(self, name):
        """The keep_alive 0 empty request: unload the named
        model when it is the loaded one, and yield the single
        done event. The GUI still wins (a busy window or a
        running bridge generation is a 409, never a yank), and
        an unknown model is the usual 404."""
        if self._is_busy():
            raise BridgeError(
                409, "Fyrn is busy: a reply is being written in "
                     "the Fyrn window. Try again when it "
                     "finishes.")
        if self.generating:
            raise BridgeError(
                409, "Fyrn is busy answering another program "
                     "through the bridge. Try again when it "
                     "finishes.")
        entry = self._find_model(name)
        if entry is None:
            raise BridgeError(
                404, "model '%s' not found" % name)
        controller = self.controller
        if (controller.is_loaded
                and str(controller.current_path)
                == str(entry["path"])):
            controller.unload()
        yield {"text": "", "stats": {"tokens": 0},
               "elapsed_s": 0.0, "model": name}

    def _unload_loaded(self):
        """Unload whatever is loaded (a keep_alive 0 request
        whose generation just finished)."""
        if self.controller.is_loaded:
            self.controller.unload()

    # ------------------------------------------------------------ internals

    def _ensure_model(self, name, num_ctx=None):
        """Have the named model loaded, loading it if needed.

        The one-model rule: a different loaded model is
        unloaded by the load, exactly as in the window. The RAM
        fit check runs first: a refusal is a clear error, never
        an attempted load.

        num_ctx (Ollama's options.num_ctx) asks for a specific
        context size: a model already loaded at another size is
        reloaded at the requested one, with the fit check's KV
        math priced at it. The value is clamped to at least 128
        tokens and, when the GGUF header names the trained
        context, to that: a window past what the model was
        trained for is not a real window."""
        entry = self._find_model(name)
        if entry is None:
            raise BridgeError(
                404, "model '%s' not found" % name)
        if num_ctx is not None:
            num_ctx = max(128, num_ctx)
            trained = read_gguf_facts(
                entry["path"]).get("context_length")
            if trained and num_ctx > trained:
                num_ctx = trained
        controller = self.controller
        if (controller.is_loaded
                and str(controller.current_path)
                == str(entry["path"])):
            if num_ctx is None:
                return
            current = getattr(controller.runner,
                              "context_tokens", None)
            if current == num_ctx:
                return
        try:
            verdict, message = controller.check_before_load(
                entry["path"], context_tokens=num_ctx)
        except ModelNotFoundError as exc:
            raise BridgeError(404, str(exc))
        if verdict == "refuse":
            raise BridgeError(500, message)
        try:
            controller.load(entry["path"], display_name=name,
                            n_ctx=num_ctx)
        except (ModelTooBigError, RunnerError) as exc:
            raise BridgeError(500, str(exc))

    def _find_model(self, name):
        """The listed model with this name, or None. Ollama
        treats a name with no tag as the :latest tag, so the
        match allows that one difference."""
        models = self._list_models()
        for entry in models:
            if entry["name"] == name:
                return entry
        if ":" not in name:
            for entry in models:
                if entry["name"] == name + ":latest":
                    return entry
        elif name.endswith(":latest"):
            for entry in models:
                if entry["name"] == name[:-len(":latest")]:
                    return entry
        return None


def _chat_request(payload):
    """Read a /api/chat payload into everything the bridge
    needs:

    - prompt / history / instructions for the plain path (the
      last user message is the prompt, the earlier user and
      assistant turns are the history, system messages become
      the instructions);
    - turn_messages for the tools path: the whole conversation
      in engine shape — system entries first, then every turn
      in order, with assistant tool_calls converted to the
      engine's shape and role "tool" results carried through;
    - images: every message's base64 images, in order;
    - tools: the request's tool definitions, validated, or
      None when the request offers none;
    - has_messages: False when the request carries no messages
      at all (the keep_alive 0 unload form checks that first).
    """
    result = {"prompt": None, "history": [], "instructions": None,
              "turn_messages": [], "images": [], "tools": None,
              "has_messages": False}
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return result
    result["has_messages"] = True
    history = []
    instructions = []
    system_entries = []
    turns = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str):
            content = ""
        images = message.get("images")
        if isinstance(images, list):
            result["images"].extend(
                item for item in images if isinstance(item, str))
        if role == "system":
            if content.strip():
                instructions.append(content)
                system_entries.append(
                    {"role": "system", "content": content})
        elif role in ("user", "assistant"):
            history.append({"role": role, "content": content})
            turn = {"role": role, "content": content}
            if role == "assistant":
                calls = _engine_tool_calls(message)
                if calls:
                    turn["tool_calls"] = calls
            turns.append(turn)
        elif role == "tool":
            turns.append({
                "role": "tool",
                "tool_call_id": message.get("tool_call_id") or "",
                "name": message.get("name") or "",
                "content": content})
    prompt = None
    for index in range(len(history) - 1, -1, -1):
        if history[index]["role"] == "user":
            prompt = history.pop(index)["content"]
            break
    if prompt is None:
        raise BridgeError(
            400, "The request has no user message.")
    result["prompt"] = prompt
    result["history"] = history
    result["instructions"] = (
        "\n".join(instructions) if instructions else None)
    result["turn_messages"] = system_entries + turns
    tools = payload.get("tools")
    if tools is not None:
        if (not isinstance(tools, list)
                or any(not isinstance(t, dict) for t in tools)):
            raise BridgeError(
                400, "The request's tools were not a list of "
                     "tool definitions.")
        if tools:
            result["tools"] = tools
    return result


def _engine_tool_calls(message):
    """An assistant message's Ollama-shaped tool_calls in the
    shape the engine's chat templates apply: arguments as a
    mapping. (The engine refuses a JSON-string arguments when
    it renders the conversation back — proven by the v0.10
    real-model drive, whose second turn failed on exactly
    that before this conversion parsed the string.) A client
    that sends arguments as a JSON string has them parsed;
    an unreadable string becomes an empty mapping."""
    calls = []
    for entry in message.get("tool_calls") or []:
        if not isinstance(entry, dict):
            continue
        function = entry.get("function") or {}
        name = function.get("name") or ""
        if not name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append({
            "id": entry.get("id") or "",
            "type": "function",
            "function": {"name": name, "arguments": arguments}})
    return calls


def _ollama_tool_calls(calls):
    """The runner's normalised calls in Ollama's response
    shape: [{"function": {"name": ..., "arguments": {...}}}],
    arguments as an object."""
    shaped = []
    for call in calls or []:
        shaped.append({"function": {
            "name": call.get("name") or "",
            "arguments": call.get("arguments") or {}}})
    return shaped


def _keep_alive_zero(payload):
    """Whether the request carries Ollama's keep_alive 0: the
    unload instruction. Accepts the number 0 and the duration
    strings that mean zero ("0", "0s", "0m", "0h"); every other
    value (a duration, -1, absent) leaves Fyrn's own idle
    setting in charge."""
    value = payload.get("keep_alive")
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return value.strip().lower() in ("0", "0s", "0m", "0h")
    return False


def _request_num_ctx(payload):
    """Ollama's options.num_ctx for one request: a positive
    int, else None (the loaded context stays as it is)."""
    options = payload.get("options")
    if isinstance(options, dict):
        value = options.get("num_ctx")
        if (isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0):
            return value
    return None


def _write_temp_images(images_b64):
    """Decode base64 images to temp files under Fyrn's home
    (the runner's vision path reads files). Returns the paths;
    the caller removes them with _remove_temp_images. A bad
    image is a plain 400: undecodable base64, or bytes that are
    neither PNG nor JPEG (the two the engine's loader takes)."""
    paths = []
    if not images_b64:
        return paths
    folder = get_home() / "bridge-images"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BridgeError(
            500, "Fyrn could not set aside room for the "
                 "picture (%s)." % exc)
    try:
        for index, item in enumerate(images_b64):
            try:
                data = base64.b64decode(item, validate=True)
            except (ValueError, TypeError):
                raise BridgeError(
                    400, "A picture in the request was not "
                         "readable base64.")
            if data.startswith(b"\x89PNG"):
                suffix = ".png"
            elif data.startswith(b"\xff\xd8"):
                suffix = ".jpg"
            else:
                raise BridgeError(
                    400, "A picture in the request was not a "
                         "PNG or JPEG image.")
            path = folder / ("bridge-%d-%d%s" % (
                threading.get_ident(), index, suffix))
            path.write_bytes(data)
            paths.append(path)
    except BridgeError:
        _remove_temp_images(paths)
        raise
    except OSError as exc:
        _remove_temp_images(paths)
        raise BridgeError(
            500, "Fyrn could not set aside room for the "
                 "picture (%s)." % exc)
    return paths


def _remove_temp_images(paths):
    for path in paths or []:
        try:
            Path(path).unlink()
        except OSError:
            pass


def _request_cap(payload, max_tokens_fn):
    """The token cap for one request: Ollama's
    options.num_predict when it is a positive number, else the
    app's reply-length setting, else the runner default."""
    options = payload.get("options")
    if isinstance(options, dict):
        num_predict = options.get("num_predict")
        if (isinstance(num_predict, int)
                and not isinstance(num_predict, bool)
                and num_predict > 0):
            return num_predict
    if max_tokens_fn is not None:
        return reply_cap(max_tokens_fn())
    return None


def _stats_fields(event):
    """Ollama's closing numbers, from Fyrn's real stats:
    durations in nanoseconds, the eval count from the reply's
    own token count."""
    stats = event.get("stats") or {}
    elapsed_s = event.get("elapsed_s") or 0.0
    fields = {"total_duration": int(elapsed_s * 1e9),
              "eval_count": int(stats.get("tokens", 0)),
              "eval_duration": int(
                  (stats.get("elapsed_s") or elapsed_s) * 1e9)}
    if stats.get("stopped"):
        fields["done_reason"] = "stop"
    return fields


def _make_handler(service):
    """The HTTP handler class for one BridgeService."""

    class BridgeHandler(BaseHTTPRequestHandler):
        # One request per connection: replies (streamed lines
        # especially) are delimited by the close, the simple
        # shape every Ollama client already handles.
        protocol_version = "HTTP/1.0"
        server_version = "FyrnBridge"

        def log_message(self, *_args):  # keep the console clean
            pass

        # -- plumbing ------------------------------------------------

        def _send_json(self, status, obj):
            body = json.dumps(obj).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type",
                                 "application/json")
                self.send_header("Content-Length",
                                 str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _send_error(self, exc):
            self._send_json(exc.status, {"error": exc.message})

        def _read_payload(self):
            length = self.headers.get("Content-Length")
            if length is None:
                raise BridgeError(
                    400, "The request has no body.")
            try:
                length = int(length)
            except ValueError:
                raise BridgeError(
                    400, "The request's length could not be "
                         "read.")
            if length < 0 or length > MAX_BODY_BYTES:
                raise BridgeError(
                    400, "The request is too large.")
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise BridgeError(
                    400, "The request was not valid JSON.")

        # -- routes --------------------------------------------------

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path == "/api/tags":
                self._send_json(200, service.tags())
            elif self.path == "/api/ps":
                self._send_json(200, service.ps())
            elif self.path == "/api/version":
                self._send_json(200, service.version_info())
            elif self.path == "/":
                body = b"Fyrn bridge is running"
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length",
                                     str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path == "/api/show":
                try:
                    payload = self._read_payload()
                    self._send_json(200, service.show(payload))
                except BridgeError as exc:
                    self._send_error(exc)
                return
            if self.path not in ("/api/generate", "/api/chat"):
                self._send_json(404, {"error": "not found"})
                return
            try:
                payload = self._read_payload()
            except BridgeError as exc:
                self._send_error(exc)
                return
            chat = self.path == "/api/chat"
            stream = True
            if isinstance(payload, dict):
                stream = payload.get("stream", True) is not False
            if stream:
                self._stream(payload, chat)
            else:
                self._whole(payload, chat)

        # -- the two reply shapes -------------------------------------

        def _whole(self, payload, chat):
            """Non-streaming: one JSON object with the whole
            reply, Ollama's shape."""
            try:
                final = None
                for event in service.generate_events(payload,
                                                     chat=chat):
                    if "text" in event:
                        final = event
                if final is None:  # cannot happen; be honest anyway
                    raise BridgeError(
                        500, "The model gave no reply.")
            except BridgeError as exc:
                self._send_error(exc)
                return
            except RunnerError as exc:
                self._send_json(500, {"error": str(exc)})
                return
            reply = {"model": final["model"],
                     "created_at": _now_iso(),
                     "done": True}
            if chat:
                message = {"role": "assistant",
                           "content": final["text"]}
                if final.get("tool_calls"):
                    message["tool_calls"] = final["tool_calls"]
                reply["message"] = message
            else:
                reply["response"] = final["text"]
            reply.update(_stats_fields(final))
            self._send_json(200, reply)

        def _stream(self, payload, chat):
            """Streaming: one JSON object per line, a line per
            piece, then a closing line with done true and the
            stats. A refusal before the first piece is a plain
            error response; the generator's refusals all happen
            before its first event, so the status is still free
            to be an error's."""
            events = service.generate_events(payload, chat=chat)
            try:
                first = next(events, None)
            except BridgeError as exc:
                self._send_error(exc)
                return
            except RunnerError as exc:
                self._send_json(500, {"error": str(exc)})
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type",
                                 "application/x-ndjson")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                return
            name = payload.get("model", "")
            gone = False

            def emit(obj):
                nonlocal gone
                if gone:
                    return
                try:
                    line = json.dumps(obj).encode("utf-8") + b"\n"
                    self.wfile.write(line)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    gone = True

            def piece_event(piece):
                event = {"model": name, "created_at": _now_iso(),
                         "done": False}
                if chat:
                    event["message"] = {
                        "role": "assistant", "content": piece}
                else:
                    event["response"] = piece
                return event

            final = None
            pending = first
            saw_piece = False
            try:
                while pending is not None:
                    if "piece" in pending:
                        saw_piece = True
                        emit(piece_event(pending["piece"]))
                        if gone:
                            # The client went away: closing the
                            # generator cancels the generation.
                            events.close()
                            return
                    elif "text" in pending:
                        final = pending
                    pending = next(events, None)
            except RunnerError as exc:
                emit({"error": str(exc), "done": True})
                return
            if final is None:
                emit({"error": "The model gave no reply.",
                      "done": True})
                return
            if final.get("tool_calls"):
                # A tools turn arrives whole (it does not
                # stream): one chunk carrying the text (usually
                # empty) and the calls, then the closing line.
                emit({"model": final["model"],
                      "created_at": _now_iso(), "done": False,
                      "message": {
                          "role": "assistant",
                          "content": final["text"],
                          "tool_calls": final["tool_calls"]}})
            elif final["text"] and not saw_piece:
                emit(piece_event(final["text"]))
            closing = {"model": final["model"],
                       "created_at": _now_iso(), "done": True}
            if chat:
                closing["message"] = {
                    "role": "assistant", "content": ""}
            else:
                closing["response"] = ""
            closing.update(_stats_fields(final))
            emit(closing)

    return BridgeHandler
