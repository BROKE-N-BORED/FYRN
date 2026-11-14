"""Tests for fyrn.core: RAM checks, fit badges, model scan,
import, and the one-model-at-a-time controller."""

import os
import tempfile
import unittest
from pathlib import Path

from fyrn.core import (HEADROOM_BYTES, ModelController,
                             ModelNotFoundError, ModelTooBigError,
                             default_models_folder, ensure_home,
                             ensure_models_folder, fit_status,
                             format_size, get_home, import_gguf,
                             ram_check, read_ram_bytes, scan_models)
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


if __name__ == "__main__":
    unittest.main()
