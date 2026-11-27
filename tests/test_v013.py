"""Tests for the v0.13 repairs: the model's own Ollama
parameters honored at generation, the token-true history
budget (a chat can no longer wedge on an over-long reply),
plain-words chat failures, honest tool-turn stats that clear
when they should, the vision projector found in Ollama's
store, and the GGUF header reader that now walks the whole
KV section.

Runner tests use a stub engine (the _StubLlm pattern from
test_runner.py, widened to accept the real tokenize
signature); GUI tests follow test_gui.py's GuiTestCase.
"""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from fyrn.core import _read_gguf_kv, read_gguf_metadata
from fyrn.discover import discover_ollama_models, parse_params_blob
from fyrn.runner import (CHAT_TOO_LONG_MESSAGE, LlamaRunner,
                         RunnerError)

GB = 1024 ** 3

MODEL_LAYER = "application/vnd.ollama.image.model"
PARAMS_LAYER = "application/vnd.ollama.image.params"
SYSTEM_LAYER = "application/vnd.ollama.image.system"
PROJECTOR_LAYER = "application/vnd.ollama.image.projector"

BONSAI_BLOB = Path(
    "/home/hatch/workspace/ollama/models/blobs/"
    "sha256-a8cbeebea75d63abd2fa3ef98207a792ef879f5db82aebdae"
    "8a2a23884242642")


# ------------------------------------------------------------ header

class GgufHeaderReaderTests(unittest.TestCase):
    """The real bonsai GGUF declares 49 KV entries: 41 scalars
    and 8 arrays. The old reader returned 30 scalars: its
    256 KB window and array cap killed the parse inside the
    tokenizer tables, before the chat template (key 44)."""

    def setUp(self):
        if not BONSAI_BLOB.is_file():
            self.skipTest("the bonsai GGUF is not in this store")

    def test_all_scalar_entries_come_back(self):
        kv = _read_gguf_kv(BONSAI_BLOB)
        self.assertEqual(len(kv), 41)

    def test_the_chat_template_is_found(self):
        metadata = read_gguf_metadata(BONSAI_BLOB)
        template = metadata.get("tokenizer.chat_template", "")
        self.assertIn("<|im_start|>", template)
        self.assertGreater(len(template), 1000)

    def test_late_sampling_keys_are_found(self):
        kv = _read_gguf_kv(BONSAI_BLOB)
        self.assertEqual(kv.get("general.sampling.top_k"), 20)
        self.assertIn("tokenizer.ggml.eos_token_id", kv)


# ------------------------------------------------------------ discovery

def make_store(root, layers, blob_bytes=b"fake-model-bytes"):
    """A fake Ollama store with one model whose manifest
    carries the given extra layers: {mediaType: bytes}. The
    layer blobs are written under fixed digests."""
    root = Path(root)
    manifest_layers = [{
        "mediaType": MODEL_LAYER,
        "digest": "sha256:" + "aa" * 32,
        "size": len(blob_bytes)}]
    blobs = root / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    (blobs / ("sha256-" + "aa" * 32)).write_bytes(blob_bytes)
    digests = {PARAMS_LAYER: "bb" * 32, SYSTEM_LAYER: "cc" * 32,
               PROJECTOR_LAYER: "dd" * 32}
    for media_type, data in layers.items():
        digest = digests[media_type]
        manifest_layers.append({
            "mediaType": media_type,
            "digest": "sha256:" + digest,
            "size": len(data)})
        (blobs / ("sha256-" + digest)).write_bytes(data)
    manifest = (root / "manifests" / "registry.ollama.ai"
                / "eriktonon" / "bonsai-2-uncensored" / "2b")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"layers": manifest_layers}),
                        encoding="utf-8")
    return root


class ParamsDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_json_params_blob_is_carried(self):
        store = make_store(self.base / "store", {
            PARAMS_LAYER: json.dumps({
                "num_ctx": 32768, "presence_penalty": 1,
                "temperature": 0.7, "top_k": 20, "top_p": 0.9,
                "stop": ["<|im_end|>", "<|endoftext|>"]}).encode()})
        models, _warnings = discover_ollama_models([store])
        self.assertEqual(len(models), 1)
        params = models[0].get("params") or {}
        self.assertEqual(params.get("presence_penalty"), 1.0)
        self.assertEqual(params.get("temperature"), 0.7)
        self.assertEqual(params.get("top_k"), 20)
        self.assertEqual(params.get("stop"),
                         ["<|im_end|>", "<|endoftext|>"])
        # The context size stays Fyrn's choice, never the
        # Modelfile's.
        self.assertNotIn("num_ctx", params)

    def test_modelfile_params_blob_is_carried(self):
        store = make_store(self.base / "store", {
            PARAMS_LAYER: (
                "# the model's own settings\n"
                "PARAMETER presence_penalty 1.0\n"
                "PARAMETER temperature 0.7\n"
                "PARAMETER stop \"<|im_end|>\"\n"
                "PARAMETER num_ctx 32768\n").encode()})
        models, _warnings = discover_ollama_models([store])
        params = models[0].get("params") or {}
        self.assertEqual(params.get("presence_penalty"), 1.0)
        self.assertEqual(params.get("temperature"), 0.7)
        self.assertEqual(params.get("stop"), ["<|im_end|>"])
        self.assertNotIn("num_ctx", params)

    def test_system_layer_is_carried(self):
        store = make_store(self.base / "store", {
            SYSTEM_LAYER: b"Always answer in the user's "
                          b"language.\n"})
        models, _warnings = discover_ollama_models([store])
        self.assertEqual(models[0].get("system"),
                         "Always answer in the user's language.")

    def test_projector_layer_is_carried(self):
        store = make_store(self.base / "store", {
            PROJECTOR_LAYER: b"fake-projector-bytes"})
        models, _warnings = discover_ollama_models([store])
        projector = models[0].get("projector")
        self.assertIsNotNone(projector)
        self.assertEqual(Path(projector).read_bytes(),
                         b"fake-projector-bytes")

    def test_no_extra_layers_means_no_extra_keys(self):
        store = make_store(self.base / "store", {})
        models, _warnings = discover_ollama_models([store])
        self.assertNotIn("params", models[0])
        self.assertNotIn("system", models[0])
        self.assertNotIn("projector", models[0])

    def test_parse_params_blob_units(self):
        self.assertEqual(parse_params_blob(""), {})
        self.assertEqual(parse_params_blob("{not json"), {})
        self.assertEqual(
            parse_params_blob('{"repeat_penalty": 1.1}'),
            {"repeat_penalty": 1.1})
        # A value of the wrong kind is dropped, not passed on.
        self.assertEqual(
            parse_params_blob('{"temperature": "hot"}'), {})


# ------------------------------------------------------------ stub engine

class _EngineStub:
    """A stand-in for the engine object: records every
    create_chat_completion call and answers from a script.
    tokenize counts whitespace-separated words."""

    def __init__(self, script=(), stream_text="A plain answer."):
        self.script = list(script)
        self.stream_text = stream_text
        self.calls = []
        self.metadata = {}

    def tokenize(self, blob, add_bos=True, special=False):
        if isinstance(blob, str):
            blob = blob.encode("utf-8")
        return blob.split()

    def create_chat_completion(self, messages=None, **kwargs):
        self.calls.append((list(messages or []), dict(kwargs)))
        if kwargs.get("stream"):
            return [{"choices": [
                {"delta": {"content": self.stream_text}}]}]
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _tool_response(name, arguments, completion_tokens):
    return {"choices": [{"message": {
        "role": "assistant", "content": "",
        "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": name, "arguments": arguments}}],
    }}], "usage": {"completion_tokens": completion_tokens}}


def _text_response(text, completion_tokens):
    return {"choices": [{"message": {
        "role": "assistant", "content": text}}],
        "usage": {"completion_tokens": completion_tokens}}


def _loaded_runner(stub, context=2048, params=None, system=None):
    runner = LlamaRunner(free_ram_fn=lambda: 8 * GB)
    runner._llm = stub
    runner.model_path = "stub.gguf"
    runner.context_tokens = context
    runner._tools = True
    if params:
        runner._model_params = dict(params)
    if system:
        runner._model_system = system
    return runner


# ------------------------------------------------------------ params at the engine

class _FakeEngine:
    """A fake llama_cpp Llama class: load() builds one, and the
    test reads the instance back to see what generation sent."""

    instances = []

    def __init__(self, model_path, n_threads, n_ctx, verbose,
                 chat_handler=None):
        self.metadata = {}
        self.calls = []
        self.chat_handler = chat_handler
        _FakeEngine.instances.append(self)

    def tokenize(self, blob, add_bos=True, special=False):
        if isinstance(blob, str):
            blob = blob.encode("utf-8")
        return blob.split()

    def create_chat_completion(self, messages=None, **kwargs):
        self.calls.append((list(messages or []), dict(kwargs)))
        if kwargs.get("stream"):
            return [{"choices": [
                {"delta": {"content": "A plain answer."}}]}]
        return _text_response("A tool-free answer.", 3)


class ParamsReachEngineTests(unittest.TestCase):
    def setUp(self):
        _FakeEngine.instances = []
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "model.gguf"
        self.path.write_bytes(b"x" * 2048)

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self, **load_kwargs):
        module = types.ModuleType("llama_cpp")
        module.Llama = _FakeEngine
        runner = LlamaRunner(free_ram_fn=lambda: 8 * GB)
        with mock.patch.dict(sys.modules, {"llama_cpp": module}):
            runner.load(str(self.path), **load_kwargs)
        return runner, _FakeEngine.instances[-1]

    def test_plain_path_passes_the_models_parameters(self):
        runner, engine = self._load(
            model_params={"presence_penalty": 1.0,
                          "temperature": 0.7,
                          "stop": ["<|im_end|>"]},
            model_system="The model's own system prompt.")
        runner.generate("hello there")
        messages, kwargs = engine.calls[0]
        self.assertEqual(kwargs.get("presence_penalty"), 1.0)
        self.assertEqual(kwargs.get("temperature"), 0.7)
        self.assertEqual(kwargs.get("stop"), ["<|im_end|>"])
        # The model's system prompt leads when the chat has no
        # instructions of its own.
        self.assertEqual(messages[0],
                         {"role": "system",
                          "content": "The model's own system "
                                     "prompt."})

    def test_tools_path_passes_the_models_parameters(self):
        runner, engine = self._load(
            model_params={"presence_penalty": 1.0})
        runner.generate_tools(
            "hello", execute_tool=lambda n, a: ("", ""))
        _messages, kwargs = engine.calls[0]
        self.assertEqual(kwargs.get("presence_penalty"), 1.0)
        self.assertIn("tools", kwargs)

    def test_chat_instructions_win_over_the_model_system(self):
        runner, engine = self._load(
            model_system="The model's own system prompt.")
        runner.generate("hello", instructions="The chat's own.")
        messages, _kwargs = engine.calls[0]
        self.assertEqual(messages[0]["content"], "The chat's own.")

    def test_no_params_means_no_sampling_kwargs(self):
        runner, engine = self._load()
        runner.generate("hello")
        _messages, kwargs = engine.calls[0]
        for key in ("presence_penalty", "temperature", "stop"):
            self.assertNotIn(key, kwargs)


# ------------------------------------------------------------ the token budget

class TokenBudgetTests(unittest.TestCase):
    """The wedge from the diagnosis, replayed: a history whose
    newest exchange is one giant reply. The follow-up must end
    in the plain-words message, never the raw engine error;
    and a history with older exchanges to spare must shed them
    and answer."""

    def test_wedged_chat_ends_in_plain_words(self):
        stub = _EngineStub()
        runner = _loaded_runner(stub, context=100)
        history = [
            {"role": "user", "content": "a rooted phone question"},
            {"role": "assistant",
             "content": " ".join(["salad"] * 300)},
        ]
        with self.assertRaises(RunnerError) as caught:
            runner.generate("and another thing", history=history)
        self.assertEqual(str(caught.exception),
                         CHAT_TOO_LONG_MESSAGE)
        self.assertNotIn("exceed context window",
                         str(caught.exception))
        # The engine was never called: the turn did not run.
        self.assertEqual(stub.calls, [])
        # The caller's history is untouched (trimming is
        # per-turn; the saved chat keeps everything).
        self.assertEqual(len(history), 2)

    def test_wedged_tools_chat_ends_in_plain_words(self):
        stub = _EngineStub()
        runner = _loaded_runner(stub, context=100)
        history = [
            {"role": "user", "content": "a rooted phone question"},
            {"role": "assistant",
             "content": " ".join(["salad"] * 300)},
        ]
        with self.assertRaises(RunnerError) as caught:
            runner.generate_tools(
                "and another thing", history=history,
                execute_tool=lambda n, a: ("", ""))
        self.assertEqual(str(caught.exception),
                         CHAT_TOO_LONG_MESSAGE)
        self.assertEqual(stub.calls, [])

    def test_oldest_exchanges_drop_and_the_turn_runs(self):
        stub = _EngineStub()
        runner = _loaded_runner(stub, context=200)
        history = [
            {"role": "user", "content": " ".join(["old"] * 30)},
            {"role": "assistant",
             "content": " ".join(["older"] * 30)},
            {"role": "user", "content": "hi there"},
            {"role": "assistant", "content": "ok sure"},
        ]
        stats = runner.generate("next", history=history)
        self.assertGreater(stats["tokens"], 0)
        messages, kwargs = stub.calls[0]
        contents = [str(m.get("content", "")) for m in messages]
        self.assertFalse(any("old" in text for text in contents))
        self.assertIn("hi there", contents)
        self.assertIn("next", contents)
        # The cap shrank to what fits instead of the full 1024.
        self.assertLess(kwargs["max_tokens"], 1024)
        self.assertGreaterEqual(kwargs["max_tokens"], 1)
        # The caller's history keeps all four messages.
        self.assertEqual(len(history), 4)

    def test_engine_overflow_text_is_mapped_to_plain_words(self):
        stub = _EngineStub(script=[ValueError(
            "Requested tokens (2111) exceed context window "
            "of 2048")])
        runner = _loaded_runner(stub, context=4096)
        with self.assertRaises(RunnerError) as caught:
            runner.generate_tools(
                "hi", execute_tool=lambda n, a: ("", ""))
        self.assertEqual(str(caught.exception),
                         CHAT_TOO_LONG_MESSAGE)


# ------------------------------------------------------------ tool-turn stats

class ToolStatsTests(unittest.TestCase):
    def test_stats_count_every_round(self):
        stub = _EngineStub(script=[
            _tool_response("calculate", '{"expression": "2+2"}',
                           11),
            _text_response("The answer is 4.", 13),
        ])
        runner = _loaded_runner(stub)
        stats = runner.generate_tools(
            "what is 2+2",
            execute_tool=lambda n, a: ("2+2 = 4", "2+2 = 4"))
        # 11 tokens from the tool round (the engine's own
        # count) + the final answer's 4 words. Before the fix
        # only the final round was counted: 4.
        self.assertEqual(stats["tokens"], 15)
        self.assertGreater(stats["tokens_per_sec"], 0)
        self.assertGreaterEqual(stats["ttft_s"], 0)
        self.assertLessEqual(stats["ttft_s"], stats["elapsed_s"])


# ------------------------------------------------------------ store projector at load

class _FakeMtmdHandler:
    instances = []

    def __init__(self, clip_model_path, verbose=False,
                 use_gpu=False):
        self.clip_model_path = clip_model_path
        _FakeMtmdHandler.instances.append(self)


class StoreProjectorLoadTests(unittest.TestCase):
    def test_load_uses_the_named_projector(self):
        _FakeEngine.instances = []
        _FakeMtmdHandler.instances = []
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "sha256-modelblob"
            model.write_bytes(b"x" * 2048)
            projector = Path(tmp) / "sha256-projectorblob"
            projector.write_bytes(b"y" * 1024)
            module = types.ModuleType("llama_cpp")
            module.Llama = _FakeEngine
            chat_format = types.ModuleType(
                "llama_cpp.llama_chat_format")
            chat_format.MTMDChatHandler = _FakeMtmdHandler
            runner = LlamaRunner(free_ram_fn=lambda: 8 * GB)
            with mock.patch.dict(sys.modules, {
                    "llama_cpp": module,
                    "llama_cpp.llama_chat_format": chat_format}):
                result = runner.load(str(model),
                                     projector=projector)
            self.assertTrue(runner.supports_vision)
            self.assertEqual(runner.projector_path,
                             str(projector))
            self.assertEqual(len(_FakeMtmdHandler.instances), 1)
            self.assertEqual(
                _FakeMtmdHandler.instances[0].clip_model_path,
                str(projector))
            self.assertNotIn("vision_warning", result)


# ------------------------------------------------------------ GUI stats clearing

from PySide6.QtWidgets import QApplication  # noqa: E402

import fyrn.gui as gui  # noqa: E402
from test_gui import GuiTestCase  # noqa: E402

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance()
    if _app is None:
        _app = QApplication([])
    _app.setStyleSheet(gui.DARK_STYLESHEET)


class StatsClearingTests(GuiTestCase):
    """One model's numbers must never show under another
    (v0.13): _last_stats clears on load finish, on unload, and
    on chat failure; and a failure's raw engine overflow text
    becomes the plain-words sentence in the transcript."""

    STATS = {"tokens": 1206, "tokens_per_sec": 1.2,
             "ttft_s": 1022.0, "elapsed_s": 1022.0}

    def test_stats_cleared_on_load_finish(self):
        window = self.make_window()
        window._last_stats = dict(self.STATS)
        window._on_load_finished({"load_time_s": 1.0,
                                  "warning": ""})
        self.assertIsNone(window._last_stats)
        # The load-finished path refreshes the model list: let
        # its scan finish before teardown closes the window.
        self.wait_scan(window)

    def test_stats_cleared_on_unload(self):
        from helpers import make_fake_gguf
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.load_first_model(window)
        window._last_stats = dict(self.STATS)
        window._unload_now()
        self.assertIsNone(window._last_stats)
        self.wait_scan(window)

    def test_stats_cleared_and_plain_words_on_chat_failure(self):
        window = self.make_window()
        window._last_stats = dict(self.STATS)
        window._on_chat_failed(
            "The model stopped with an error (Requested tokens "
            "(2111) exceed context window of 2048).")
        self.assertIsNone(window._last_stats)
        text = window.transcript.toPlainText()
        self.assertIn(CHAT_TOO_LONG_MESSAGE, text)
        self.assertNotIn("exceed context window", text)


# --------------------------------------------- browsing off by default

from PySide6.QtTest import QTest  # noqa: E402

from helpers import make_fake_gguf  # noqa: E402

from fyrn.chats import ChatStore  # noqa: E402
from fyrn.gui import set_forge_browser_path  # noqa: E402
from test_tools import make_stub_forge_browser  # noqa: E402
from fyrn.runner import FakeRunner  # noqa: E402


class _PathRecordingRunner(FakeRunner):
    """A FakeRunner that remembers which generation path each
    turn took: "plain" (generate) or "tools" (generate_tools)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.paths = []

    def generate(self, prompt, **kwargs):
        self.paths.append("plain")
        return super().generate(prompt, **kwargs)

    def generate_tools(self, prompt, **kwargs):
        self.paths.append("tools")
        return super().generate_tools(prompt, **kwargs)


class BrowsingOffByDefaultTests(GuiTestCase):
    """Web browsing is OFF for every chat until the user turns
    it on in that chat (Joshua's order, 2026-11-27: "turned
    off as default no matter what"). v0.13 also removed
    Fyrn's built-in tools entirely; the one outside
    connection left is web browsing through Forge Browser,
    and it follows the same rule: nothing enables it except
    the user's flip in that chat. A new chat starts off; a
    saved chat loads off whatever its stored record says
    (saved chat files are never rewritten by loading); a new
    chat after a model switch starts off too. The offered /
    not-offered pin is the path the turn took: "plain"
    (generate) or "tools" (generate_tools)."""

    def _window(self, runner=None):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window(
            runner=runner or _PathRecordingRunner(
                supports_tools=True))
        self.load_first_model(window)
        return window

    def _send(self, window, text="Hello"):
        window.chat_input.setText(text)
        window.send_message()
        self.wait_chat(window)

    def test_new_chat_starts_with_browsing_off(self):
        window = self._window()
        self.assertFalse(window._browsing_on)
        self.assertFalse(window.browsing_check.isChecked())
        self.assertIn("Web browsing is off for this chat",
                      window.browsing_status_label.text())
        self._send(window)
        # The turn took the plain path: the engine was never
        # offered the web calls.
        self.assertEqual(window.runner.paths, ["plain"])

    def test_saved_chat_with_a_stored_flag_loads_off(self):
        store = ChatStore(home=self.home)
        record = store.new_record()
        record["title"] = "Flagged chat"
        record["messages"] = [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"}]
        record["tools"] = True
        store.save(record)
        window = self._window()
        window._open_chat(record["id"])
        QTest.qWait(50)
        self.assertFalse(window._browsing_on)
        self.assertFalse(window.browsing_check.isChecked())
        self._send(window, "Follow up")
        self.assertEqual(window.runner.paths, ["plain"])

    def test_turning_browsing_on_offers_the_web_calls(self):
        app_dir = make_stub_forge_browser(self.base / "installed")
        set_forge_browser_path(str(app_dir))
        runner = _PathRecordingRunner(
            supports_tools=True,
            tool_script=[("call", "web_search",
                          {"query": "stub query"}),
                         ("text", "The answer is Stub Result.")])
        window = self._window(runner=runner)
        window.browsing_check.setChecked(True)
        self.assertTrue(window._browsing_on)
        self.assertIn("Web browsing is on for this chat",
                      window.browsing_status_label.text())
        self._send(window, "Search for the stub, please")
        self.assertEqual(runner.paths, ["tools"])
        self.assertIn("Stub Result",
                      runner.last_tool_messages[0]["content"])
        self.assertIn("Web search - stub query",
                      window.transcript.toPlainText())

    def test_turning_browsing_back_off_stops_offering(self):
        runner = _PathRecordingRunner(supports_tools=True)
        window = self._window(runner=runner)
        window.browsing_check.setChecked(True)
        window.browsing_check.setChecked(False)
        self.assertFalse(window._browsing_on)
        self._send(window)
        self.assertEqual(runner.paths, ["plain"])

    def test_leaving_a_chat_resets_browsing_to_off(self):
        window = self._window()
        self._send(window)  # a real chat to leave
        window.browsing_check.setChecked(True)
        self.assertTrue(window._browsing_on)
        window.new_chat()
        QTest.qWait(50)
        self.assertFalse(window._browsing_on)
        self.assertFalse(window.browsing_check.isChecked())


if __name__ == "__main__":
    unittest.main()
