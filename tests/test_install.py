"""Static checks on the installer, launchers, desktop files, and
zip builder. No venv is created here; the install simulation is a
separate verification step (see the build report)."""

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
        self.assertIn(".local/share/applications", text)

    def test_run_sh_launches_from_venv(self):
        text = read("run.sh")
        self.assertIn(".venv/bin/python -m fyrn", text)
        self.assertIn("exec", text)

    def test_launcher_sets_pythonpath(self):
        # `fyrn` must work from any folder: without PYTHONPATH,
        # `python -m fyrn` only finds the package when the terminal
        # happens to be in the app folder (caught by the install
        # sim: "No module named fyrn" from $HOME).
        text = read("install.sh")
        self.assertIn("PYTHONPATH=", text)
        self.assertIn(".local/bin/fyrn", text)


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

    def test_app_desktop_exec_and_icon_use_app_dir(self):
        entries = self._entries("Fyrn.desktop")
        self.assertIn("__APP_DIR__", entries["Exec"])
        self.assertEqual(entries["Icon"],
                         "__APP_DIR__/assets/fyrn-48.png")
        self.assertIn("python3 -c", entries["Exec"])
        self.assertIn("run.sh", entries["Exec"])

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
        self.assertIn("PySide6-Essentials", text)
        self.assertIn("llama-cpp-python", text)
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
        self.assertIn("DARK_STYLESHEET", gui)
        self.assertIn("app.setStyleSheet(DARK_STYLESHEET)", gui)
        self.assertIn("#14202e", gui)  # icon tile colour
        self.assertIn("#f2c14e", gui)  # ember accent

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
            self.assertIn("modes ok: True", result.stdout)


if __name__ == "__main__":
    unittest.main()
