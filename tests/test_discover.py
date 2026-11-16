"""Tests for fyrn.discover: finding the models Ollama already
downloaded, from its manifests and blobs. Fake stores only."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import write_gguf_file
from fyrn.discover import discover_ollama_models, ollama_store_dirs

MODEL_LAYER = "application/vnd.ollama.image.model"


def make_store(root, models):
    """Build a fake Ollama store at root.

    models is a list of (manifest_relpath, digest_hex, blob_bytes
    or None). A manifest is written for each, and the blob file
    when blob_bytes is given.
    """
    root = Path(root)
    for relpath, digest, blob_bytes in models:
        manifest = root / "manifests" / relpath
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({
            "layers": [
                {"mediaType": MODEL_LAYER,
                 "digest": "sha256:" + digest,
                 "size": len(blob_bytes or b"")},
                {"mediaType": "application/vnd.ollama.image.config",
                 "digest": "sha256:config",
                 "size": 100},
            ]}), encoding="utf-8")
        if blob_bytes is not None:
            blob = root / "blobs" / ("sha256-" + digest)
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(blob_bytes)
    return root


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_library_model_is_named_without_library(self):
        store = make_store(self.base / "store", [
            ("registry.ollama.ai/library/qwen3/2b", "aa" * 32,
             b"fake-blob-bytes")])
        models, warnings = discover_ollama_models([store])
        self.assertEqual(warnings, [])
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["name"], "qwen3:2b")
        self.assertEqual(models[0]["path"],
                         store / "blobs" / ("sha256-" + "aa" * 32))
        self.assertEqual(models[0]["size"], len(b"fake-blob-bytes"))
        self.assertEqual(models[0]["source"], "ollama")

    def test_namespace_is_kept_in_the_name(self):
        store = make_store(self.base / "store", [
            ("registry.ollama.ai/acme/mymodel/latest", "bb" * 32,
             b"x")])
        models, _warnings = discover_ollama_models([store])
        self.assertEqual(models[0]["name"], "acme/mymodel:latest")

    def test_description_comes_from_the_blob_header(self):
        blob_path = self.base / "blob.gguf"
        write_gguf_file(blob_path, {"general.description":
                                    "A small test model."})
        store = make_store(self.base / "store", [
            ("registry.ollama.ai/library/tiny/1b", "cc" * 32,
             blob_path.read_bytes())])
        models, _warnings = discover_ollama_models([store])
        self.assertEqual(models[0]["description"],
                         "A small test model.")

    def test_missing_blob_is_skipped(self):
        store = make_store(self.base / "store", [
            ("registry.ollama.ai/library/ghost/1b", "dd" * 32, None)])
        models, _warnings = discover_ollama_models([store])
        self.assertEqual(models, [])

    def test_manifest_without_a_model_layer_is_skipped(self):
        store = self.base / "store"
        manifest = (store / "manifests" / "registry.ollama.ai"
                    / "library" / "weird" / "1b")
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({
            "layers": [{"mediaType": "text/plain",
                        "digest": "sha256:ee",
                        "size": 10}]}), encoding="utf-8")
        models, _warnings = discover_ollama_models([store])
        self.assertEqual(models, [])

    def test_bad_json_is_skipped(self):
        store = self.base / "store"
        manifest = (store / "manifests" / "registry.ollama.ai"
                    / "library" / "broken" / "1b")
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{not json", encoding="utf-8")
        models, _warnings = discover_ollama_models([store])
        self.assertEqual(models, [])

    def test_missing_store_is_empty_not_an_error(self):
        models, warnings = discover_ollama_models(
            [self.base / "no-such-store"])
        self.assertEqual(models, [])
        self.assertEqual(warnings, [])

    def test_same_model_in_two_stores_is_listed_once(self):
        store_a = make_store(self.base / "a", [
            ("registry.ollama.ai/library/qwen3/2b", "ff" * 32, b"x")])
        store_b = make_store(self.base / "b", [
            ("registry.ollama.ai/library/qwen3/2b", "ee" * 32, b"y")])
        models, _warnings = discover_ollama_models(
            [store_a, store_b])
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["name"], "qwen3:2b")
        # The first store wins.
        self.assertEqual(models[0]["path"],
                         store_a / "blobs" / ("sha256-" + "ff" * 32))

    def test_unreadable_store_warns_instead_of_crashing(self):
        store = self.base / "store"
        (store / "manifests").mkdir(parents=True)
        with mock.patch.object(Path, "rglob",
                               side_effect=OSError("denied")):
            models, warnings = discover_ollama_models([store])
        self.assertEqual(models, [])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0][0], "Ollama models")
        self.assertIn("could not read", warnings[0][1])

    def test_models_sorted_by_name(self):
        store = make_store(self.base / "store", [
            ("registry.ollama.ai/library/zebra/1b", "11" * 32, b"x"),
            ("registry.ollama.ai/library/apple/1b", "22" * 32, b"x")])
        models, _warnings = discover_ollama_models([store])
        self.assertEqual([m["name"] for m in models],
                         ["apple:1b", "zebra:1b"])


class StoreDirsTests(unittest.TestCase):
    def test_override_comes_first(self):
        with mock.patch.dict("os.environ",
                             {"OLLAMA_MODELS": "/tmp/ollama-test"}):
            dirs = ollama_store_dirs()
        self.assertEqual(dirs[0], Path("/tmp/ollama-test"))

    def test_defaults_cover_user_and_service(self):
        # Only the override is removed: clearing the whole
        # environment would also clear HOME, and Path.home()
        # would answer from the password file instead.
        with mock.patch.dict(os.environ):
            os.environ.pop("OLLAMA_MODELS", None)
            dirs = ollama_store_dirs()
        self.assertIn(Path.home() / ".ollama" / "models", dirs)
        self.assertIn(Path("/usr/share/ollama/.ollama/models"),
                      dirs)


if __name__ == "__main__":
    unittest.main()
