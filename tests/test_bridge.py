"""Tests for the bridge (v0.9): Fyrn's Ollama-shaped adapter
for other programs on the same PC.

The service runs for real, on an ephemeral port, against a
FakeRunner-backed controller and a fixed model list, and the
tests speak real HTTP to it. No Qt, no real model, no network
beyond 127.0.0.1.
"""

import http.client
import json
import socket
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
            "version": "0.9",
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
        self.assertEqual(payload, {"version": "0.9"})

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


if __name__ == "__main__":
    unittest.main()
