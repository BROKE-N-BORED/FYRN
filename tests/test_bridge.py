"""Tests for the bridge (v0.9, extended v0.10): Fyrn's
Ollama-shaped adapter for other programs on the same PC.

The service runs for real, on an ephemeral port, against a
FakeRunner-backed controller and a fixed model list, and the
tests speak real HTTP to it. No Qt, no real model, no network
beyond 127.0.0.1.
"""

import base64
import http.client
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import make_fake_gguf

from fyrn.bridge import BridgeError, BridgeService
from fyrn.core import ModelController
from fyrn.runner import FakeRunner

GB = 1024 ** 3


class SlowRunner(FakeRunner):
    """A FakeRunner whose generate blocks until released, so a
    test can hold a bridge generation open."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate(self, prompt, **kwargs):
        self.entered.set()
        self.release.wait(30)
        return super().generate(prompt, **kwargs)


class BridgeServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.alpha = self.base / "alpha.gguf"
        self.beta = self.base / "beta.gguf"
        make_fake_gguf(self.alpha, 2048)
        make_fake_gguf(self.beta, 2048)
        self.blob = self.base / ("sha256-" + "ab" * 32)
        make_fake_gguf(self.blob, 1024)
        self.models = [
            {"name": "alpha:latest", "path": self.alpha,
             "size": 2048},
            {"name": "beta:2b", "path": self.beta, "size": 2048},
            {"name": "gamma:0.6b", "path": self.blob,
             "size": 1024},
        ]
        self.runner = FakeRunner()
        self.controller = ModelController(
            self.runner, free_ram_fn=lambda: 16 * GB)
        self.busy = False
        self.activity = 0
        self.service = self._make_service()
        self.assertTrue(self.service.start())
        self.addCleanup(self.service.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_service(self, **overrides):
        kwargs = {
            "controller": self.controller,
            "list_models_fn": lambda: list(self.models),
            "is_busy_fn": lambda: self.busy,
            "on_activity": self._count_activity,
            "port": 0,
            "version": "0.10",
        }
        kwargs.update(overrides)
        return BridgeService(**kwargs)

    def _count_activity(self):
        self.activity += 1

    # -- HTTP plumbing -------------------------------------------------

    def _request(self, method, path, payload=None, raw=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.service.port, timeout=30)
        if raw is not None:
            body = raw
        elif payload is not None:
            body = json.dumps(payload)
        else:
            body = None
        conn.request(method, path, body=body,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def _request_json(self, method, path, payload=None, raw=None):
        status, data = self._request(method, path, payload, raw)
        return status, json.loads(data.decode("utf-8"))

    # -- tags and version ----------------------------------------------

    def test_tags_shape(self):
        status, payload = self._request_json("GET", "/api/tags")
        self.assertEqual(status, 200)
        models = payload["models"]
        self.assertEqual([m["name"] for m in models],
                         ["alpha:latest", "beta:2b", "gamma:0.6b"])
        alpha = models[0]
        self.assertEqual(alpha["model"], "alpha:latest")
        self.assertEqual(alpha["size"], 2048)
        self.assertIn("modified_at", alpha)
        self.assertNotIn("digest", alpha)
        # An Ollama-store blob's name is its digest.
        self.assertEqual(models[2]["digest"],
                         "sha256:" + "ab" * 32)
        self.assertGreater(self.activity, 0)

    def test_version(self):
        status, payload = self._request_json("GET", "/api/version")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"version": "0.10"})

    def test_root_answers(self):
        status, data = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Fyrn bridge", data)

    def test_unknown_path_is_404(self):
        status, payload = self._request_json("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)
        status, payload = self._request_json(
            "POST", "/api/nope", {"model": "alpha:latest"})
        self.assertEqual(status, 404)

    # -- generate --------------------------------------------------------

    def test_generate_non_streaming(self):
        status, payload = self._request_json(
            "POST", "/api/generate",
            {"model": "alpha:latest", "prompt": "Say hello",
             "stream": False})
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], "alpha:latest")
        self.assertTrue(payload["done"])
        self.assertEqual(
            payload["response"],
            "Hello from the fake model. This is a test reply.")
        self.assertGreater(payload["eval_count"], 0)
        self.assertIn("total_duration", payload)
        # The request loaded the model it named.
        self.assertTrue(self.controller.is_loaded)
        self.assertEqual(self.controller.current_name,
                         "alpha:latest")

    def test_generate_streaming_lines(self):
        status, data = self._request(
            "POST", "/api/generate",
            {"model": "alpha:latest", "prompt": "Say hello"})
        self.assertEqual(status, 200)
        lines = [json.loads(line)
                 for line in data.decode("utf-8").splitlines()
                 if line.strip()]
        self.assertGreater(len(lines), 1)
        for line in lines[:-1]:
            self.assertFalse(line["done"])
            self.assertIn("response", line)
        text = "".join(line["response"] for line in lines)
        self.assertEqual(
            text,
            "Hello from the fake model. This is a test reply.")
        last = lines[-1]
        self.assertTrue(last["done"])
        self.assertEqual(last["response"], "")
        self.assertGreater(last["eval_count"], 0)

    def test_generate_loads_the_named_model_and_switches(self):
        status, _payload = self._request_json(
            "POST", "/api/generate",
            {"model": "alpha:latest", "prompt": "hi",
             "stream": False})
        self.assertEqual(status, 200)
        self.assertEqual(str(self.controller.current_path),
                         str(self.alpha))
        status, _payload = self._request_json(
            "POST", "/api/generate",
            {"model": "beta:2b", "prompt": "hi", "stream": False})
        self.assertEqual(status, 200)
        self.assertEqual(str(self.controller.current_path),
                         str(self.beta))
        self.assertEqual(self.controller.current_name, "beta:2b")

    def test_name_without_tag_finds_latest(self):
        status, _payload = self._request_json(
            "POST", "/api/generate",
            {"model": "alpha", "prompt": "hi", "stream": False})
        self.assertEqual(status, 200)
        self.assertEqual(str(self.controller.current_path),
                         str(self.alpha))

    def test_unknown_model_is_404(self):
        status, payload = self._request_json(
            "POST", "/api/generate",
            {"model": "nope:1b", "prompt": "hi", "stream": False})
        self.assertEqual(status, 404)
        self.assertIn("nope:1b", payload["error"])
        self.assertIn("not found", payload["error"])
        self.assertFalse(self.controller.is_loaded)

    def test_missing_model_and_prompt_are_400(self):
        status, payload = self._request_json(
            "POST", "/api/generate",
            {"prompt": "hi", "stream": False})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        status, payload = self._request_json(
            "POST", "/api/generate",
            {"model": "alpha:latest", "stream": False})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_bad_json_is_400(self):
        status, payload = self._request_json(
            "POST", "/api/generate", raw="{not json")
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_busy_window_refuses_with_409(self):
        self.busy = True
        status, payload = self._request_json(
            "POST", "/api/generate",
            {"model": "alpha:latest", "prompt": "hi",
             "stream": False})
        self.assertEqual(status, 409)
        self.assertIn("busy", payload["error"])
        self.assertFalse(self.controller.is_loaded)

    def test_num_predict_caps_the_reply(self):
        status, payload = self._request_json(
            "POST", "/api/generate",
            {"model": "alpha:latest", "prompt": "hi",
             "stream": False, "options": {"num_predict": 3}})
        self.assertEqual(status, 200)
        self.assertEqual(self.runner.last_max_tokens, 3)
        self.assertEqual(payload["response"], "Hello from the")

    def test_default_cap_comes_from_the_setting(self):
        self.service.stop()
        self.service = self._make_service(
            max_tokens_fn=lambda: 5)
        self.assertTrue(self.service.start())
        self.addCleanup(self.service.stop)
        status, _payload = self._request_json(
            "POST", "/api/generate",
            {"model": "alpha:latest", "prompt": "hi",
             "stream": False})
        self.assertEqual(status, 200)
        self.assertEqual(self.runner.last_max_tokens, 5)

    # -- chat ------------------------------------------------------------

    def test_chat_non_streaming_with_history_and_system(self):
        status, payload = self._request_json(
            "POST", "/api/chat",
            {"model": "alpha:latest",
             "messages": [
                 {"role": "system", "content": "Be brief."},
                 {"role": "user", "content": "first question"},
                 {"role": "assistant", "content": "first answer"},
                 {"role": "user", "content": "second question"}],
             "stream": False})
        self.assertEqual(status, 200)
        self.assertTrue(payload["done"])
        self.assertEqual(payload["message"]["role"], "assistant")
        self.assertEqual(
            payload["message"]["content"],
            "Hello from the fake model. This is a test reply.")
        # The earlier turns went as history, the system text as
        # instructions, and the last user message as the prompt.
        self.assertEqual(
            self.runner.last_history,
            [{"role": "user", "content": "first question"},
             {"role": "assistant", "content": "first answer"}])
        self.assertEqual(self.runner.last_instructions,
                         "Be brief.")

    def test_chat_streaming_lines(self):
        status, data = self._request(
            "POST", "/api/chat",
            {"model": "alpha:latest",
             "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        lines = [json.loads(line)
                 for line in data.decode("utf-8").splitlines()
                 if line.strip()]
        text = "".join(line["message"]["content"]
                       for line in lines)
        self.assertEqual(
            text,
            "Hello from the fake model. This is a test reply.")
        self.assertTrue(lines[-1]["done"])
        self.assertEqual(lines[-1]["message"]["content"], "")

    def test_chat_without_user_message_is_400(self):
        status, payload = self._request_json(
            "POST", "/api/chat",
            {"model": "alpha:latest",
             "messages": [{"role": "assistant",
                           "content": "only me"}],
             "stream": False})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    # -- concurrency -----------------------------------------------------

    def test_second_generation_refused_while_one_runs(self):
        slow = SlowRunner()
        controller = ModelController(
            slow, free_ram_fn=lambda: 16 * GB)
        service = self._make_service(controller=controller)
        self.assertTrue(service.start())
        self.addCleanup(service.stop)
        self.addCleanup(slow.release.set)
        events = service.generate_events(
            {"model": "alpha:latest", "prompt": "hi",
             "stream": False})
        outcome = {}

        def consume():
            try:
                outcome["events"] = list(events)
            except Exception as exc:  # recorded, asserted below
                outcome["error"] = exc

        thread = threading.Thread(target=consume)
        thread.start()
        self.assertTrue(slow.entered.wait(10))
        self.assertTrue(service.generating)
        with self.assertRaises(BridgeError) as ctx:
            list(service.generate_events(
                {"model": "alpha:latest", "prompt": "again",
                 "stream": False}))
        self.assertEqual(ctx.exception.status, 409)
        slow.release.set()
        thread.join(10)
        self.assertNotIn("error", outcome)
        self.assertFalse(service.generating)


CALCULATE_TOOL = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": "Do an arithmetic calculation.",
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {"type": "string"}},
            "required": ["expression"]}}}

# A real 1x1 PNG, base64 (the smallest valid picture).
PNG_B64 = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001"
        "08060000001f15c4890000000a49444154789c6360000000"
        "020001e221bc330000000049454e44ae426082")).decode("ascii")


def write_header_gguf(path, size=4096):
    """A minimal valid GGUF v3 file: architecture llama,
    trained context 32768, no tensors, padded to size."""
    out = bytearray()
    out += b"GGUF"
    out += struct.pack("<I", 3)
    out += struct.pack("<Q", 0)      # tensor count
    out += struct.pack("<Q", 3)      # kv count

    def put_string(text):
        blob = text.encode("utf-8")
        out.extend(struct.pack("<Q", len(blob)))
        out.extend(blob)

    put_string("general.architecture")
    out += struct.pack("<I", 8)      # STRING
    put_string("llama")
    put_string("llama.context_length")
    out += struct.pack("<I", 4)      # UINT32
    out += struct.pack("<I", 32768)
    put_string("general.file_type")
    out += struct.pack("<I", 4)      # UINT32
    out += struct.pack("<I", 0)
    out += b"\0" * max(0, size - len(out))
    Path(path).write_bytes(bytes(out))
    return path


class CountingRunner(FakeRunner):
    """A FakeRunner that counts its loads (num_ctx reloads)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.load_count = 0

    def load(self, path, **kwargs):
        self.load_count += 1
        return super().load(path, **kwargs)


class BridgeToolsTests(unittest.TestCase):
    """v0.10 item 1: tools pass through the bridge to the
    model and back, unexecuted — the client runs them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.alpha = self.base / "alpha.gguf"
        make_fake_gguf(self.alpha, 2048)
        self.models = [{"name": "alpha:latest",
                        "path": self.alpha, "size": 2048}]
        self.runner = FakeRunner(
            supports_tools=True,
            tool_script=[("call", "calculate",
                          {"expression": "17 * 24"})])
        self.controller = ModelController(
            self.runner, free_ram_fn=lambda: 16 * GB)
        self.service = BridgeService(
            self.controller, lambda: list(self.models),
            port=0, version="0.10")
        self.assertTrue(self.service.start())
        self.addCleanup(self.service.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _chat(self, payload):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.service.port, timeout=30)
        conn.request("POST", "/api/chat",
                     body=json.dumps(payload),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def test_tool_call_passes_through_unexecuted(self):
        sentinel = self.base / "sentinel.txt"
        write_tool = {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write a file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"}},
                    "required": ["path", "content"]}}}
        self.runner.tool_script = [
            ("call", "write_file",
             {"path": str(sentinel), "content": "boom"})]
        status, data = self._chat({
            "model": "alpha:latest",
            "messages": [{"role": "user",
                          "content": "Write the sentinel file."}],
            "tools": [write_tool],
            "stream": False})
        payload = json.loads(data.decode("utf-8"))
        self.assertEqual(status, 200)
        calls = payload["message"]["tool_calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"],
                         "write_file")
        self.assertEqual(
            calls[0]["function"]["arguments"],
            {"path": str(sentinel), "content": "boom"})
        # The bridge never executes: the file was NOT created.
        self.assertFalse(sentinel.exists())

    def test_tool_result_round_trip(self):
        status, data = self._chat({
            "model": "alpha:latest",
            "messages": [{"role": "user",
                          "content": "What is 17 * 24?"}],
            "tools": [CALCULATE_TOOL],
            "stream": False})
        payload = json.loads(data.decode("utf-8"))
        self.assertEqual(status, 200)
        calls = payload["message"]["tool_calls"]
        self.assertEqual(calls[0]["function"]["name"],
                         "calculate")
        self.assertEqual(calls[0]["function"]["arguments"],
                         {"expression": "17 * 24"})
        # The client runs the tool and calls back with the
        # result as a role "tool" message.
        status, data = self._chat({
            "model": "alpha:latest",
            "messages": [
                {"role": "user", "content": "What is 17 * 24?"},
                {"role": "assistant", "content": "",
                 "tool_calls": calls},
                {"role": "tool", "content": "408"}],
            "tools": [CALCULATE_TOOL],
            "stream": False})
        payload = json.loads(data.decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertNotIn("tool_calls", payload["message"])
        self.assertEqual(
            payload["message"]["content"],
            "Hello from the fake model. This is a test reply.")
        # The conversation reached the runner whole: the tool
        # result as a tool message, the earlier call converted
        # to the shape the engine's templates apply (arguments
        # as a mapping, not a JSON string).
        seen = self.runner.last_turn_messages
        tool_msgs = [m for m in seen if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["content"], "408")
        assistant = [m for m in seen if m["role"] == "assistant"]
        self.assertEqual(len(assistant), 1)
        fed_args = assistant[0]["tool_calls"][0]["function"]
        self.assertEqual(fed_args["name"], "calculate")
        self.assertEqual(fed_args["arguments"],
                         {"expression": "17 * 24"})
        # The client's definitions reached the runner untouched.
        self.assertEqual(self.runner.last_turn_tools,
                         [CALCULATE_TOOL])

    def test_tools_streaming_carries_the_calls(self):
        status, data = self._chat({
            "model": "alpha:latest",
            "messages": [{"role": "user",
                          "content": "What is 17 * 24?"}],
            "tools": [CALCULATE_TOOL]})
        self.assertEqual(status, 200)
        lines = [json.loads(line)
                 for line in data.decode("utf-8").splitlines()
                 if line.strip()]
        with_calls = [line for line in lines
                      if line["message"].get("tool_calls")]
        self.assertEqual(len(with_calls), 1)
        self.assertEqual(
            with_calls[0]["message"]["tool_calls"][0]
            ["function"]["name"], "calculate")
        self.assertFalse(with_calls[0]["done"])
        self.assertTrue(lines[-1]["done"])
        self.assertEqual(lines[-1]["message"]["content"], "")

    def test_tools_on_a_model_without_tool_support(self):
        # Ollama's behaviour: the model answers as text.
        self.service.stop()
        self.runner = FakeRunner(supports_tools=False)
        self.controller = ModelController(
            self.runner, free_ram_fn=lambda: 16 * GB)
        self.service = BridgeService(
            self.controller, lambda: list(self.models),
            port=0, version="0.10")
        self.assertTrue(self.service.start())
        self.addCleanup(self.service.stop)
        status, data = self._chat({
            "model": "alpha:latest",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [CALCULATE_TOOL],
            "stream": False})
        payload = json.loads(data.decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertNotIn("tool_calls", payload["message"])
        self.assertEqual(
            payload["message"]["content"],
            "Hello from the fake model. This is a test reply.")

    def test_malformed_tools_are_400(self):
        status, data = self._chat({
            "model": "alpha:latest",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": "calculate",
            "stream": False})
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(data.decode("utf-8")))


class BridgeImagesTests(unittest.TestCase):
    """v0.10 item 2: images are carried to vision models and
    refused in plain words for the rest — never dropped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.alpha = self.base / "alpha.gguf"
        make_fake_gguf(self.alpha, 2048)
        self.models = [{"name": "alpha:latest",
                        "path": self.alpha, "size": 2048}]
        self._old_home = os.environ.get("FYRN_HOME")
        os.environ["FYRN_HOME"] = str(self.base / "fyrn-home")
        self.addCleanup(self._restore_home)

    def tearDown(self):
        self.tmp.cleanup()

    def _restore_home(self):
        if self._old_home is None:
            os.environ.pop("FYRN_HOME", None)
        else:
            os.environ["FYRN_HOME"] = self._old_home

    def _service(self, runner):
        controller = ModelController(
            runner, free_ram_fn=lambda: 16 * GB)
        service = BridgeService(
            controller, lambda: list(self.models),
            port=0, version="0.10")
        self.assertTrue(service.start())
        self.addCleanup(service.stop)
        return service

    def _post(self, service, path, payload):
        conn = http.client.HTTPConnection(
            "127.0.0.1", service.port, timeout=30)
        conn.request("POST", path, body=json.dumps(payload),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data.decode("utf-8"))

    def test_image_to_a_text_model_is_a_plain_400(self):
        service = self._service(FakeRunner())
        status, payload = self._post(service, "/api/chat", {
            "model": "alpha:latest",
            "messages": [{"role": "user",
                          "content": "What is in this picture?",
                          "images": [PNG_B64]}],
            "stream": False})
        self.assertEqual(status, 400)
        self.assertIn("cannot see pictures", payload["error"])
        # No temp picture is left behind.
        folder = self.base / "fyrn-home" / "bridge-images"
        leftovers = (list(folder.iterdir())
                     if folder.exists() else [])
        self.assertEqual(leftovers, [])

    def test_image_reaches_a_vision_model(self):
        runner = FakeRunner(supports_vision=True)
        service = self._service(runner)
        status, payload = self._post(service, "/api/chat", {
            "model": "alpha:latest",
            "messages": [{"role": "user",
                          "content": "What is in this picture?",
                          "images": [PNG_B64]}],
            "stream": False})
        self.assertEqual(status, 200)
        self.assertEqual(len(runner.last_images), 1)
        # The temp file was cleaned up after the reply.
        self.assertFalse(Path(runner.last_images[0]).exists())

    def test_generate_image_to_a_text_model_is_400(self):
        service = self._service(FakeRunner())
        status, payload = self._post(service, "/api/generate", {
            "model": "alpha:latest", "prompt": "Describe it.",
            "images": [PNG_B64], "stream": False})
        self.assertEqual(status, 400)
        self.assertIn("cannot see pictures", payload["error"])

    def test_unreadable_image_is_400(self):
        service = self._service(FakeRunner(supports_vision=True))
        status, payload = self._post(service, "/api/chat", {
            "model": "alpha:latest",
            "messages": [{"role": "user", "content": "Look.",
                          "images": ["!!! not base64 !!!"]}],
            "stream": False})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)


class BridgeResidencyTests(unittest.TestCase):
    """v0.10 items 3 and 4: /api/ps, keep_alive 0, num_ctx,
    and /api/show."""

    EXPIRES = "2026-11-20T04:00:00.000000Z"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.alpha = self.base / "alpha.gguf"
        write_header_gguf(self.alpha)
        self.blob = self.base / ("sha256-" + "cd" * 32)
        make_fake_gguf(self.blob, 1024)
        self.models = [
            {"name": "alpha:latest", "path": self.alpha,
             "size": 4096},
            {"name": "gamma:0.6b", "path": self.blob,
             "size": 1024},
        ]
        self.runner = CountingRunner()
        self.controller = ModelController(
            self.runner, free_ram_fn=lambda: 16 * GB)
        self.service = BridgeService(
            self.controller, lambda: list(self.models),
            expires_at_fn=lambda: self.EXPIRES,
            port=0, version="0.10")
        self.assertTrue(self.service.start())
        self.addCleanup(self.service.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _request_json(self, method, path, payload=None):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.service.port, timeout=30)
        body = (json.dumps(payload)
                if payload is not None else None)
        conn.request(method, path, body=body,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data.decode("utf-8"))

    def _load_alpha(self):
        status, _payload = self._request_json(
            "POST", "/api/generate",
            {"model": "alpha:latest", "prompt": "hi",
             "stream": False})
        self.assertEqual(status, 200)
        self.assertTrue(self.controller.is_loaded)

    # -- /api/ps ---------------------------------------------------

    def test_ps_empty_then_loaded(self):
        status, payload = self._request_json("GET", "/api/ps")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"models": []})
        self._load_alpha()
        status, payload = self._request_json("GET", "/api/ps")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["models"]), 1)
        entry = payload["models"][0]
        self.assertEqual(entry["name"], "alpha:latest")
        self.assertEqual(entry["model"], "alpha:latest")
        self.assertEqual(entry["size"], 4096)
        self.assertNotIn("digest", entry)
        self.assertEqual(entry["expires_at"], self.EXPIRES)

    def test_ps_carries_the_blob_digest(self):
        status, _payload = self._request_json(
            "POST", "/api/generate",
            {"model": "gamma:0.6b", "prompt": "hi",
             "stream": False})
        self.assertEqual(status, 200)
        _status, payload = self._request_json("GET", "/api/ps")
        self.assertEqual(payload["models"][0]["digest"],
                         "sha256:" + "cd" * 32)

    # -- keep_alive 0 ------------------------------------------------

    def test_keep_alive_zero_empty_generate_unloads(self):
        self._load_alpha()
        status, payload = self._request_json(
            "POST", "/api/generate",
            {"model": "alpha:latest", "keep_alive": 0})
        self.assertEqual(status, 200)
        self.assertTrue(payload["done"])
        self.assertEqual(payload["response"], "")
        self.assertFalse(self.controller.is_loaded)

    def test_keep_alive_zero_empty_chat_unloads(self):
        self._load_alpha()
        status, payload = self._request_json(
            "POST", "/api/chat",
            {"model": "alpha:latest", "messages": [],
             "keep_alive": 0})
        self.assertEqual(status, 200)
        self.assertTrue(payload["done"])
        self.assertFalse(self.controller.is_loaded)

    def test_keep_alive_zero_after_a_reply_unloads(self):
        status, payload = self._request_json(
            "POST", "/api/generate",
            {"model": "alpha:latest", "prompt": "hi",
             "keep_alive": 0, "stream": False})
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["response"],
            "Hello from the fake model. This is a test reply.")
        self.assertFalse(self.controller.is_loaded)

    def test_keep_alive_duration_keeps_the_model(self):
        status, _payload = self._request_json(
            "POST", "/api/generate",
            {"model": "alpha:latest", "prompt": "hi",
             "keep_alive": "5m", "stream": False})
        self.assertEqual(status, 200)
        self.assertTrue(self.controller.is_loaded)

    def test_keep_alive_zero_unknown_model_is_404(self):
        status, payload = self._request_json(
            "POST", "/api/generate",
            {"model": "nope:1b", "keep_alive": 0})
        self.assertEqual(status, 404)
        self.assertIn("not found", payload["error"])

    # -- num_ctx ------------------------------------------------------

    def test_num_ctx_loads_reloads_and_reuses(self):
        status, _payload = self._request_json(
            "POST", "/api/generate",
            {"model": "alpha:latest", "prompt": "hi",
             "stream": False, "options": {"num_ctx": 4096}})
        self.assertEqual(status, 200)
        self.assertEqual(self.runner.context_tokens, 4096)
        self.assertEqual(self.runner.load_count, 1)
        # The same size again reuses the loaded model.
        status, _payload = self._request_json(
            "POST", "/api/generate",
            {"model": "alpha:latest", "prompt": "hi",
             "stream": False, "options": {"num_ctx": 4096}})
        self.assertEqual(status, 200)
        self.assertEqual(self.runner.load_count, 1)
        # A different size reloads at it.
        status, _payload = self._request_json(
            "POST", "/api/generate",
            {"model": "alpha:latest", "prompt": "hi",
             "stream": False, "options": {"num_ctx": 1024}})
        self.assertEqual(status, 200)
        self.assertEqual(self.runner.load_count, 2)
        self.assertEqual(self.runner.context_tokens, 1024)

    def test_num_ctx_clamps_to_the_trained_context(self):
        status, _payload = self._request_json(
            "POST", "/api/generate",
            {"model": "alpha:latest", "prompt": "hi",
             "stream": False, "options": {"num_ctx": 999999}})
        self.assertEqual(status, 200)
        self.assertEqual(self.runner.context_tokens, 32768)

    # -- /api/show -----------------------------------------------------

    def test_show_reports_the_header_facts(self):
        status, payload = self._request_json(
            "POST", "/api/show", {"model": "alpha:latest"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["name"], "alpha:latest")
        self.assertEqual(payload["size"], 4096)
        self.assertEqual(payload["details"]["format"], "gguf")
        self.assertEqual(payload["details"]["family"], "llama")
        self.assertEqual(
            payload["model_info"]["llama.context_length"],
            32768)
        self.assertEqual(
            payload["model_info"]["general.architecture"],
            "llama")
        self.assertNotIn("loaded_context_tokens", payload)

    def test_show_names_the_loaded_context(self):
        self._load_alpha()
        status, payload = self._request_json(
            "POST", "/api/show", {"model": "alpha:latest"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["loaded_context_tokens"], 2048)

    def test_show_unknown_model_and_missing_name(self):
        status, payload = self._request_json(
            "POST", "/api/show", {"model": "nope:1b"})
        self.assertEqual(status, 404)
        status, payload = self._request_json(
            "POST", "/api/show", {})
        self.assertEqual(status, 400)


class BridgeStartTests(unittest.TestCase):
    def test_port_taken_is_a_plain_error_not_a_crash(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "m.gguf"
                make_fake_gguf(path, 1024)
                controller = ModelController(
                    FakeRunner(), free_ram_fn=lambda: 16 * GB)
                service = BridgeService(
                    controller,
                    lambda: [{"name": "m:latest", "path": path,
                              "size": 1024}],
                    port=port)
                self.assertFalse(service.start())
                self.assertFalse(service.running)
                self.assertIn("already in use",
                              service.start_error)
                self.assertIn(str(port), service.start_error)
        finally:
            blocker.close()

    def test_stop_frees_the_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.gguf"
            make_fake_gguf(path, 1024)
            controller = ModelController(
                FakeRunner(), free_ram_fn=lambda: 16 * GB)
            models = lambda: [{"name": "m:latest", "path": path,
                               "size": 1024}]  # noqa: E731
            first = BridgeService(controller, models, port=0)
            self.assertTrue(first.start())
            port = first.port
            first.stop()
            self.assertFalse(first.running)
            second = BridgeService(controller, models,
                                   port=port)
            self.assertTrue(second.start())
            second.stop()


class BridgeShareDeckTests(unittest.TestCase):
    """v0.11: home-network sharing picks the bind address, and
    the same server serves the phone deck at /deck."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.alpha = self.base / "alpha.gguf"
        make_fake_gguf(self.alpha, 2048)
        self.models = [
            {"name": "alpha:latest", "path": self.alpha,
             "size": 2048},
        ]
        self.controller = ModelController(
            FakeRunner(), free_ram_fn=lambda: 16 * GB)
        self.services = []
        self.addCleanup(self._stop_all)
        self.addCleanup(self.tmp.cleanup)

    def _stop_all(self):
        for service in self.services:
            service.stop()

    def _start(self, share):
        service = BridgeService(
            self.controller, lambda: list(self.models),
            share_fn=lambda: share, port=0, version="0.11")
        self.assertTrue(service.start())
        self.services.append(service)
        return service

    def _get(self, service, path):
        conn = http.client.HTTPConnection(
            "127.0.0.1", service.port, timeout=30)
        conn.request("GET", path)
        response = conn.getresponse()
        data = response.read()
        headers = dict(response.getheaders())
        conn.close()
        return response.status, headers, data

    def test_share_off_binds_this_pc_only(self):
        service = self._start(share=False)
        self.assertFalse(service.shared)
        self.assertEqual(service.host, "127.0.0.1")
        self.assertEqual(
            service._server.server_address[0], "127.0.0.1")

    def test_share_on_binds_the_network(self):
        service = self._start(share=True)
        self.assertTrue(service.shared)
        self.assertEqual(service.host, "0.0.0.0")
        self.assertEqual(
            service._server.server_address[0], "0.0.0.0")

    def test_no_share_setting_means_this_pc_only(self):
        service = BridgeService(
            self.controller, lambda: list(self.models),
            port=0, version="0.11")
        self.assertTrue(service.start())
        self.services.append(service)
        self.assertFalse(service.shared)
        self.assertEqual(service.host, "127.0.0.1")

    def test_deck_route_serves_the_page(self):
        service = self._start(share=False)
        status, headers, data = self._get(service, "/deck")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"<title>Fyrn deck</title>", data)
        self.assertIn(b'id="model"', data)
        self.assertIn(b'id="chat"', data)
        self.assertIn(b"/api/chat", data)
        self.assertIn(b"/api/tags", data)

    def test_deck_route_serves_when_shared_too(self):
        service = self._start(share=True)
        status, _headers, data = self._get(service, "/deck")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Fyrn deck</title>", data)

    def test_api_still_answers_alongside_the_deck(self):
        service = self._start(share=True)
        status, _headers, data = self._get(service, "/api/tags")
        self.assertEqual(status, 200)
        self.assertIn(b"alpha:latest", data)


if __name__ == "__main__":
    unittest.main()
