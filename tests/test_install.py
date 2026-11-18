"""Static checks on the installer, launchers, desktop files, and
zip builder. No venv is created here; the install simulation is a
separate verification step (see the build report)."""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


class InstallScriptTests(unittest.TestCase):
    def test_install_sh_is_bash_with_pipefail(self):
        text = read("install.sh")
        first = text.splitlines()[0]
        self.assertIn("bash", first)
        self.assertIn("set -euo pipefail", text)

    def test_install_sh_never_says_sh_install_sh(self):
        # "bash install.sh" contains "sh install.sh" as a substring,
        # so only a bare "sh install.sh" counts.
        for name in ("install.sh", "README.md", "CHANGELOG.md"):
            text = read(name)
            self.assertIsNone(
                re.search(r"(?<!ba)sh install\.sh", text),
                "%s tells the user to run sh install.sh" % name)

    def test_install_sh_builds_venv_and_installs_deps(self):
        text = read("install.sh")
        self.assertIn("python3 -m venv", text)
        self.assertIn("pip install --quiet -r requirements.txt", text)

    def test_install_sh_verifies_by_import(self):
        # The fatal check is an import, never a dist-name lookup:
        # the installed dist is PySide6-Essentials, not PySide6,
        # so a metadata check for "PySide6" would always fail.
        # (The dist name may appear in a comment explaining this.)
        text = read("install.sh")
        self.assertIn("import PySide6", text)
        self.assertIn("import llama_cpp", text)
        self.assertNotIn("importlib.metadata", text)
        self.assertNotIn("pip show", text)

    def test_install_sh_bakes_app_dir_into_desktop(self):
        text = read("install.sh")
        self.assertIn("__APP_DIR__", text)
        self.assertIn("__HOME__", text)
        self.assertIn(".local/share/applications", text)

    def test_install_sh_installs_launcher_from_template(self):
        # The launcher is installed from launcher.sh (which finds
        # the folder again if it moved), not written inline.
        text = read("install.sh")
        self.assertIn("launcher.sh", text)
        self.assertIn(".local/bin/fyrn", text)

    def test_install_sh_checks_disk_space_first(self):
        # 1 GB floor, plain message, before the venv is created.
        text = read("install.sh")
        self.assertIn("df -P -k", text)
        self.assertIn("1048576", text)
        self.assertIn("Not enough free disk space", text)
        self.assertLess(text.index("df -P -k"),
                        text.index("python3 -m venv"))

    def test_run_sh_launches_from_venv(self):
        text = read("run.sh")
        self.assertIn(".venv/bin/python -m fyrn", text)
        self.assertIn("exec", text)

    def test_launcher_sets_pythonpath(self):
        # `fyrn` must work from any folder: without PYTHONPATH,
        # `python -m fyrn` only finds the package when the terminal
        # happens to be in the app folder (caught by the install
        # sim: "No module named fyrn" from $HOME).
        text = read("launcher.sh")
        self.assertIn("PYTHONPATH=", text)
        self.assertIn(".venv/bin/python\" -m fyrn", text)

    def test_launcher_template_has_app_dir_placeholder(self):
        text = read("launcher.sh")
        self.assertIn('APP_DIR="__APP_DIR__"', text)
        self.assertIn("find", text)  # the moved-folder search
        self.assertIn("exec", text)


class DesktopFileTests(unittest.TestCase):
    def _entries(self, name):
        entries = {}
        for line in read(name).splitlines():
            if "=" in line and not line.startswith("["):
                key, value = line.split("=", 1)
                entries[key] = value
        return entries

    def test_app_desktop_has_no_percent_k(self):
        entries = self._entries("Fyrn.desktop")
        self.assertNotIn("%k", entries["Exec"])
        self.assertNotIn("%k", entries.get("Icon", ""))

    def test_app_desktop_exec_uses_launcher_icon_uses_app_dir(self):
        # The icon starts Fyrn through the launcher (which finds
        # the folder again if it moved); the icon image still
        # comes from the app folder.
        entries = self._entries("Fyrn.desktop")
        self.assertEqual(entries["Exec"],
                         "__HOME__/.local/bin/fyrn")
        self.assertEqual(entries["Icon"],
                         "__APP_DIR__/assets/fyrn-48.png")

    def test_app_desktop_exec_has_no_nested_quotes(self):
        # GLib's desktop parser rejects nested escaped quotes in
        # Exec, so the whole file would fail to load.
        entries = self._entries("Fyrn.desktop")
        self.assertNotIn('\\"', entries["Exec"])

    def test_startup_wm_class_matches_app_name(self):
        entries = self._entries("Fyrn.desktop")
        gui = read("fyrn/gui.py")
        match = re.search(r'setApplicationName\("([^"]+)"\)', gui)
        self.assertIsNotNone(match)
        self.assertEqual(entries["StartupWMClass"], match.group(1))

    def test_install_desktop_uses_percent_k_as_token(self):
        # %k is its own argv token at the end of Exec (field-code
        # expansion keeps a spaced path as one argument); it must
        # never sit inside the quoted python code.
        entries = self._entries("Install-Fyrn.desktop")
        self.assertEqual(entries["Exec"].rsplit(None, 1)[-1], "%k")
        # The launcher sets the flag via a dict assignment in the
        # python one-liner (e['FYRN_GUI_INSTALL']='1'), so the
        # name is what to pin, not a NAME=1 literal.
        self.assertIn("FYRN_GUI_INSTALL", entries["Exec"])
        self.assertEqual(entries["Terminal"], "true")

    def test_desktop_files_are_applications(self):
        for name in ("Fyrn.desktop", "Install-Fyrn.desktop"):
            entries = self._entries(name)
            self.assertEqual(entries["Type"], "Application")
            self.assertTrue(entries["Name"])


class ProjectFileTests(unittest.TestCase):
    def test_requirements(self):
        text = read("requirements.txt")
        # Pinned: an unpinned install drifts with every release,
        # and a new llama-cpp-python can break the vision handler
        # or the load path without warning.
        self.assertIn("PySide6-Essentials==6.12.0", text)
        self.assertIn("llama-cpp-python==0.3.36", text)
        # llama-cpp-python has no PyPI wheels: without the CPU wheel
        # index + --only-binary, pip builds llama.cpp from source
        # (needs gcc + cmake + a long compile). Pin both lines so the
        # wheel install cannot silently regress to a source build.
        self.assertIn(
            "--extra-index-url "
            "https://abetlen.github.io/llama-cpp-python/whl/cpu",
            text)
        self.assertIn("--only-binary=llama-cpp-python", text)

    def test_license_is_mit_for_joshua(self):
        text = read("LICENSE")
        self.assertIn("MIT License", text)
        self.assertIn("Copyright (c) 2026 Joshua Lofland", text)

    def test_gitignore_covers_secrets(self):
        text = read(".gitignore")
        for pattern in (".env", "*.key", "*.pem", "providers.json"):
            self.assertIn(pattern, text)

    def test_readme_says_bash_install(self):
        self.assertIn("bash install.sh", read("README.md"))

    def test_stylesheet_applied_by_main(self):
        gui = read("fyrn/gui.py")
        theme = read("fyrn/theme.py")
        self.assertIn("DARK_STYLESHEET", gui)
        # main() applies the active theme's stylesheet (dark by
        # default, high-contrast when that setting is on).
        self.assertIn("app.setStyleSheet(current_stylesheet())", gui)
        # The colours live in the theme tokens, not in gui.py.
        self.assertIn("#14202e", theme)  # icon tile colour
        self.assertIn("#f2c14e", theme)  # ember accent
        self.assertNotIn("#14202e", gui)
        self.assertNotIn("#f2c14e", gui)

    def test_icon_files_exist(self):
        # A fresh clone holds only the SVG (the push path cannot
        # carry binary PNGs), so render them first when absent.
        # Render in a SEPARATE process: rendering inside the test
        # process crashed the suite in another project.
        pngs = ("fyrn-48.png", "fyrn-256.png", "fyrn.png")
        if any(not (ROOT / "assets" / name).is_file()
               for name in pngs):
            result = subprocess.run(
                [sys.executable, str(ROOT / "render_icons.py")],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0,
                             result.stdout + result.stderr)
        for name in ("fyrn.svg",) + pngs:
            path = ROOT / "assets" / name
            self.assertTrue(path.is_file(), name)
            self.assertGreater(path.stat().st_size, 100, name)


class ZipBuilderTests(unittest.TestCase):
    def test_build_zip_produces_correct_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "fyrn"
            shutil.copytree(
                ROOT, copy,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.zip", "*.pyc"))
            result = subprocess.run(
                [sys.executable, "build_zip.py"],
                cwd=str(copy), capture_output=True, text=True,
                timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout
                             + result.stderr)
            # The zip name follows the version (a trailing .0 patch
            # is dropped, so 0.1.0 ships as v0.1 and 0.1.1 as
            # v0.1.1); derive it, never hardcode it.
            sys.path.insert(0, str(copy))
            try:
                from fyrn import __version__
            finally:
                sys.path.remove(str(copy))
            parts = __version__.split(".")
            tag = (".".join(parts[:2]) if len(parts) == 3
                   and parts[2] == "0" else __version__)
            zip_path = copy / ("fyrn-v%s.zip" % tag)
            self.assertTrue(zip_path.is_file())
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                self.assertTrue(
                    all(name.startswith("fyrn/") for name in names))
                self.assertFalse(
                    [name for name in names
                     if "__pycache__" in name or name.endswith(".zip")])
                for info in zf.infolist():
                    mode = info.external_attr >> 16
                    base = os.path.basename(info.filename)
                    if base in ("install.sh", "run.sh",
                                "Fyrn.desktop",
                                "Install-Fyrn.desktop"):
                        self.assertEqual(
                            mode, 0o100755, info.filename)
                    else:
                        self.assertEqual(
                            mode, 0o100644, info.filename)
                for wanted in ("fyrn/install.sh",
                               "fyrn/run.sh",
                               "fyrn/Fyrn.desktop",
                               "fyrn/README.md",
                               "fyrn/fyrn/gui.py",
                               "fyrn/tests/test_gui.py",
                               "fyrn/assets/fyrn-48.png"):
                    self.assertIn(wanted, names)
                # The whole tree ships, minus the documented
                # exclusions: a new file forgotten from the
                # builder's hand-maintained list fails here, not
                # on the user's PC. Excluded: caches and build
                # artifacts (__pycache__, *.pyc, *.zip, the
                # checksum sidecar), the venv and git, and
                # CI-STATUS.md (the CI verdict file rides in
                # the push, not in the zip).
                expected = set()
                for dirpath, dirnames, filenames in os.walk(copy):
                    dirnames[:] = [
                        d for d in dirnames
                        if d not in ("__pycache__", ".venv", ".git")]
                    for name in filenames:
                        if name.endswith((".pyc", ".zip",
                                          ".sha256")):
                            continue
                        rel = os.path.relpath(
                            os.path.join(dirpath, name), copy)
                        if rel == "CI-STATUS.md":
                            continue
                        expected.add("fyrn/" + rel)
                self.assertEqual(set(names), expected)
            self.assertIn("modes ok: True", result.stdout)
            # The checksum sidecar (v0.6): the updater verifies a
            # release zip against it, so it ships alongside the
            # zip (never inside it), in sha256sum format, and
            # names the zip's real hash.
            sidecar_path = Path(str(zip_path) + ".sha256")
            self.assertTrue(sidecar_path.is_file())
            digest = hashlib.sha256()
            with open(zip_path, "rb") as fh:
                digest.update(fh.read())
            self.assertEqual(
                sidecar_path.read_text(encoding="utf-8"),
                "%s  %s\n" % (digest.hexdigest(), zip_path.name))


class DiskSpaceTests(unittest.TestCase):
    """The installer's disk-space check, with a fake df earlier
    in PATH than the real one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.copy = self.base / "fyrn"
        shutil.copytree(
            ROOT, self.copy,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.zip", "*.pyc"))

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_df(self, available_kb=None):
        # A df that reports the given free space (or nothing at
        # all, when available_kb is None).
        script = self.bin / "df"
        if available_kb is None:
            script.write_text("#!/bin/sh\nexit 1\n")
        else:
            script.write_text(
                "#!/bin/sh\n"
                "echo 'Filesystem 1024-blocks Used Available "
                "Capacity Mounted on'\n"
                "echo '/dev/fake 100000000 90000000 %d 90%% /tmp'\n"
                % available_kb)
        script.chmod(0o755)

    def _run_install(self):
        env = dict(os.environ)
        env["PATH"] = "%s:%s" % (self.bin, os.environ["PATH"])
        env["HOME"] = str(self.home)
        return subprocess.run(
            ["bash", "install.sh"], cwd=str(self.copy),
            capture_output=True, text=True, timeout=120, env=env)

    def test_tiny_free_space_refuses_to_install(self):
        self._fake_df(available_kb=100)
        result = self._run_install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Not enough free disk space",
                      result.stdout)
        self.assertFalse((self.copy / ".venv").exists())

    def test_unmeasurable_space_warns_and_continues(self):
        # df fails: the installer warns and carries on to the
        # python check (which fails here because the fake python3
        # below cannot import venv — the point is the install got
        # past the disk check instead of stopping on it).
        self._fake_df(available_kb=None)
        fake_python = self.bin / "python3"
        fake_python.write_text("#!/bin/sh\nexit 1\n")
        fake_python.chmod(0o755)
        result = self._run_install()
        self.assertIn("Could not check free disk space",
                      result.stdout)
        self.assertIn("python3-venv is missing", result.stdout)
        self.assertEqual(result.returncode, 1)


class LauncherTests(unittest.TestCase):
    """The self-healing launcher: a moved folder is found again,
    and a move it cannot resolve says so plainly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        (self.home / ".local" / "bin").mkdir(parents=True)
        (self.home / ".local" / "share" / "applications").mkdir(
            parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _install_dir(self, *parts):
        """A fake Fyrn install: install.sh, the fyrn package, and
        a stub venv python that prints a marker when run."""
        folder = self.home.joinpath(*parts)
        folder.mkdir(parents=True)
        (folder / "install.sh").write_text("#!/usr/bin/env bash\n")
        (folder / "fyrn").mkdir()
        (folder / "fyrn" / "__init__.py").write_text(
            '__version__ = "0.4"\n')
        venv_bin = folder / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        stub = venv_bin / "python"
        stub.write_text(
            "#!/bin/sh\necho FYRN-STARTED-FROM %s\n"
            % folder)
        stub.chmod(0o755)
        return folder

    def _install_launcher(self, app_dir):
        launcher = self.home / ".local" / "bin" / "fyrn"
        text = read("launcher.sh").replace("__APP_DIR__",
                                           str(app_dir))
        launcher.write_text(text)
        launcher.chmod(0o755)
        return launcher

    def _desktop_file(self, app_dir):
        desktop = (self.home / ".local" / "share" / "applications"
                   / "Fyrn.desktop")
        desktop.write_text(
            "[Desktop Entry]\nType=Application\nName=Fyrn\n"
            "Exec=%s/.local/bin/fyrn\n"
            "Icon=%s/assets/fyrn-48.png\n"
            % (self.home, app_dir))
        return desktop

    def _run_launcher(self, launcher):
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        return subprocess.run(
            [str(launcher)], capture_output=True, text=True,
            timeout=60, env=env)

    def test_fast_path_starts_fyrn_unchanged(self):
        folder = self._install_dir("fyrn")
        launcher = self._install_launcher(folder)
        before = launcher.read_bytes()
        result = self._run_launcher(launcher)
        self.assertEqual(result.returncode, 0)
        self.assertIn("FYRN-STARTED-FROM %s" % folder,
                      result.stdout)
        self.assertEqual(launcher.read_bytes(), before)

    def test_moved_folder_is_found_and_healed(self):
        folder = self._install_dir("moved", "fyrn")
        old = self.home / "old" / "fyrn"
        launcher = self._install_launcher(old)
        desktop = self._desktop_file(old)
        result = self._run_launcher(launcher)
        self.assertEqual(result.returncode, 0)
        self.assertIn("FYRN-STARTED-FROM %s" % folder,
                      result.stdout)
        # The launcher rewrote itself to the new folder, and the
        # desktop icon points there too.
        self.assertIn('APP_DIR="%s"' % folder,
                      launcher.read_text())
        self.assertIn("Icon=%s/assets/fyrn-48.png" % folder,
                      desktop.read_text())

    def test_two_folders_is_an_error_not_a_guess(self):
        self._install_dir("one", "fyrn")
        self._install_dir("two", "fyrn")
        launcher = self._install_launcher(self.home / "old" / "fyrn")
        result = self._run_launcher(launcher)
        self.assertEqual(result.returncode, 1)
        self.assertIn("more than one Fyrn folder", result.stderr)

    def test_no_folder_is_a_plain_error(self):
        launcher = self._install_launcher(self.home / "old" / "fyrn")
        result = self._run_launcher(launcher)
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not find its new folder",
                      result.stderr)
        self.assertIn("bash install.sh", result.stderr)


if __name__ == "__main__":
    unittest.main()
