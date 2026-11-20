"""Tests for fyrn.core: RAM checks, fit badges, model scan,
import, and the one-model-at-a-time controller."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import fyrn.core
from helpers import make_fake_gguf, write_gguf_file
from fyrn.core import (HEADROOM_BYTES, WORKING_BYTES, ImportCancelled,
                             ModelController, ModelNotFoundError,
                             ModelTooBigError, default_models_folder,
                             ensure_home, ensure_models_folder,
                             find_projector, fit_status, format_facts,
                             format_size, get_home, import_gguf,
                             kv_cache_bytes_per_token, list_part_files,
                             model_headroom_bytes, move_to_trash,
                             projector_size_bytes, ram_check,
                             read_gguf_facts, read_gguf_metadata,
                             read_gguf_number_array, read_gguf_numbers,
                             read_gguf_tensor_params, read_ram_bytes,
                             remove_gguf, scan_models, trash_dir)
from fyrn.runner import FakeRunner

GB = 1024 ** 3


class FormatSizeTests(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(512), "512 B")

    def test_kilobytes(self):
        self.assertEqual(format_size(1024), "1 KB")

    def test_megabytes(self):
        # Under 100 MB the size keeps one decimal; 100 and up is whole.
        self.assertEqual(format_size(1024 * 1024), "1.0 MB")
        self.assertEqual(format_size(850 * 1024 * 1024), "850 MB")

    def test_gigabytes(self):
        self.assertEqual(format_size(int(2.5 * GB)), "2.5 GB")
        self.assertEqual(format_size(3 * GB), "3.0 GB")

    def test_unknown(self):
        self.assertEqual(format_size(None), "unknown size")


class FitStatusTests(unittest.TestCase):
    def test_fits_under_three_quarters(self):
        free = 8 * GB
        # needs exactly 3/4 of free RAM: fits.
        size = free * 3 // 4 - HEADROOM_BYTES
        self.assertEqual(fit_status(size, free), "fits")
        # one byte more: tight.
        self.assertEqual(fit_status(size + 1, free), "tight")

    def test_tight_up_to_free_ram(self):
        free = 8 * GB
        size = free - HEADROOM_BYTES
        self.assertEqual(fit_status(size, free), "tight")
        # one byte more: too big.
        self.assertEqual(fit_status(size + 1, free), "too big")

    def test_too_big(self):
        self.assertEqual(fit_status(20 * GB, 8 * GB), "too big")

    def test_unknown_ram(self):
        self.assertEqual(fit_status(1024, None), "unknown")


class RamCheckTests(unittest.TestCase):
    def test_ok(self):
        verdict, message = ram_check(1024 * 1024, 8 * GB)
        self.assertEqual(verdict, "ok")
        self.assertIn("fits", message)

    def test_warn(self):
        verdict, message = ram_check(8 * GB - HEADROOM_BYTES, 8 * GB)
        self.assertEqual(verdict, "warn")
        self.assertIn("tight", message)

    def test_refuse(self):
        verdict, message = ram_check(20 * GB, 8 * GB)
        self.assertEqual(verdict, "refuse")
        self.assertIn("will not fit", message)

    def test_unknown_ram_is_ok(self):
        verdict, _message = ram_check(1024, None)
        self.assertEqual(verdict, "ok")


class ReadRamTests(unittest.TestCase):
    def test_reads_numbers_on_linux(self):
        total, available = read_ram_bytes()
        if total is None:
            self.skipTest("not a Linux /proc/meminfo machine")
        self.assertGreater(total, 0)
        self.assertGreaterEqual(available, 0)
        self.assertLessEqual(available, total)


class HomeTests(unittest.TestCase):
    def test_home_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FYRN_HOME")
            os.environ["FYRN_HOME"] = tmp
            try:
                self.assertEqual(get_home(), Path(tmp))
                home = ensure_home()
                self.assertTrue((home / "logs").is_dir())
            finally:
                if old is None:
                    del os.environ["FYRN_HOME"]
                else:
                    os.environ["FYRN_HOME"] = old

    def test_default_models_folder(self):
        self.assertEqual(default_models_folder(),
                         Path.home() / "Models")


class ScanModelsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, size=100):
        path = self.folder / name
        path.write_bytes(b"x" * size)
        return path

    def test_empty_folder(self):
        models, skipped = scan_models(self.folder)
        self.assertEqual(models, [])
        self.assertEqual(skipped, [])

    def test_missing_folder(self):
        models, skipped = scan_models(self.folder / "nope")
        self.assertEqual(models, [])
        self.assertEqual(skipped, [])

    def test_lists_gguf_sorted(self):
        self._write("b-model.gguf", 200)
        self._write("a-model.gguf", 100)
        models, skipped = scan_models(self.folder)
        self.assertEqual([m["name"] for m in models],
                         ["a-model.gguf", "b-model.gguf"])
        self.assertEqual(models[0]["size"], 100)
        self.assertEqual(skipped, [])

    def test_ignores_non_gguf_and_part_files(self):
        self._write("notes.txt")
        self._write("half.gguf.part")
        self._write("real.gguf")
        models, _skipped = scan_models(self.folder)
        self.assertEqual([m["name"] for m in models], ["real.gguf"])

    def test_gguf_folder_is_skipped_not_silent(self):
        (self.folder / "weird.gguf").mkdir()
        models, skipped = scan_models(self.folder)
        self.assertEqual(models, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0][0], "weird.gguf")
        self.assertIn("folder", skipped[0][1])

    def test_case_insensitive_suffix(self):
        self._write("UPPER.GGUF")
        models, _skipped = scan_models(self.folder)
        self.assertEqual([m["name"] for m in models], ["UPPER.GGUF"])


class GgufMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_v3_header_parses_strings(self):
        path = write_gguf_file(
            self.folder / "a.gguf",
            {"general.name": "Tiny Test",
             "general.description": "A small test model."})
        metadata = read_gguf_metadata(path)
        self.assertEqual(metadata.get("general.name"), "Tiny Test")
        self.assertEqual(metadata.get("general.description"),
                         "A small test model.")
        # Non-string entries are skipped, not returned.
        self.assertNotIn("general.file_type", metadata)
        self.assertNotIn("general.tags", metadata)

    def test_v2_header_parses(self):
        path = write_gguf_file(
            self.folder / "a.gguf",
            {"general.description": "Old format, still readable."},
            version=2)
        self.assertEqual(read_gguf_metadata(path).get(
            "general.description"), "Old format, still readable.")

    def test_garbage_file_returns_empty(self):
        path = make_fake_gguf(self.folder / "fake.gguf", 2048)
        self.assertEqual(read_gguf_metadata(path), {})

    def test_truncated_header_returns_empty(self):
        path = write_gguf_file(
            self.folder / "a.gguf",
            {"general.description": "Cut short."})
        data = path.read_bytes()
        cut = self.folder / "cut.gguf"
        cut.write_bytes(data[:20])  # magic + version + part of counts
        self.assertEqual(read_gguf_metadata(cut), {})

    def test_missing_file_returns_empty(self):
        self.assertEqual(
            read_gguf_metadata(self.folder / "nope.gguf"), {})

    def test_scan_includes_description(self):
        write_gguf_file(
            self.folder / "described.gguf",
            {"general.description": "Does test things."})
        make_fake_gguf(self.folder / "plain.gguf", 1024)
        models, _skipped = scan_models(self.folder)
        by_name = {m["name"]: m for m in models}
        self.assertEqual(by_name["described.gguf"]["description"],
                         "Does test things.")
        self.assertEqual(by_name["plain.gguf"]["description"], "")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.folder = self.base / "models"
        self.source = self.base / "elsewhere"

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_copies_file(self):
        self.source.mkdir()
        src = self.source / "tuned.gguf"
        src.write_bytes(b"model-bytes")
        dest = import_gguf(src, self.folder)
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), b"model-bytes")

    def test_import_creates_folder(self):
        self.source.mkdir()
        src = self.source / "tuned.gguf"
        src.write_bytes(b"x")
        import_gguf(src, self.folder)
        self.assertTrue(self.folder.is_dir())

    def test_import_same_folder_is_noop(self):
        ensure_models_folder(self.folder)
        src = self.folder / "already.gguf"
        src.write_bytes(b"x")
        self.assertEqual(import_gguf(src, self.folder), src)

    def test_import_missing_file(self):
        with self.assertRaises(ValueError):
            import_gguf(self.base / "gone.gguf", self.folder)

    def test_import_not_gguf(self):
        self.source.mkdir()
        src = self.source / "notes.txt"
        src.write_text("hi")
        with self.assertRaises(ValueError):
            import_gguf(src, self.folder)

    def test_import_duplicate_name_refused(self):
        self.source.mkdir()
        src = self.source / "same.gguf"
        src.write_bytes(b"x")
        import_gguf(src, self.folder)
        with self.assertRaises(ValueError):
            import_gguf(src, self.folder)


class RemoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_remove_deletes_file(self):
        path = self.folder / "alpha.gguf"
        path.write_bytes(b"x" * 100)
        remove_gguf(path)
        self.assertFalse(path.exists())

    def test_remove_missing_file_raises(self):
        with self.assertRaises(ValueError) as ctx:
            remove_gguf(self.folder / "gone.gguf")
        self.assertIn("already gone", str(ctx.exception))

    def test_remove_non_gguf_raises(self):
        path = self.folder / "notes.txt"
        path.write_bytes(b"x")
        with self.assertRaises(ValueError) as ctx:
            remove_gguf(path)
        self.assertIn("not a .gguf", str(ctx.exception))
        self.assertTrue(path.exists())


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.small = self.folder / "small.gguf"
        self.small.write_bytes(b"x" * 1024)
        self.other = self.folder / "other.gguf"
        self.other.write_bytes(b"y" * 1024)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_and_unload(self):
        runner = FakeRunner()
        ctrl = ModelController(runner, free_ram_fn=lambda: 8 * GB)
        result = ctrl.load(self.small)
        self.assertTrue(ctrl.is_loaded)
        self.assertEqual(ctrl.current_name, "small.gguf")
        self.assertEqual(result["warning"], "")
        self.assertGreaterEqual(result["load_time_s"], 0.0)
        ctrl.unload()
        self.assertFalse(ctrl.is_loaded)
        self.assertIsNone(ctrl.current_path)
        self.assertFalse(runner.is_loaded)

    def test_load_second_auto_unloads_first(self):
        runner = FakeRunner()
        ctrl = ModelController(runner, free_ram_fn=lambda: 8 * GB)
        ctrl.load(self.small)
        ctrl.load(self.other)
        self.assertEqual(ctrl.current_name, "other.gguf")
        self.assertEqual(runner.model_name, "other.gguf")

    def test_refuse_keeps_current_model(self):
        runner = FakeRunner()
        free = [8 * GB]
        ctrl = ModelController(runner, free_ram_fn=lambda: free[0])
        ctrl.load(self.small)
        free[0] = 1024  # almost no RAM left
        with self.assertRaises(ModelTooBigError):
            ctrl.load(self.other)
        self.assertTrue(ctrl.is_loaded)
        self.assertEqual(ctrl.current_name, "small.gguf")

    def test_warn_loads_with_warning(self):
        runner = FakeRunner()
        # Free RAM just above the file + headroom: tight, not refused.
        free = 1024 + HEADROOM_BYTES + 1024
        ctrl = ModelController(runner, free_ram_fn=lambda: free)
        result = ctrl.load(self.small)
        self.assertTrue(ctrl.is_loaded)
        self.assertIn("tight", result["warning"])

    def test_missing_file(self):
        runner = FakeRunner()
        ctrl = ModelController(runner, free_ram_fn=lambda: 8 * GB)
        with self.assertRaises(ModelNotFoundError):
            ctrl.load(self.folder / "gone.gguf")

    def test_check_before_load_verdicts(self):
        runner = FakeRunner()
        ctrl = ModelController(runner, free_ram_fn=lambda: 8 * GB)
        verdict, _msg = ctrl.check_before_load(self.small)
        self.assertEqual(verdict, "ok")
        ctrl_tiny = ModelController(
            runner, free_ram_fn=lambda: 1024)
        verdict, _msg = ctrl_tiny.check_before_load(self.small)
        self.assertEqual(verdict, "refuse")


class GgufNumbersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_numbers_parse_and_strings_still_exclude_them(self):
        path = write_gguf_file(
            self.folder / "a.gguf",
            {"general.architecture": "llama"},
            numbers={"llama.block_count": 24,
                     "llama.attention.head_count": 16})
        numbers = read_gguf_numbers(path)
        self.assertEqual(numbers.get("llama.block_count"), 24)
        self.assertEqual(numbers.get("llama.attention.head_count"),
                         16)
        # String metadata still excludes numbers (existing
        # contract: read_gguf_metadata is strings only).
        metadata = read_gguf_metadata(path)
        self.assertNotIn("llama.block_count", metadata)
        self.assertEqual(metadata.get("general.architecture"),
                         "llama")

    def test_garbage_file_returns_empty_numbers(self):
        path = make_fake_gguf(self.folder / "fake.gguf", 2048)
        self.assertEqual(read_gguf_numbers(path), {})


class KvCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _header(self, **numbers):
        header = {"general.architecture": "llama"}
        header.update(numbers)
        return header

    def test_per_token_math(self):
        # 24 layers, 4 KV heads, 128-dim keys and values:
        # 24 * (4*128 + 4*128) values * 2 bytes = 49,152.
        header = self._header(
            **{"llama.block_count": 24,
               "llama.attention.head_count": 16,
               "llama.attention.head_count_kv": 4,
               "llama.attention.key_length": 128,
               "llama.attention.value_length": 128})
        self.assertEqual(kv_cache_bytes_per_token(header), 49152)

    def test_head_dim_derived_from_embedding(self):
        # No key_length: embedding 2048 over 16 heads is 128.
        header = self._header(
            **{"llama.block_count": 2,
               "llama.embedding_length": 2048,
               "llama.attention.head_count": 16,
               "llama.attention.head_count_kv": 2})
        self.assertEqual(kv_cache_bytes_per_token(header),
                         2 * (2 * 128 + 2 * 128) * 2)

    def test_missing_pieces_return_none(self):
        self.assertIsNone(kv_cache_bytes_per_token({}))
        self.assertIsNone(kv_cache_bytes_per_token(
            self._header(**{"llama.attention.head_count": 16})))
        self.assertIsNone(kv_cache_bytes_per_token(
            self._header(**{"llama.block_count": 24})))

    def test_headroom_from_a_real_file(self):
        path = write_gguf_file(
            self.folder / "a.gguf",
            {"general.architecture": "llama"},
            numbers={"llama.block_count": 2,
                     "llama.attention.head_count": 4,
                     "llama.attention.head_count_kv": 1,
                     "llama.attention.key_length": 64,
                     "llama.attention.value_length": 64})
        # Per token: 2 * (64 + 64) * 2 = 512 bytes. At 2048
        # context: 1 MB, plus the fixed working memory.
        self.assertEqual(model_headroom_bytes(path, 2048),
                         512 * 2048 + WORKING_BYTES)

    def test_headroom_falls_back_without_numbers(self):
        path = make_fake_gguf(self.folder / "fake.gguf", 2048)
        self.assertEqual(model_headroom_bytes(path, 4096),
                         HEADROOM_BYTES)


class PerLayerKvTests(unittest.TestCase):
    """v0.7 (F3): per-layer KV head counts. LFM3's
    attention.head_count_kv is an array of 24 ints (11 layers
    with 4 KV heads, 13 convolution layers with 0), not a
    scalar. The old code fell back to head_count=16 for all 24
    layers and charged 196,608 bytes per token; the real number
    is 22,528, measured against a real LFM3 load."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _header(self, heads_kv, layers=4):
        return {"general.architecture": "lfm3",
                "lfm3.block_count": layers,
                "lfm3.attention.head_count": 16,
                "lfm3.attention.head_count_kv": heads_kv,
                "lfm3.attention.key_length": 128,
                "lfm3.attention.value_length": 128}

    def test_zero_head_layers_cache_nothing(self):
        # 4 layers, heads [0, 4, 0, 2]: keys (0 + 512 + 0 + 256)
        # plus values padded to the largest (512) for the 2 KV
        # layers, times 2 bytes: (768 + 1024) * 2 = 3,584.
        header = self._header([0, 4, 0, 2])
        self.assertEqual(kv_cache_bytes_per_token(header), 3584)

    def test_values_padded_to_the_largest_layer(self):
        # Heads [1, 4]: value sizes 128 and 512. The backend
        # pads every KV layer's values to 512, so both layers
        # pay 512: keys (128 + 512) + values (512 + 512), times
        # 2 bytes = 3,328.
        header = self._header([1, 4], layers=2)
        self.assertEqual(kv_cache_bytes_per_token(header), 3328)

    def test_all_zero_heads_means_no_kv(self):
        header = self._header([0, 0, 0, 0])
        self.assertEqual(kv_cache_bytes_per_token(header), 0)

    def test_bad_arrays_return_none(self):
        # Wrong length, a negative count, and a non-integer:
        # inconsistent data falls back to the fixed headroom.
        self.assertIsNone(kv_cache_bytes_per_token(
            self._header([4, 4, 4])))
        self.assertIsNone(kv_cache_bytes_per_token(
            self._header([4, -1, 4, 4])))
        self.assertIsNone(kv_cache_bytes_per_token(
            self._header([4, "4", 4, 4])))

    def test_headroom_reads_the_array_from_a_file(self):
        path = write_gguf_file(
            self.folder / "hybrid.gguf",
            {"general.architecture": "lfm3"},
            numbers={"lfm3.block_count": 4,
                     "lfm3.attention.head_count": 16,
                     "lfm3.attention.key_length": 128,
                     "lfm3.attention.value_length": 128},
            arrays={"lfm3.attention.head_count_kv": (5, [0, 4, 0, 2])})
        # 3,584 bytes per token (see above) at 2048 context,
        # plus the fixed working memory.
        self.assertEqual(model_headroom_bytes(path, 2048),
                         3584 * 2048 + WORKING_BYTES)

    def test_real_lfm3_blob_numbers(self):
        blob = Path(
            "/home/hatch/workspace/ollama/models/blobs/sha256-"
            "3793e1e6e2266919d6e015287221b41100175a98eddaba92"
            "e111aed3b05c93c4")
        if not blob.is_file():
            self.skipTest("roster blob absent")
        # The real LFM3 header: 24 layers, 11 with 4 KV heads.
        array = read_gguf_number_array(
            blob, "lfm3.attention.head_count_kv")
        self.assertEqual(len(array), 24)
        self.assertEqual(sum(1 for count in array if count > 0),
                         11)
        self.assertEqual(model_headroom_bytes(blob, 2048),
                         22528 * 2048 + WORKING_BYTES)


class GgufNumberArrayTests(unittest.TestCase):
    """read_gguf_number_array: the per-layer array reader."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_an_int_array(self):
        path = write_gguf_file(
            self.folder / "a.gguf",
            {"general.architecture": "lfm3"},
            arrays={"lfm3.attention.head_count_kv":
                    (5, [0, 4, 0, 2])})
        self.assertEqual(
            read_gguf_number_array(
                path, "lfm3.attention.head_count_kv"),
            [0, 4, 0, 2])

    def test_missing_key_scalar_key_and_bad_file_are_none(self):
        path = write_gguf_file(
            self.folder / "a.gguf",
            {"general.architecture": "llama"},
            numbers={"llama.block_count": 2})
        self.assertIsNone(read_gguf_number_array(
            path, "llama.attention.head_count_kv"))
        # A scalar under the key is not an array.
        self.assertIsNone(read_gguf_number_array(
            path, "llama.block_count"))
        self.assertIsNone(read_gguf_number_array(
            self.folder / "gone.gguf", "llama.block_count"))
        self.assertIsNone(read_gguf_number_array(
            Path("/nonexistent.gguf"), "llama.block_count"))


class FitHeadroomTests(unittest.TestCase):
    def test_explicit_headroom_moves_the_verdict(self):
        free = 8 * GB
        # 5 GB file, fixed 512 MB headroom: 5.5 GB of 8 GB fits.
        self.assertEqual(fit_status(5 * GB, free), "fits")
        # The same file with a real 2 GB headroom: 7 GB is tight.
        self.assertEqual(fit_status(5 * GB, free, 2 * GB), "tight")
        # With a real 4 GB headroom: 9 GB of 8 GB is too big.
        self.assertEqual(fit_status(5 * GB, free, 4 * GB), "too big")
        # And the other way: a 6 GB file is tight on the fixed
        # headroom (6.5 GB) but fits on a small real one (6 GB).
        self.assertEqual(fit_status(6 * GB, free), "tight")
        self.assertEqual(fit_status(6 * GB, free, 0), "fits")

    def test_ram_check_uses_the_headroom_in_its_message(self):
        _verdict, message = ram_check(7 * GB, 8 * GB,
                                      2 * GB)
        self.assertIn("9.0 GB needed", message)


class StatRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.small = self.folder / "small.gguf"
        self.small.write_bytes(b"x" * 1024)

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_vanishing_between_checks_is_missing(self):
        runner = FakeRunner()
        ctrl = ModelController(runner, free_ram_fn=lambda: 8 * GB)
        fake_path = mock.MagicMock()
        fake_path.is_file.return_value = True
        fake_path.stat.side_effect = OSError("vanished")
        with mock.patch("fyrn.core.Path",
                        return_value=fake_path):
            with self.assertRaises(ModelNotFoundError):
                ctrl.check_before_load(self.small)


class ImportChunkedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.folder = self.base / "models"
        self.source = self.base / "elsewhere"
        self.source.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_progress_reports_copied_and_total(self):
        src = self.source / "big.gguf"
        make_fake_gguf(src, 3 * 1024 * 1024)  # 3 chunks
        seen = []
        dest = import_gguf(
            src, self.folder,
            progress_cb=lambda c, t: seen.append((c, t)))
        self.assertTrue(dest.is_file())
        self.assertTrue(seen)
        self.assertEqual(seen[-1], (3 * 1024 * 1024,
                                    3 * 1024 * 1024))
        self.assertFalse((self.folder / "big.gguf.part").exists())

    def test_cancel_stops_and_leaves_nothing(self):
        src = self.source / "big.gguf"
        make_fake_gguf(src, 2 * 1024 * 1024)
        event = threading.Event()
        event.set()
        with self.assertRaises(ImportCancelled):
            import_gguf(src, self.folder, cancel_event=event)
        self.assertFalse((self.folder / "big.gguf").exists())
        self.assertFalse((self.folder / "big.gguf.part").exists())

    def test_no_disk_space_refuses_plainly(self):
        src = self.source / "big.gguf"
        make_fake_gguf(src, 1024)
        usage = mock.Mock()
        usage.free = 512  # less than the file
        with mock.patch("fyrn.core.shutil.disk_usage",
                        return_value=usage):
            with self.assertRaises(ValueError) as ctx:
                import_gguf(src, self.folder)
        self.assertIn("disk space", str(ctx.exception))
        self.assertFalse((self.folder / "big.gguf").exists())


class ListPartFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lists_parts_not_sidecars_or_models(self):
        (self.folder / "a.gguf.part").write_bytes(b"x" * 100)
        (self.folder / "a.gguf.part.url").write_text("http://x")
        (self.folder / "a.gguf.part.etag").write_text("v1")
        (self.folder / "done.gguf").write_bytes(b"y" * 50)
        parts = list_part_files(self.folder)
        self.assertEqual([p["name"] for p in parts],
                         ["a.gguf.part"])
        self.assertEqual(parts[0]["size"], 100)

    def test_missing_folder_is_empty(self):
        self.assertEqual(list_part_files(self.folder / "nope"), [])


class DeadConstantTests(unittest.TestCase):
    def test_no_dead_version_constant_in_core(self):
        # The version lives in fyrn/__init__.py only; a second
        # copy in core.py is how version drift starts.
        self.assertFalse(hasattr(fyrn.core, "VERSION"))
        self.assertEqual(fyrn.core.APP_NAME, "Fyrn")


class GgufFactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_facts_from_header(self):
        path = write_gguf_file(
            self.folder / "a.gguf",
            {"general.architecture": "qwen3"},
            numbers={"qwen3.context_length": 32768},
            tensors=[("blk.0.attn_q.weight", [2000, 1000000], 0)],
            file_type=15)  # Q4_K_M
        facts = read_gguf_facts(path)
        self.assertEqual(facts["params"], 2000000000)
        self.assertEqual(facts["params_text"], "2B")
        self.assertEqual(facts["architecture"], "qwen3")
        self.assertEqual(facts["context_length"], 32768)
        self.assertEqual(facts["context_text"], "32K context")
        self.assertEqual(facts["quantization"], "Q4_K_M")
        self.assertEqual(format_facts(facts),
                         "2B · Q4_K_M · 32K context")

    def test_millions_format(self):
        path = write_gguf_file(
            self.folder / "a.gguf", {},
            tensors=[("w", [1000, 1000], 0)])
        facts = read_gguf_facts(path)
        self.assertEqual(facts["params_text"], "1M")
        self.assertEqual(format_facts(facts), "1M · F32")

    def test_unknown_file_type_shows_nothing(self):
        # A file type the table does not know is no fact at all:
        # never a guess.
        path = write_gguf_file(self.folder / "a.gguf", {},
                               file_type=999)
        facts = read_gguf_facts(path)
        self.assertNotIn("quantization", facts)
        self.assertEqual(format_facts(facts), "")

    def test_context_needs_architecture(self):
        # A context length without its architecture key is not
        # attributable to anything: no context fact.
        path = write_gguf_file(
            self.folder / "a.gguf", {},
            numbers={"qwen3.context_length": 32768})
        facts = read_gguf_facts(path)
        self.assertNotIn("context_length", facts)
        self.assertNotIn("context_text", facts)

    def test_architecture_without_context(self):
        path = write_gguf_file(
            self.folder / "a.gguf",
            {"general.architecture": "qwen3"})
        facts = read_gguf_facts(path)
        self.assertEqual(facts["architecture"], "qwen3")
        self.assertNotIn("context_length", facts)

    def test_bad_file_has_no_facts(self):
        path = make_fake_gguf(self.folder / "fake.gguf", 2048)
        self.assertEqual(read_gguf_facts(path), {})
        self.assertEqual(format_facts({}), "")

    def test_missing_file_has_no_facts(self):
        self.assertEqual(
            read_gguf_facts(self.folder / "nope.gguf"), {})

    def test_scan_includes_facts(self):
        write_gguf_file(
            self.folder / "real.gguf",
            {"general.architecture": "qwen3"},
            file_type=15)
        make_fake_gguf(self.folder / "plain.gguf", 1024)
        models, _skipped = scan_models(self.folder)
        by_name = {m["name"]: m for m in models}
        self.assertEqual(
            by_name["real.gguf"]["facts"]["quantization"],
            "Q4_K_M")
        self.assertEqual(by_name["plain.gguf"]["facts"], {})


class TensorParamsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_count_sums_tensors(self):
        path = write_gguf_file(
            self.folder / "a.gguf", {},
            tensors=[("a", [2, 3], 0), ("b", [4], 0)])
        self.assertEqual(read_gguf_tensor_params(path), 10)

    def test_no_tensors_is_zero(self):
        path = write_gguf_file(self.folder / "a.gguf", {})
        self.assertEqual(read_gguf_tensor_params(path), 0)

    def test_bad_file_is_none(self):
        path = make_fake_gguf(self.folder / "fake.gguf", 2048)
        self.assertIsNone(read_gguf_tensor_params(path))

    def test_truncated_tensor_section_is_none(self):
        path = write_gguf_file(
            self.folder / "a.gguf", {},
            tensors=[("a", [2, 3], 0), ("b", [4], 0)])
        data = path.read_bytes()
        cut = self.folder / "cut.gguf"
        cut.write_bytes(data[:-4])  # mid-tensor-info
        self.assertIsNone(read_gguf_tensor_params(cut))


class FindProjectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _model(self, name="Qwen3-VL-2B-Instruct-Q4_K_M.gguf"):
        path = self.folder / name
        path.write_bytes(b"x")
        return path

    def test_no_projector_is_none(self):
        self.assertIsNone(find_projector(self._model()))
        self.assertEqual(projector_size_bytes(self._model()), 0)

    def test_one_candidate_wins(self):
        model = self._model()
        projector = self.folder / "mmproj-Qwen3-VL-2B-Q8_0.gguf"
        projector.write_bytes(b"p" * 100)
        self.assertEqual(find_projector(model), projector)
        self.assertEqual(projector_size_bytes(model), 100)

    def test_matching_stem_wins(self):
        model = self._model()
        wrong = self.folder / "mmproj-llava-1.6-mistral-7b.gguf"
        wrong.write_bytes(b"x")
        right = self.folder / "mmproj-Qwen3-VL-2B-Instruct-Q8_0.gguf"
        right.write_bytes(b"x")
        self.assertEqual(find_projector(model), right)

    def test_tie_is_none(self):
        model = self._model("model.gguf")
        (self.folder / "mmproj-a-q4_k_m.gguf").write_bytes(b"x")
        (self.folder / "mmproj-b-q4_k_m.gguf").write_bytes(b"x")
        # Both share nothing with the model name: a tie means no
        # projector, never a guess between two.
        self.assertIsNone(find_projector(model))

    def test_case_insensitive_and_non_gguf_ignored(self):
        model = self._model()
        (self.folder / "mmproj-foo.txt").write_bytes(b"x")
        projector = self.folder / "MMPROJ-Qwen3-VL-2B-Q8_0.GGUF"
        projector.write_bytes(b"x")
        self.assertEqual(find_projector(model), projector)

    def test_missing_folder_is_none(self):
        self.assertIsNone(
            find_projector(self.folder / "nope" / "a.gguf"))


class MoveToTrashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.data_home = self.folder / "data"
        self._old_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = str(self.data_home)

    def tearDown(self):
        if self._old_xdg is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self._old_xdg
        self.tmp.cleanup()

    def test_trash_dir_uses_xdg_data_home(self):
        self.assertEqual(trash_dir(), self.data_home / "Trash")

    def test_move_lands_in_trash_with_info(self):
        path = self.folder / "alpha.gguf"
        path.write_bytes(b"model-bytes")
        dest = move_to_trash(path)
        self.assertFalse(path.exists())
        self.assertEqual(dest, self.data_home / "Trash" / "files"
                         / "alpha.gguf")
        self.assertEqual(dest.read_bytes(), b"model-bytes")
        info = (self.data_home / "Trash" / "info"
                / "alpha.gguf.trashinfo").read_text()
        self.assertIn("[Trash Info]", info)
        self.assertIn("Path=%s" % path.resolve(), info)
        self.assertRegex(info, r"DeletionDate=\d{4}-\d{2}-\d{2}T"
                         r"\d{2}:\d{2}:\d{2}")

    def test_collision_numbers_the_name(self):
        first_dir = self.folder / "one"
        second_dir = self.folder / "two"
        first_dir.mkdir()
        second_dir.mkdir()
        (first_dir / "same.gguf").write_bytes(b"1")
        (second_dir / "same.gguf").write_bytes(b"2")
        move_to_trash(first_dir / "same.gguf")
        dest = move_to_trash(second_dir / "same.gguf")
        self.assertEqual(dest.name, "same.gguf.1")
        self.assertEqual(dest.read_bytes(), b"2")
        self.assertTrue((self.data_home / "Trash" / "info"
                         / "same.gguf.1.trashinfo").is_file())

    def test_missing_file_raises(self):
        with self.assertRaises(ValueError) as ctx:
            move_to_trash(self.folder / "gone.gguf")
        self.assertIn("already gone", str(ctx.exception))

    def test_non_gguf_raises_and_keeps_file(self):
        path = self.folder / "notes.txt"
        path.write_bytes(b"x")
        with self.assertRaises(ValueError) as ctx:
            move_to_trash(path)
        self.assertIn("not a .gguf", str(ctx.exception))
        self.assertTrue(path.exists())

    def test_cross_device_falls_back_to_copy(self):
        path = self.folder / "alpha.gguf"
        path.write_bytes(b"model-bytes")
        with mock.patch("fyrn.core.os.replace",
                        side_effect=OSError(
                            18, "Invalid cross-device link")):
            dest = move_to_trash(path)
        self.assertFalse(path.exists())
        self.assertEqual(dest.read_bytes(), b"model-bytes")

    def test_failed_move_keeps_the_file(self):
        path = self.folder / "alpha.gguf"
        path.write_bytes(b"model-bytes")
        with mock.patch("fyrn.core.os.replace",
                        side_effect=OSError(13, "Permission denied")):
            with self.assertRaises(ValueError):
                move_to_trash(path)
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b"model-bytes")
        self.assertFalse((self.data_home / "Trash" / "files"
                          / "alpha.gguf").exists())


class WorkingAllowanceTests(unittest.TestCase):
    """The working allowance (v0.9) is the measured one: real
    loads showed the engine's own buffers beyond file + KV run
    to about 713 MB on the worst measured roster model, so the
    allowance is 768 MB, not the old optimistic 64 MB."""

    def test_allowance_is_the_measured_figure(self):
        self.assertEqual(WORKING_BYTES, 768 * 1024 * 1024)

    def test_a_model_near_the_line_flips_with_the_allowance(self):
        # A model whose file + KV lands between the two
        # allowances: the old 64 MB working figure called it a
        # fit; the measured 768 MB figure honestly calls it
        # tight.
        from helpers import write_gguf_file
        from fyrn.core import fit_status, model_headroom_bytes
        with tempfile.TemporaryDirectory() as tmp:
            path = write_gguf_file(
                Path(tmp) / "kv.gguf",
                {"general.architecture": "llama"},
                numbers={"llama.block_count": 32,
                         "llama.embedding_length": 2048,
                         "llama.attention.head_count": 32,
                         "llama.attention.head_count_kv": 8})
            # KV here is 65,536 B/token: 128 MB at 2048 tokens.
            headroom = model_headroom_bytes(path, 2048)
        self.assertEqual(headroom,
                         128 * 1024 * 1024 + WORKING_BYTES)
        free = 4 * GB
        size = int(2.5 * GB)
        self.assertEqual(fit_status(size, free, headroom),
                         "tight")
        old_headroom = 128 * 1024 * 1024 + 64 * 1024 * 1024
        self.assertEqual(fit_status(size, free, old_headroom),
                         "fits")


if __name__ == "__main__":
    unittest.main()
