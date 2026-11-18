"""Tests for the self-update module (v0.6): version compare,
the release check (stubbed network), the checksum sidecar,
staging, the verified install, and the restarter. Stdlib-only:
no Qt, no network."""
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fyrn import updates
from fyrn.updates import UpdateError


def fake_response(payload):
    """A urlopen stub returning payload (bytes or str) as a
    context manager, the way urllib's response works."""
    data = payload.encode("utf-8") if isinstance(payload, str) else payload

    class _Response:
        def read(self):
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(_url, timeout=None):
        return _Response()

    return opener


def release_json(tag, assets=None, url=None):
    payload = {"tag_name": tag,
               "html_url": url or "https://github.com/x/y/releases/tag/" + tag,
               "assets": assets if assets is not None else [
                   {"name": "fyrn-v0.7.zip",
                    "browser_download_url":
                        "https://example.invalid/fyrn-v0.7.zip"},
                   {"name": "fyrn-v0.7.zip.sha256",
                    "browser_download_url":
                        "https://example.invalid/fyrn-v0.7.zip.sha256"}]}
    return json.dumps(payload)


def failing_opener(exc):
    def opener(_url, timeout=None):
        raise exc

    return opener


class VersionTests(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(updates.parse_version("v0.6"), (0, 6))
        self.assertEqual(updates.parse_version("0.6"), (0, 6))
        self.assertEqual(updates.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(updates.parse_version("0.6.0"), (0, 6, 0))

    def test_parse_version_refuses_non_versions(self):
        for bad in ("", "latest", "v0.6-beta", "v0.6 beta",
                    "0.6.7.8.9x", None, 7, "v"):
            self.assertIsNone(updates.parse_version(bad), bad)

    def test_is_newer(self):
        self.assertTrue(updates.is_newer("0.7", "0.6"))
        self.assertTrue(updates.is_newer("v0.7", "0.6"))
        self.assertTrue(updates.is_newer("1.0", "0.99"))
        self.assertFalse(updates.is_newer("0.6", "0.6"))
        self.assertFalse(updates.is_newer("0.5", "0.6"))
        self.assertFalse(updates.is_newer("0.6.0", "0.6"))
        self.assertFalse(updates.is_newer("0.6", "0.6.0"))

    def test_is_newer_never_trusts_an_unknown_version(self):
        self.assertFalse(updates.is_newer("not-a-version", "0.6"))
        self.assertFalse(updates.is_newer("0.7", "not-a-version"))
        self.assertFalse(updates.is_newer(None, "0.6"))


class CheckTests(unittest.TestCase):
    def test_newer_release(self):
        result = updates.check_for_update(
            "0.6", urlopen=fake_response(release_json("v0.7")))
        self.assertEqual(result["state"], "newer")
        self.assertEqual(result["latest"], "0.7")
        self.assertEqual(result["zip_name"], "fyrn-v0.7.zip")
        self.assertIn("fyrn-v0.7.zip", result["zip_url"])
        self.assertIn(".sha256", result["sha256_url"])

    def test_current_release(self):
        result = updates.check_for_update(
            "0.6", urlopen=fake_response(release_json("v0.6")))
        self.assertEqual(result["state"], "current")
        self.assertEqual(result["latest"], "0.6")

    def test_dev_tree_newer_than_latest_is_current(self):
        result = updates.check_for_update(
            "0.9", urlopen=fake_response(release_json("v0.7")))
        self.assertEqual(result["state"], "current")

    def test_release_without_sidecar_still_reports_newer(self):
        # Releases before v0.6 carried no sidecar: the check
        # reports the release, and the download step refuses it
        # (an update without a checksum cannot be verified).
        assets = [{"name": "fyrn-v0.7.zip",
                   "browser_download_url":
                       "https://example.invalid/fyrn-v0.7.zip"}]
        result = updates.check_for_update(
            "0.6",
            urlopen=fake_response(release_json("v0.7", assets)))
        self.assertEqual(result["state"], "newer")
        self.assertIsNone(result["sha256_url"])

    def test_network_failure_is_unknown_never_an_exception(self):
        result = updates.check_for_update(
            "0.6",
            urlopen=failing_opener(
                urllib.error.URLError("no route to host")))
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["latest"])
        self.assertIn("GitHub", result["reason"])

    def test_timeout_is_unknown(self):
        result = updates.check_for_update(
            "0.6", urlopen=failing_opener(TimeoutError()))
        self.assertEqual(result["state"], "unknown")
        self.assertIn("timed out", result["reason"])

    def test_bad_json_is_unknown(self):
        result = updates.check_for_update(
            "0.6", urlopen=fake_response("this is not json {"))
        self.assertEqual(result["state"], "unknown")

    def test_non_dict_payload_is_unknown(self):
        result = updates.check_for_update(
            "0.6", urlopen=fake_response('["a", "list"]'))
        self.assertEqual(result["state"], "unknown")

    def test_missing_tag_is_unknown(self):
        result = updates.check_for_update(
            "0.6", urlopen=fake_response('{"assets": []}'))
        self.assertEqual(result["state"], "unknown")

    def test_bad_tag_is_unknown(self):
        result = updates.check_for_update(
            "0.6", urlopen=fake_response(release_json("v0.7-beta")))
        self.assertEqual(result["state"], "unknown")

    def test_release_without_a_zip_is_unknown(self):
        result = updates.check_for_update(
            "0.6", urlopen=fake_response(release_json("v0.7", [])))
        self.assertEqual(result["state"], "unknown")

    def test_fetch_latest_release_raises_plain_update_errors(self):
        with self.assertRaises(UpdateError):
            updates.fetch_latest_release(
                urlopen=failing_opener(
                    urllib.error.URLError("offline")))
        with self.assertRaises(UpdateError):
            updates.fetch_latest_release(
                urlopen=fake_response('{"tag_name": ""}'))


class SidecarTests(unittest.TestCase):
    def test_parse_sidecar_sha256sum_format(self):
        digest = "a" * 64
        self.assertEqual(
            updates.parse_sidecar("%s  fyrn-v0.7.zip\n" % digest),
            digest)

    def test_parse_sidecar_matches_the_named_zip(self):
        good = "b" * 64
        other = "c" * 64
        text = ("%s  fyrn-v0.6.zip\n%s  fyrn-v0.7.zip\n"
                % (other, good))
        self.assertEqual(
            updates.parse_sidecar(text, zip_name="fyrn-v0.7.zip"),
            good)
        # A sidecar for another file is no verification at all.
        self.assertIsNone(
            updates.parse_sidecar(text, zip_name="fyrn-v0.8.zip"))

    def test_parse_sidecar_without_a_name_takes_the_first(self):
        digest = "d" * 64
        text = "%s  fyrn-v0.7.zip\n%s  other.zip\n" % (digest, "e" * 64)
        self.assertEqual(updates.parse_sidecar(text), digest)

    def test_parse_sidecar_refuses_garbage(self):
        self.assertIsNone(updates.parse_sidecar(""))
        self.assertIsNone(updates.parse_sidecar("not a hash here"))
        self.assertIsNone(updates.parse_sidecar("zzzz  fyrn-v0.7.zip"))
        self.assertIsNone(updates.parse_sidecar(None))
        self.assertIsNone(updates.parse_sidecar("a" * 63 + "  x.zip"))

    def test_fetch_sidecar(self):
        digest = "f" * 64
        text = updates.fetch_sidecar(
            "https://example.invalid/x.sha256",
            urlopen=fake_response("%s  x.zip\n" % digest))
        self.assertEqual(text, "%s  x.zip\n" % digest)

    def test_fetch_sidecar_failure_is_a_plain_error(self):
        with self.assertRaises(UpdateError):
            updates.fetch_sidecar(
                "https://example.invalid/x.sha256",
                urlopen=failing_opener(
                    urllib.error.URLError("offline")))

    def test_verify_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file.bin"
            path.write_bytes(b"fyrn")
            digest = hashlib.sha256(b"fyrn").hexdigest()
            self.assertTrue(updates.verify_file(path, digest))
            self.assertTrue(updates.verify_file(path, digest.upper()))
            self.assertFalse(updates.verify_file(path, "0" * 64))
            self.assertFalse(
                updates.verify_file(Path(tmp) / "missing", digest))
            self.assertFalse(updates.verify_file(path, None))


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _stage(self, version="0.7", digest=None):
        zip_path = self.home / "download.zip"
        zip_path.write_bytes(b"fake zip bytes")
        digest = digest or hashlib.sha256(b"fake zip bytes").hexdigest()
        updates.stage_update(self.home, zip_path,
                             "%s  fyrn-v0.7.zip\n" % digest,
                             version, digest)
        return digest

    def test_stage_and_pending_round_trip(self):
        digest = self._stage()
        record = updates.pending_update(self.home)
        self.assertEqual(record["version"], "0.7")
        self.assertEqual(record["zip_sha256"], digest)
        staged = updates.staging_dir(self.home)
        self.assertTrue((staged / "update.zip").is_file())
        self.assertTrue((staged / "update.zip.sha256").is_file())
        # The download moved in: the original is gone.
        self.assertFalse((self.home / "download.zip").exists())

    def test_stage_replaces_a_previous_staging(self):
        self._stage(version="0.7")
        self._stage(version="0.8")
        self.assertEqual(updates.pending_update(self.home)["version"],
                         "0.8")

    def test_clear_staged_update(self):
        self._stage()
        updates.clear_staged_update(self.home)
        self.assertIsNone(updates.pending_update(self.home))
        self.assertFalse(updates.staging_dir(self.home).exists())
        # Clearing nothing is fine.
        updates.clear_staged_update(self.home)

    def test_damaged_record_reads_as_nothing_staged(self):
        staged = updates.staging_dir(self.home)
        staged.mkdir(parents=True)
        (staged / "pending.json").write_text(
            "this is not json", encoding="utf-8")
        self.assertIsNone(updates.pending_update(self.home))
        (staged / "pending.json").write_text(
            '{"version": "0.7"}', encoding="utf-8")
        self.assertIsNone(updates.pending_update(self.home))
        (staged / "pending.json").write_text(
            '["not", "a", "dict"]', encoding="utf-8")
        self.assertIsNone(updates.pending_update(self.home))

    def test_nothing_staged(self):
        self.assertIsNone(updates.pending_update(self.home))


def make_install(install_dir):
    """A fake installed Fyrn: the two files apply() validates,
    plus a file the update will replace."""
    (install_dir / "fyrn").mkdir(parents=True)
    (install_dir / "fyrn" / "__init__.py").write_text(
        '__version__ = "0.6"\n', encoding="utf-8")
    (install_dir / "fyrn" / "gui.py").write_text(
        "# old gui\n", encoding="utf-8")
    script = install_dir / "install.sh"
    script.write_text("#!/bin/sh\n# old installer\n",
                      encoding="utf-8")
    os.chmod(script, 0o755)


def make_zip(zip_path, entries):
    """entries: {zip name: (bytes, mode or None)}."""
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, (data, mode) in entries.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = ((mode or 0o100644) << 16)
            zf.writestr(info, data)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.install = self.base / "fyrn-install"
        self.install.mkdir()
        make_install(self.install)

    def tearDown(self):
        self.tmp.cleanup()

    def _stage_zip(self, entries, digest=None):
        zip_path = self.base / "download.zip"
        make_zip(zip_path, entries)
        digest = digest or updates.sha256_file(zip_path)
        updates.stage_update(
            self.home, zip_path,
            "%s  fyrn-v0.7.zip\n" % digest, "0.7", digest)

    def test_apply_replaces_adds_and_restores_modes(self):
        self._stage_zip({
            "fyrn/fyrn/gui.py": (b"# new gui\n", 0o100644),
            "fyrn/fyrn/newfile.py": (b"# brand new\n", 0o100644),
            "fyrn/install.sh": (b"#!/bin/sh\n# new installer\n",
                                0o100755),
        })
        ok, version = updates.apply_pending_update(
            self.home, install_dir=self.install)
        self.assertTrue(ok, version)
        self.assertEqual(version, "0.7")
        self.assertEqual(
            (self.install / "fyrn" / "gui.py").read_text(),
            "# new gui\n")
        self.assertEqual(
            (self.install / "fyrn" / "newfile.py").read_text(),
            "# brand new\n")
        self.assertTrue(
            (self.install / "install.sh").stat().st_mode
            & stat.S_IXUSR)
        # The staging is consumed, and the old files are backed
        # up for a manual recovery.
        self.assertIsNone(updates.pending_update(self.home))
        backup = self.home / "updates" / "backup"
        self.assertEqual(
            (backup / "fyrn" / "gui.py").read_text(), "# old gui\n")
        # Untouched files stay untouched.
        self.assertEqual(
            (self.install / "fyrn" / "__init__.py").read_text(),
            '__version__ = "0.6"\n')

    def test_apply_leaves_models_settings_and_venv_alone(self):
        # The zip carries program files only: a models folder, a
        # settings file, and a venv sitting in the install dir
        # are never in the zip, so they are never touched.
        models = self.install / "models"
        models.mkdir()
        (models / "a.gguf").write_bytes(b"model")
        venv = self.install / ".venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "pkg.py").write_text("x = 1\n", encoding="utf-8")
        self._stage_zip({
            "fyrn/fyrn/gui.py": (b"# new gui\n", 0o100644),
        })
        ok, _version = updates.apply_pending_update(
            self.home, install_dir=self.install)
        self.assertTrue(ok)
        self.assertEqual((models / "a.gguf").read_bytes(), b"model")
        self.assertEqual((venv / "pkg.py").read_text(), "x = 1\n")

    def test_checksum_mismatch_refuses_and_clears_staging(self):
        self._stage_zip(
            {"fyrn/fyrn/gui.py": (b"# new gui\n", 0o100644)},
            digest="0" * 64)
        ok, message = updates.apply_pending_update(
            self.home, install_dir=self.install)
        self.assertFalse(ok)
        self.assertIn("checksum", message)
        self.assertIsNone(updates.pending_update(self.home))
        self.assertEqual(
            (self.install / "fyrn" / "gui.py").read_text(),
            "# old gui\n")

    def test_corrupt_zip_refuses_and_leaves_install_intact(self):
        zip_path = self.base / "download.zip"
        zip_path.write_bytes(b"this is not a zip at all")
        digest = updates.sha256_file(zip_path)
        updates.stage_update(
            self.home, zip_path,
            "%s  fyrn-v0.7.zip\n" % digest, "0.7", digest)
        ok, _message = updates.apply_pending_update(
            self.home, install_dir=self.install)
        self.assertFalse(ok)
        self.assertEqual(
            (self.install / "fyrn" / "gui.py").read_text(),
            "# old gui\n")

    def test_unsafe_entries_are_refused(self):
        for name in ("../escape.py", "/etc/passwd",
                     "other/gui.py", "fyrn/../escape.py"):
            with self.subTest(name=name):
                self._stage_zip({name: (b"evil\n", 0o100644)})
                ok, _message = updates.apply_pending_update(
                    self.home, install_dir=self.install)
                self.assertFalse(ok)
                self.assertEqual(
                    (self.install / "fyrn" / "gui.py").read_text(),
                    "# old gui\n")
                updates.clear_staged_update(self.home)

    def test_empty_zip_is_refused(self):
        self._stage_zip({})
        ok, _message = updates.apply_pending_update(
            self.home, install_dir=self.install)
        self.assertFalse(ok)

    def test_mid_extract_failure_rolls_back(self):
        # A directory where the zip has a file: the extract
        # fails partway, and the old version must come back.
        (self.install / "fyrn" / "blocked").mkdir()
        self._stage_zip({
            "fyrn/fyrn/gui.py": (b"# new gui\n", 0o100644),
            "fyrn/fyrn/added.py": (b"# added\n", 0o100644),
            "fyrn/fyrn/blocked": (b"# cannot land\n", 0o100644),
        })
        ok, _message = updates.apply_pending_update(
            self.home, install_dir=self.install)
        self.assertFalse(ok)
        self.assertEqual(
            (self.install / "fyrn" / "gui.py").read_text(),
            "# old gui\n")
        self.assertFalse((self.install / "fyrn" / "added.py").exists())
        self.assertTrue((self.install / "fyrn" / "blocked").is_dir())

    def test_nothing_staged_is_a_plain_no(self):
        ok, message = updates.apply_pending_update(
            self.home, install_dir=self.install)
        self.assertFalse(ok)
        self.assertIn("No update", message)

    def test_missing_download_is_cleared(self):
        self._stage_zip({"fyrn/fyrn/gui.py": (b"x\n", 0o100644)})
        (updates.staging_dir(self.home) / "update.zip").unlink()
        ok, _message = updates.apply_pending_update(
            self.home, install_dir=self.install)
        self.assertFalse(ok)
        self.assertIsNone(updates.pending_update(self.home))

    def test_not_an_install_dir_is_refused_and_staging_survives(self):
        bare = self.base / "not-an-install"
        bare.mkdir()
        self._stage_zip({"fyrn/fyrn/gui.py": (b"x\n", 0o100644)})
        ok, message = updates.apply_pending_update(
            self.home, install_dir=bare)
        self.assertFalse(ok)
        self.assertIn("not running from an installed copy", message)
        # The staging stays: a later launch from a real install
        # can still apply it.
        self.assertIsNotNone(updates.pending_update(self.home))

    def test_install_dir_from_here_is_this_tree(self):
        found = updates.install_dir_from_here()
        self.assertTrue((found / "fyrn" / "__init__.py").is_file())


class RestartTests(unittest.TestCase):
    def test_restart_after_exit_runs_the_command(self):
        # A dead PID: the restarter finds the process gone at
        # once and runs argv. The command writes a marker file.
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "restarted.txt"
            script = ("open(%r, 'w').write('restarted')"
                      % str(marker))
            updates.restart_after_exit(
                [sys.executable, "-c", script], pid=99999999)
            deadline = time.time() + 10
            while not marker.exists() and time.time() < deadline:
                time.sleep(0.2)
            self.assertTrue(marker.exists())
            self.assertEqual(marker.read_text(), "restarted")


if __name__ == "__main__":
    unittest.main()
