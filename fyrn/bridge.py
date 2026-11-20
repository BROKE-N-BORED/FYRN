"""The bridge: Fyrn's engine, offered to other programs (v0.9).

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
- Tools are never offered through the bridge: a bridge reply
  is a plain reply, exactly like a chat turn without tools.

Standard library only, no Qt: the HTTP handlers run on their
own threads, and everything GUI-shaped (the model list, the
busy check) arrives as callbacks from the window.
"""

import json
import queue as _queue
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import ModelNotFoundError, ModelTooBigError
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
    every request (the idle auto-unload timer listens to it).
    """

    def __init__(self, controller, list_models_fn, is_busy_fn=None,
                 max_tokens_fn=None, on_activity=None,
                 port=DEFAULT_PORT, version=""):
        self.controller = controller
        self._list_models = list_models_fn
        self._is_busy = is_busy_fn or (lambda: False)
        self._max_tokens_fn = max_tokens_fn
        self._on_activity = on_activity or (lambda: None)
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
            blob = entry["path"].name
            if blob.startswith("sha256-"):
                item["digest"] = "sha256:" + blob[len("sha256-"):]
            models.append(item)
        return {"models": models}

    def version_info(self):
        """GET /api/version: Fyrn's own version (the bridge is
        Fyrn, not Ollama; the shape is Ollama's)."""
        self._on_activity()
        return {"version": self.version}

    def generate_events(self, payload, chat=False):
        """Run one generation and yield its events.

        Each event is a dict: zero or more {"piece": text}
        events, then exactly one final event with the whole
        reply under "text" plus "stats". The HTTP layer turns
        the events into Ollama's streaming lines or its single
        non-streaming object. Raises BridgeError for every
        refusal (busy, unknown model, bad request, fit) and
        RunnerError for an engine failure.
        """
        self._on_activity()
        if not isinstance(payload, dict):
            raise BridgeError(400, "The request was not a JSON "
                                   "object.")
        name = payload.get("model")
        if not name or not isinstance(name, str):
            raise BridgeError(400, "The request names no model.")
        if chat:
            prompt, history, instructions = _chat_parts(payload)
        else:
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt:
                raise BridgeError(
                    400, "The request has no prompt.")
            history = []
            instructions = None
            system = payload.get("system")
            if isinstance(system, str) and system.strip():
                instructions = system
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
        self._ensure_model(name)
        cap = _request_cap(payload, self._max_tokens_fn)
        cancel_event = threading.Event()
        queue = _queue.Queue()
        runner = self.controller.runner

        def run():
            try:
                stats = runner.generate(
                    prompt,
                    on_token=lambda piece: queue.put(
                        ("piece", piece)),
                    cancel_event=cancel_event,
                    history=history, instructions=instructions,
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
        thread = threading.Thread(target=run, daemon=True,
                                  name="fyrn-bridge-generate")
        thread.start()
        pieces = []
        try:
            while True:
                kind, value = queue.get()
                if kind == "piece":
                    pieces.append(value)
                    yield {"piece": value}
                elif kind == "done":
                    yield {"text": "".join(pieces),
                           "stats": value,
                           "elapsed_s": time.monotonic() - started,
                           "model": name}
                    return
                else:  # "error"
                    raise value
        finally:
            # The consumer stopped early (a client that went
            # away): stop the generation instead of letting it
            # run out its cap for nobody.
            cancel_event.set()

    # ------------------------------------------------------------ internals

    def _ensure_model(self, name):
        """Have the named model loaded, loading it if needed.

        The one-model rule: a different loaded model is
        unloaded by the load, exactly as in the window. The RAM
        fit check runs first: a refusal is a clear error, never
        an attempted load."""
        entry = self._find_model(name)
        if entry is None:
            raise BridgeError(
                404, "model '%s' not found" % name)
        controller = self.controller
        if (controller.is_loaded
                and str(controller.current_path)
                == str(entry["path"])):
            return
        try:
            verdict, message = controller.check_before_load(
                entry["path"])
        except ModelNotFoundError as exc:
            raise BridgeError(404, str(exc))
        if verdict == "refuse":
            raise BridgeError(500, message)
        try:
            controller.load(entry["path"], display_name=name)
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


def _chat_parts(payload):
    """Split a /api/chat payload into (prompt, history,
    instructions): the last user message is the prompt, the
    earlier conversation is the history, and system messages
    become the instructions. Images are not carried (BRIDGE.md
    says so): a message's content is read as text."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BridgeError(400, "The request has no messages.")
    history = []
    instructions = []
    prompt = None
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str):
            content = ""
        if role == "system":
            if content.strip():
                instructions.append(content)
        elif role in ("user", "assistant"):
            history.append({"role": role, "content": content})
    for index in range(len(history) - 1, -1, -1):
        if history[index]["role"] == "user":
            prompt = history.pop(index)["content"]
            break
    if prompt is None:
        raise BridgeError(
            400, "The request has no user message.")
    return (prompt, history,
            "\n".join(instructions) if instructions else None)


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
                reply["message"] = {
                    "role": "assistant",
                    "content": final["text"]}
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
            try:
                while pending is not None:
                    if "piece" in pending:
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
