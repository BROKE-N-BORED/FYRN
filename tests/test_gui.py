"""GUI tests for Fyrn, run offscreen with a fake runner and
fake .gguf files. No real model, no network (pulls use a local
fake HTTP server)."""

import hashlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
import unittest
import warnings
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from PySide6.QtCore import (QAbstractAnimation, QBuffer, QEvent,
                             QIODevice, QMimeData, QPoint, QRect,
                             QTimer, Qt, QUrl)
from PySide6.QtGui import (QColor, QDragEnterEvent, QDropEvent, QFont,
                           QShortcut, QWheelEvent)
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QSystemTrayIcon,
                               QTextEdit, QToolTip, QWidget)

from helpers import ModalWatcher, file_bytes, make_fake_gguf
from helpers import start_range_server, write_gguf_file
from fyrn.core import (HEADROOM_BYTES, ImportCancelled, fit_status,
                       format_chat_export, read_ram_bytes)
from fyrn.gui import (DARK_STYLESHEET, FIRST_PULL_SUGGESTION,
                            FIT_BADGES, HIGH_CONTRAST_STYLESHEET,
                            HOW_TO_USE_SECTIONS, ChatInput,
                            CommandPalette, HowToUseWindow, MainWindow,
                            ModelCard, PressDipButton, PullWindow,
                            PullWorker, RaiseListener, SettingsWindow,
                            SingleInstance, SkeletonCard, TURN_DIVIDER,
                            current_tokens, get_chime_muted,
                            get_compact_list, get_high_contrast,
                            get_models_folder, get_reduce_motion,
                            get_show_ollama_models, get_update_check,
                            grid_columns, reduced_motion,
                            set_chime_muted, set_compact_list,
                            set_high_contrast, set_models_folder,
                            set_reduce_motion, set_show_ollama_models,
                            set_update_check)
from fyrn.pull import PullCancelled, parse_model_input
from fyrn.runner import FakeRunner, LlamaRunner, RunnerError
from fyrn.theme import DARK, HIGH_CONTRAST
from fyrn.updates import pending_update

GB = 1024 ** 3
PAYLOAD = bytes(range(256)) * 256  # 64 KiB deterministic bytes

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance()
    if _app is None:
        _app = QApplication([])
    # The real look: main() applies the dark stylesheet, so the
    # suite does the same — otherwise the tests would verify a bare
    # default Qt look the user never sees.
    _app.setStyleSheet(DARK_STYLESHEET)


def pump_until(cond, timeout_ms=5000, step=50):
    """Pump the event loop until cond() is true or time runs out."""
    waited = 0
    while waited < timeout_ms:
        if cond():
            return True
        QTest.qWait(step)
        waited += step
    return cond()


def make_sparse_gguf(path, size):
    """A .gguf file of size bytes that takes almost no disk (sparse)."""
    with open(path, "wb") as fh:
        fh.truncate(size)
    return path


class IndeterminateRunner(FakeRunner):
    """A runner that reports no percent while loading, like the
    real backend: -1 with stage text, a pause, then 100."""

    def load(self, path, progress_cb=None, cancel_event=None,
             display_name=None):
        if progress_cb is not None:
            progress_cb(-1, "Loading into RAM...")
        if self.load_delay_s > 0:
            import time as _time
            _time.sleep(self.load_delay_s)
        self.model_path = str(path)
        if progress_cb is not None:
            progress_cb(100, "Loaded")
        return {"load_time_s": self.load_delay_s}


def visible_texts(window):
    """Every piece of text the user can see in a window."""
    texts = [window.windowTitle()]
    for widget in window.findChildren(QWidget):
        if isinstance(widget, QPushButton):
            texts.append(widget.text())
        elif isinstance(widget, QLabel):
            texts.append(widget.text())
        elif isinstance(widget, QLineEdit):
            texts.append(widget.text())
            texts.append(widget.placeholderText())
        elif isinstance(widget, QTextEdit):
            texts.append(widget.toPlainText())
            texts.append(widget.placeholderText())
    for action in window.findChildren(
            getattr(__import__("PySide6.QtGui", fromlist=["QAction"]),
                    "QAction")):
        texts.append(action.text())
    return texts


class GuiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()  # QLockFile needs the folder to exist
        self.models = self.base / "models"
        self.models.mkdir()
        self.settings_file = self.base / "settings.ini"
        self._old_home = os.environ.get("FYRN_HOME")
        self._old_settings = os.environ.get("FYRN_SETTINGS_FILE")
        self._old_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["FYRN_HOME"] = str(self.home)
        os.environ["FYRN_SETTINGS_FILE"] = str(self.settings_file)
        # The trash lives in the temp dir too: a remove test must
        # never touch the real ~/.local/share/Trash.
        self.data_home = self.base / "data"
        os.environ["XDG_DATA_HOME"] = str(self.data_home)
        set_models_folder(self.models)
        # Discovery is off by default in tests: the sandbox has a
        # real Ollama store, and its models would appear in every
        # test window. The discovery tests turn it on explicitly.
        set_show_ollama_models(False)
        self.watcher = ModalWatcher()
        self.windows = []
        self.servers = []

    def tearDown(self):
        # Stop the watcher FIRST: if a window close raises, the
        # watcher must not leak into the next test, or it steals
        # that test's dialogs (and the failure shows up far from
        # the cause).
        self.watcher.stop()
        for window in self.windows:
            try:
                window.close()
            except RuntimeError:
                pass  # already deleted (geometry test closes early)
            try:
                window.deleteLater()
            except RuntimeError:
                pass
        QTest.qWait(50)
        for server in self.servers:
            server.shutdown()
            server.server_close()
        if self._old_home is None:
            os.environ.pop("FYRN_HOME", None)
        else:
            os.environ["FYRN_HOME"] = self._old_home
        if self._old_settings is None:
            os.environ.pop("FYRN_SETTINGS_FILE", None)
        else:
            os.environ["FYRN_SETTINGS_FILE"] = self._old_settings
        if self._old_xdg is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self._old_xdg
        self.tmp.cleanup()

    def make_window(self, runner=None):
        window = MainWindow(runner=runner or FakeRunner())
        self.windows.append(window)
        window.show()
        return window

    def wait_scan(self, window):
        ok = pump_until(lambda: window._scan_worker is None, 5000)
        QTest.qWait(50)
        self.assertTrue(ok, "model list scan did not finish")
        return ok

    def wait_load(self, window):
        ok = pump_until(lambda: window.load_worker is None, 5000)
        QTest.qWait(50)
        self.assertTrue(ok, "model load did not finish")
        return ok

    def wait_chat(self, window):
        ok = pump_until(lambda: window.chat_worker is None, 5000)
        QTest.qWait(50)
        self.assertTrue(ok, "chat reply did not finish")
        return ok

    def wait_import(self, window):
        ok = pump_until(lambda: window.import_worker is None, 5000)
        QTest.qWait(50)
        self.assertTrue(ok, "import did not finish")
        return ok

    def _answer_yes(self):
        """A timer that clicks Yes on the next question dialog
        (the dialog is modal, so the click that opens it blocks
        until something answers it). Returns the timer; the
        caller stops it and re-enables the watcher."""
        self.watcher.enabled = False
        timer = QTimer()
        timer.setInterval(100)

        def answer():
            for widget in QApplication.topLevelWidgets():
                if isinstance(widget, QMessageBox) and widget.isVisible():
                    button = widget.button(
                        QMessageBox.StandardButton.Yes)
                    if button is not None:
                        timer.stop()
                        button.click()

        timer.timeout.connect(answer)
        timer.start()
        return timer

    def load_first_model(self, window):
        """Select the first card and load it; assert it loaded.

        The RAM check is pinned to 8 GB here: these tests cover the
        load/chat/unload FLOW, and ambient free RAM in the sandbox
        swings wildly (other suites running). The RAM check itself
        is unit-tested with exact boundaries in test_core.py, and
        the refuse/warn dialog paths have their own tests below
        that use real RAM.
        """
        self.wait_scan(window)
        self.assertTrue(window.model_cards)
        window.controller._free_ram_fn = lambda: 8 * GB
        path = sorted(window.model_cards)[0]
        window.model_cards[path].clicked.emit(path)
        window.load_button.click()
        self.wait_load(window)
        self.assertTrue(window.controller.is_loaded)
        return path


class MainWindowTests(GuiTestCase):
    def test_window_opens_default_size_with_two_tabs(self):
        window = self.make_window()
        self.assertEqual(window.width(), 1100)
        self.assertEqual(window.height(), 700)
        self.assertEqual(window.tabs.count(), 2)
        self.assertEqual(window.tabs.tabText(0), "Models")
        self.assertEqual(window.tabs.tabText(1), "Chat")
        self.assertEqual(window.windowTitle(), "Fyrn")

    def test_menus_exist(self):
        window = self.make_window()
        bar = window.menuBar()
        titles = [action.text() for action in bar.actions()]
        self.assertEqual(titles, ["File", "Models", "Help"])
        menus = {action.text(): action.menu() for action in bar.actions()}
        file_items = [a.text() for a in menus["File"].actions()
                      if not a.isSeparator()]
        self.assertEqual(file_items,
                         ["Settings", "Save chat", "Debug report",
                          "Quit"])
        models_items = [a.text() for a in menus["Models"].actions()]
        self.assertEqual(models_items,
                         ["Load selected model", "Pull models",
                          "Import a GGUF file", "Remove model",
                          "Refresh list", "Unload model"])
        help_items = [a.text() for a in menus["Help"].actions()]
        self.assertEqual(help_items,
                         ["Command palette", "How to Use",
                          "Check for updates", "About"])

    def test_cards_populate_and_nothing_selected(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        make_fake_gguf(self.models / "beta.gguf", 4096)
        window = self.make_window()
        self.wait_scan(window)
        self.assertEqual(len(window.model_cards), 2)
        self.assertIsNone(window._selected_path)
        for card in window.model_cards.values():
            self.assertFalse(card._selected)
            # Tiles pack to the top at their natural height; they
            # must not stretch to fill the tab (they did before the
            # expanding spacer row was added).
            self.assertLess(card.height(), 200)
        self.assertFalse(window.load_button.isEnabled())

    def test_skeleton_shown_while_scanning(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        # Right after construction the scan is still running and
        # skeleton tiles stand in for the list.
        skeletons = window.findChildren(SkeletonCard)
        self.assertTrue(skeletons)
        self.wait_scan(window)
        self.assertEqual(window.findChildren(SkeletonCard), [])
        self.assertEqual(len(window.model_cards), 1)

    def test_fit_badges(self):
        # The scan reads free RAM when it runs, so the test pins
        # the reading: without the pin, free RAM can cross the
        # fits/tight boundary between the test's read and the
        # scan's, and the badge disagrees with the expectation
        # for no product reason (a flake seen under sandbox load,
        # 2026-11-16). A 2 KB file is "Fits" at 4 GB free; the
        # huge file (real free + 4 GB) is always "Too big" there.
        make_fake_gguf(self.models / "small.gguf", 2048)
        _total, free = read_ram_bytes()
        make_sparse_gguf(self.models / "huge.gguf", free + 4 * GB)
        pinned = (8 * GB, 4 * GB)
        with mock.patch("fyrn.gui.read_ram_bytes",
                        return_value=pinned):
            window = self.make_window()
            self.wait_scan(window)
        small = window.model_cards[str(self.models / "small.gguf")]
        huge = window.model_cards[str(self.models / "huge.gguf")]
        self.assertEqual(
            small.findChild(QLabel, "fit_badge").text(),
            FIT_BADGES[fit_status(2048, pinned[1])][0])
        self.assertEqual(
            huge.findChild(QLabel, "fit_badge").text(), "Too big")

    def test_click_selects_card(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        card = window.model_cards[str(self.models / "alpha.gguf")]
        QTest.mouseClick(card, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, card.rect().center())
        QTest.qWait(50)
        self.assertEqual(window._selected_path, card.path)
        self.assertTrue(card._selected)
        self.assertTrue(window.load_button.isEnabled())

    def test_skipped_files_are_named(self):
        (self.models / "weird.gguf").mkdir()
        window = self.make_window()
        self.wait_scan(window)
        self.assertIn("weird.gguf", window.skipped_label.text())
        self.assertTrue(window.skipped_label.isVisible())

    def test_load_and_chat_round_trip(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        runner = FakeRunner(reply_text="Hello there friend")
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        self.assertEqual(window.tabs.currentIndex(), 1)  # Chat tab
        self.assertIn("Loaded: alpha.gguf", window.stats_label.text())
        self.assertIn("Load time:", window.stats_label.text())
        window.chat_input.setText("hi there")
        window.send_button.click()
        self.wait_chat(window)
        text = window.transcript.toPlainText()
        self.assertIn("You: hi there", text)
        self.assertIn("alpha.gguf: Hello there friend", text)
        stats = window.stats_label.text()
        self.assertIn("tokens per second", stats)
        self.assertIn("First token:", stats)
        self.assertIn("Reply: 3 tokens", stats)

    def test_stop_reply(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        runner = FakeRunner(
            reply_text="one two three four five six seven eight",
            token_delay_s=0.2)
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        window.chat_input.setText("go")
        window.send_button.click()
        QTest.qWait(300)
        window.stop_button.click()
        self.wait_chat(window)
        self.assertIn("(Reply stopped.)",
                      window.transcript.toPlainText())

    def test_send_without_model_stays_greyed_out(self):
        window = self.make_window()
        window.tabs.setCurrentIndex(1)
        self.assertFalse(window.send_button.isEnabled())
        window.chat_input.setText("hello")
        window.send_button.click()  # disabled: does nothing
        QTest.qWait(100)
        self.assertIsNone(window.chat_worker)
        self.assertEqual(window.transcript.toPlainText(), "")

    def test_load_second_auto_unloads_first(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        make_fake_gguf(self.models / "beta.gguf", 2048)
        window = self.make_window()
        self.load_first_model(window)
        self.assertEqual(window.controller.current_name, "alpha.gguf")
        # Loading rebuilds the list (the loaded model becomes the
        # hero tile), so wait for the rebuilt cards before touching
        # beta: the refresh clears the old cards first, and reading
        # model_cards mid-refresh is a race.
        self.wait_scan(window)
        beta = str(self.models / "beta.gguf")
        window.model_cards[beta].clicked.emit(beta)
        window.load_button.click()
        self.wait_load(window)
        self.assertEqual(window.controller.current_name, "beta.gguf")
        # The loaded model is the hero tile, first in the list.
        self.wait_scan(window)
        hero = window.model_cards[beta]
        line = hero.fontMetrics().lineSpacing()
        self.assertEqual(hero.minimumHeight(), 4 * line + 32)

    def test_refuse_too_big_model(self):
        _total, free = read_ram_bytes()
        make_sparse_gguf(self.models / "huge.gguf", free + 4 * GB)
        window = self.make_window()
        self.wait_scan(window)
        huge = str(self.models / "huge.gguf")
        window.model_cards[huge].clicked.emit(huge)
        window.load_button.click()
        QTest.qWait(300)
        self.assertFalse(window.controller.is_loaded)
        self.assertTrue(
            any("will not fit" in text
                for _title, text in self.watcher.seen),
            "no plain-words refuse dialog appeared")

    def test_warn_tight_default_is_no(self):
        # Tight band is 3/4 to all of free RAM; aim at 7/8 so a
        # small RAM swing either way stays in the band.
        _total, free = read_ram_bytes()
        size = free * 7 // 8 - HEADROOM_BYTES
        if size <= 0:
            self.skipTest("not enough free RAM to build a tight "
                          "fixture")
        make_sparse_gguf(self.models / "tight.gguf", size)
        window = self.make_window()
        self.wait_scan(window)
        tight = str(self.models / "tight.gguf")
        window.model_cards[tight].clicked.emit(tight)
        window.load_button.click()
        QTest.qWait(300)
        # The watcher clicks the default button (No).
        self.assertFalse(window.controller.is_loaded)
        self.assertTrue(
            any("Load it anyway" in text
                for _title, text in self.watcher.seen),
            "no tight-RAM question dialog appeared")

    def test_warn_tight_yes_loads(self):
        # Tight band is 3/4 to all of free RAM; aim at 7/8 so a
        # small RAM swing either way stays in the band.
        _total, free = read_ram_bytes()
        size = free * 7 // 8 - HEADROOM_BYTES
        if size <= 0:
            self.skipTest("not enough free RAM to build a tight "
                          "fixture")
        make_sparse_gguf(self.models / "tight.gguf", size)
        window = self.make_window()
        self.wait_scan(window)
        tight = str(self.models / "tight.gguf")
        window.model_cards[tight].clicked.emit(tight)
        # The question dialog is modal, so the click blocks until
        # something answers it: a timer that clicks Yes by role.
        self.watcher.enabled = False
        timer = QTimer()
        timer.setInterval(100)

        def answer_yes():
            for widget in QApplication.topLevelWidgets():
                if isinstance(widget, QMessageBox) and widget.isVisible():
                    button = widget.button(
                        QMessageBox.StandardButton.Yes)
                    if button is not None:
                        timer.stop()
                        button.click()

        timer.timeout.connect(answer_yes)
        timer.start()
        try:
            window.load_button.click()
        finally:
            timer.stop()
            self.watcher.enabled = True
        self.wait_load(window)
        self.assertTrue(window.controller.is_loaded)
        self.assertIn("tight", window.statusBar().currentMessage())

    def test_unload(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.load_first_model(window)
        window.unload_button.click()
        QTest.qWait(100)
        self.assertFalse(window.controller.is_loaded)
        self.assertIn("No model loaded.", window.stats_label.text())

    def test_indeterminate_load_bar(self):
        # A runner that reports -1 (no percent to give) shows an
        # indeterminate bar with stage text, never a fake percent.
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        runner = IndeterminateRunner(load_delay_s=0.5)
        window = self.make_window(runner=runner)
        window.controller._free_ram_fn = lambda: 8 * GB
        self.wait_scan(window)
        path = str(self.models / "alpha.gguf")
        window.model_cards[path].clicked.emit(path)
        window.load_button.click()
        ok = pump_until(lambda: window.load_progress.maximum() == 0
                        and window.load_progress.isVisible(), 5000)
        self.assertTrue(ok, "load bar never went indeterminate")
        self.assertIn("Loading", window.statusBar().currentMessage())
        self.wait_load(window)
        self.assertTrue(window.controller.is_loaded)

    def test_chat_placeholder_switches_on_load_and_unload(self):
        # The empty transcript must not say "Load a model first"
        # once a model IS loaded (rename-time nit, found in
        # independent verification): the hint follows the state.
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.assertEqual(window.transcript.placeholderText(),
                         "Load a model first, then chat here.")
        self.load_first_model(window)
        self.assertNotIn("Load a model first",
                         window.transcript.placeholderText())
        self.assertEqual(window.transcript.placeholderText(),
                         "Type a message below.")
        window.unload_button.click()
        QTest.qWait(100)
        self.assertEqual(window.transcript.placeholderText(),
                         "Load a model first, then chat here.")

    def test_import_copies_file_into_folder(self):
        elsewhere = self.base / "elsewhere.gguf"
        make_fake_gguf(elsewhere, 1024)
        window = self.make_window()
        self.wait_scan(window)
        with mock.patch(
                "fyrn.gui.QFileDialog.getOpenFileName",
                return_value=(str(elsewhere), "")):
            window.import_button.click()
        self.wait_import(window)
        self.wait_scan(window)
        self.assertIn(str(self.models / "elsewhere.gguf"),
                      window.model_cards)
        self.assertTrue((self.models / "elsewhere.gguf").is_file())

    def test_reply_carries_model_name_not_generic_model(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        runner = FakeRunner(reply_text="Hello there friend")
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        window.chat_input.setText("hi there")
        window.send_button.click()
        self.wait_chat(window)
        text = window.transcript.toPlainText()
        self.assertIn("alpha.gguf: Hello there friend", text)
        self.assertNotIn("Model: ", text)

    def test_turns_separated_by_divider(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        runner = FakeRunner(reply_text="ok")
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        for prompt in ("first", "second"):
            window.chat_input.setText(prompt)
            window.send_button.click()
            self.wait_chat(window)
        text = window.transcript.toPlainText()
        self.assertEqual(text.count(TURN_DIVIDER), 2)
        first = text.index("You: first")
        divider = text.index(TURN_DIVIDER)
        second = text.index("You: second")
        self.assertLess(first, divider)
        self.assertLess(divider, second)

    def test_model_switch_attributes_each_reply(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        make_fake_gguf(self.models / "beta.gguf", 2048)
        runner = FakeRunner(reply_text="ok")
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        window.chat_input.setText("one")
        window.send_button.click()
        self.wait_chat(window)
        self.wait_scan(window)
        beta = str(self.models / "beta.gguf")
        window.model_cards[beta].clicked.emit(beta)
        window.load_button.click()
        self.wait_load(window)
        self.assertEqual(window.controller.current_name, "beta.gguf")
        window.chat_input.setText("two")
        window.send_button.click()
        self.wait_chat(window)
        text = window.transcript.toPlainText()
        self.assertIn("alpha.gguf: ok", text)
        self.assertIn("beta.gguf: ok", text)

    def test_import_first_model_keeps_confirmation(self):
        # Importing the first model into an empty folder: the
        # confirmation must survive the scan that follows it (the
        # scan's own status update used to swallow it).
        elsewhere = self.base / "elsewhere.gguf"
        make_fake_gguf(elsewhere, 1024)
        window = self.make_window()
        self.wait_scan(window)
        self.assertTrue(window._empty_state.isVisible())
        with mock.patch(
                "fyrn.gui.QFileDialog.getOpenFileName",
                return_value=(str(elsewhere), "")):
            window.import_button.click()
        self.wait_import(window)
        self.wait_scan(window)
        self.assertIn("is now in your models folder.",
                      window.statusBar().currentMessage())

    def test_import_cancelled_does_nothing(self):
        window = self.make_window()
        self.wait_scan(window)
        with mock.patch(
                "fyrn.gui.QFileDialog.getOpenFileName",
                return_value=("", "")):
            window.import_button.click()
        QTest.qWait(100)
        self.assertEqual(window.model_cards, {})

    def test_geometry_memory(self):
        window = self.make_window()
        window.setGeometry(50, 60, 1000, 650)
        window.close()
        window.deleteLater()
        self.windows.remove(window)  # tearDown must not close it twice
        QTest.qWait(100)
        second = MainWindow(runner=FakeRunner())
        self.windows.append(second)
        self.assertEqual(second.geometry(), QRect(50, 60, 1000, 650))

    def test_debug_report_written_and_scrubbed(self):
        window = self.make_window()
        window.log.set_setting("api_key", "sk-live-secret-value-999")
        window.make_debug_report()
        QTest.qWait(300)
        reports = list((self.home / "logs").glob("debug-report-*.txt"))
        self.assertEqual(len(reports), 1)
        text = reports[0].read_text(encoding="utf-8")
        self.assertIn("DEBUG REPORT", text)
        self.assertNotIn("sk-live-secret-value-999", text)
        self.assertIn("[REDACTED]", text)

    def test_no_unexpected_dialogs_on_plain_open(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        self.assertEqual(self.watcher.seen, [])

    def test_double_click_documented_in_ui_and_how_to_use(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        texts = " ".join(visible_texts(window))
        self.assertIn("Double-click a model to load it.", texts)
        self.assertIn("Double-click a model to load it straight "
                      "away.",
                      HOW_TO_USE_SECTIONS["Models"])

    def test_dark_stylesheet_applied(self):
        self.assertIn("#14202e", _app.styleSheet())
        self.assertIn("#f2c14e", _app.styleSheet())
        self.assertIn("#e8890c", _app.styleSheet())
        window = self.make_window()
        self.assertEqual(window.styleSheet(), "")


class VisibleTextTests(GuiTestCase):
    def test_no_em_dash_and_no_bare_ampersand(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        window.show_pull_window()
        window.show_settings()
        window.show_how_to_use()
        self.windows.extend([window.pull_window,
                             window.settings_window,
                             window.howto_window])
        QTest.qWait(200)
        bad_dash = []
        bad_amp = []
        for target in (window, window.pull_window,
                       window.settings_window, window.howto_window):
            for text in visible_texts(target):
                if "—" in text:
                    bad_dash.append(text)
                if "&" in text:
                    bad_amp.append(text)
        # The How to Use body text is visible text too.
        for title in HOW_TO_USE_SECTIONS:
            body = HOW_TO_USE_SECTIONS[title]
            if "—" in body:
                bad_dash.append(title)
            if "&" in body:
                bad_amp.append(title)
        self.assertEqual(bad_dash, [])
        self.assertEqual(bad_amp, [])


class EmptyStateTests(GuiTestCase):
    def _empty_texts(self, window):
        return [label.text()
                for label in window._empty_state.findChildren(QLabel)]

    def test_empty_folder_shows_empty_state(self):
        window = self.make_window()
        self.wait_scan(window)
        self.assertTrue(window._empty_state.isVisible())
        self.assertFalse(window._cards_scroll.isVisible())
        texts = " ".join(self._empty_texts(window))
        self.assertIn("No models yet", texts)
        self.assertIn("Pull a model", texts)
        self.assertIn("press Load", texts)
        self.assertIn("No models yet",
                      window.statusBar().currentMessage())
        buttons = [button.text() for button in
                   window._empty_state.findChildren(QPushButton)]
        self.assertIn("Pull", buttons)
        self.assertIn("Import", buttons)

    def test_empty_state_goes_away_when_model_appears(self):
        window = self.make_window()
        self.wait_scan(window)
        self.assertTrue(window._empty_state.isVisible())
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window._refresh_model_list()
        self.wait_scan(window)
        self.assertFalse(window._empty_state.isVisible())
        self.assertTrue(window._cards_scroll.isVisible())
        self.assertIn(str(self.models / "alpha.gguf"),
                      window.model_cards)
        self.assertIn("Pick a model and press Load",
                      window.statusBar().currentMessage())

    def test_empty_state_pull_button_opens_pull_window(self):
        window = self.make_window()
        self.wait_scan(window)
        buttons = {button.text(): button for button in
                   window._empty_state.findChildren(QPushButton)}
        buttons["Pull"].click()
        QTest.qWait(200)
        self.assertIsNotNone(window.pull_window)
        self.assertTrue(window.pull_window.isVisible())
        self.windows.append(window.pull_window)


class StatusTimeoutTests(GuiTestCase):
    def test_transient_message_falls_back_to_default(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        window._show_status("Reply done.", timeout_ms=50)
        self.assertEqual(window.statusBar().currentMessage(),
                         "Reply done.")
        default = ("Pick a model and press Load, or pull one "
                   "from the Models menu.")
        # A fixed wait after a 50 ms timeout is a race under load:
        # the event loop may not have run the timeout yet. Poll
        # until the default line is back instead.
        back = pump_until(
            lambda: window.statusBar().currentMessage() == default,
            2000)
        self.assertTrue(back, "status never fell back to the default")
        self.assertEqual(window.statusBar().currentMessage(), default)

    def test_persistent_message_is_not_clobbered(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        window._show_status("Reply done.", timeout_ms=50)
        window._show_status_persistent("Loading alpha.gguf...")
        QTest.qWait(200)
        self.assertEqual(window.statusBar().currentMessage(),
                         "Loading alpha.gguf...")

    def test_fallback_is_empty_state_line_when_folder_empty(self):
        window = self.make_window()
        self.wait_scan(window)
        self.assertTrue(window._empty_state.isVisible())
        window._show_status("Something happened.", timeout_ms=50)
        QTest.qWait(200)
        self.assertEqual(window.statusBar().currentMessage(),
                         "No models yet. Pull one or import one to "
                         "get started.")


class SelectionTests(GuiTestCase):
    def test_refresh_keeps_selection(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        make_fake_gguf(self.models / "beta.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        beta = str(self.models / "beta.gguf")
        window.model_cards[beta].clicked.emit(beta)
        self.assertEqual(window._selected_path, beta)
        window._refresh_model_list()
        self.wait_scan(window)
        self.assertEqual(window._selected_path, beta)
        self.assertTrue(window.model_cards[beta]._selected)
        self.assertTrue(window.load_button.isEnabled())

    def test_refresh_clears_selection_when_file_gone(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        alpha = str(self.models / "alpha.gguf")
        window.model_cards[alpha].clicked.emit(alpha)
        self.assertEqual(window._selected_path, alpha)
        (self.models / "alpha.gguf").unlink()
        window._refresh_model_list()
        self.wait_scan(window)
        self.assertIsNone(window._selected_path)
        self.assertFalse(window.load_button.isEnabled())

    def test_import_selects_new_model(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        elsewhere = self.base / "elsewhere.gguf"
        make_fake_gguf(elsewhere, 1024)
        window = self.make_window()
        self.wait_scan(window)
        with mock.patch(
                "fyrn.gui.QFileDialog.getOpenFileName",
                return_value=(str(elsewhere), "")):
            window.import_button.click()
        self.wait_import(window)
        self.wait_scan(window)
        self.assertEqual(
            window._selected_path,
            str(self.models / "elsewhere.gguf"))

    def test_pull_selects_new_model(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        make_fake_gguf(self.models / "pulled.gguf", 2048)
        window._on_model_pulled(str(self.models / "pulled.gguf"))
        self.wait_scan(window)
        self.assertEqual(
            window._selected_path,
            str(self.models / "pulled.gguf"))


class BentoTests(GuiTestCase):
    def _positions(self, window):
        """Map each card to its (row, col, rowspan, colspan)."""
        layout = window._cards_layout
        found = {}
        for index in range(layout.count()):
            widget = layout.itemAt(index).widget()
            if widget is None or widget not in window.model_cards.values():
                continue
            found[widget.path] = layout.getItemPosition(index)
        return found

    def test_grid_is_three_columns_16px_gaps(self):
        for name in ("a.gguf", "b.gguf", "c.gguf", "d.gguf"):
            make_fake_gguf(self.models / name, 2048)
        window = self.make_window()
        self.wait_scan(window)
        self.assertEqual(window._cards_layout.spacing(), 16)
        positions = self._positions(window)
        self.assertEqual(len(positions), 4)
        self.assertEqual(
            positions[str(self.models / "a.gguf")], (0, 0, 1, 1))
        self.assertEqual(
            positions[str(self.models / "b.gguf")], (0, 1, 1, 1))
        self.assertEqual(
            positions[str(self.models / "c.gguf")], (0, 2, 1, 1))
        self.assertEqual(
            positions[str(self.models / "d.gguf")], (1, 0, 1, 1))

    def test_cards_are_content_height(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        card = window.model_cards[str(self.models / "alpha.gguf")]
        # Heights come from the font's line height, not fixed
        # pixels: a plain card holds four lines at minimum, five
        # at most, plus margins.
        line = card.fontMetrics().lineSpacing()
        self.assertEqual(card.minimumHeight(), 4 * line + 32)
        self.assertEqual(card.maximumHeight(), 5 * line + 32)
        self.assertGreaterEqual(card.height(), card.minimumHeight())
        self.assertLessEqual(card.height(), card.maximumHeight())

    def test_hero_spans_all_columns(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        make_fake_gguf(self.models / "beta.gguf", 2048)
        window = self.make_window()
        self.load_first_model(window)
        self.wait_scan(window)
        positions = self._positions(window)
        self.assertEqual(
            positions[str(self.models / "alpha.gguf")], (0, 0, 1, 3))
        self.assertEqual(
            positions[str(self.models / "beta.gguf")], (1, 0, 1, 1))

    def test_hero_shows_live_stats(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        runner = FakeRunner(reply_text="Hello there friend")
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        self.wait_scan(window)
        hero = window._hero_card
        self.assertIsNotNone(hero)
        self.assertIn("Load time:",
                      hero.stats_label.text())
        window.chat_input.setText("hi there")
        window.send_button.click()
        self.wait_chat(window)
        self.wait_scan(window)
        self.assertIn("tokens per second",
                      window._hero_card.stats_label.text())
        self.assertIn("Reply: 3 tokens",
                      window._hero_card.stats_label.text())


class HoverDetailTests(GuiTestCase):
    def test_card_tooltip_shows_path_ram_fit(self):
        model = {"name": "alpha.gguf",
                 "path": self.models / "alpha.gguf",
                 "size": 2048}
        card = ModelCard(model, "fits", free_bytes=8 * GB)
        tip = card.toolTip()
        self.assertIn("Full path: %s" % model["path"], tip)
        self.assertIn("Size: 2 KB", tip)
        # 2 KB file + 512 MB headroom, rounded by format_size.
        self.assertIn("Needs 512 MB of RAM, 8.0 GB free", tip)
        self.assertIn("Fits: needs less than 3/4 of free RAM.", tip)
        card.deleteLater()

    def test_card_tooltip_carries_the_facts_line(self):
        model = {"name": "alpha.gguf",
                 "path": self.models / "alpha.gguf",
                 "size": 2048,
                 "facts": {"params_text": "2B",
                           "quantization": "Q4_K_M",
                           "context_text": "32K context"}}
        card = ModelCard(model, "fits", free_bytes=8 * GB)
        self.assertIn("2B · Q4_K_M · 32K context", card.toolTip())
        card.deleteLater()

    def test_compact_card_hides_facts_label_keeps_tooltip(self):
        model = {"name": "alpha.gguf",
                 "path": self.models / "alpha.gguf",
                 "size": 2048,
                 "facts": {"params_text": "2B",
                           "quantization": "Q4_K_M",
                           "context_text": "32K context"}}
        card = ModelCard(model, "fits", free_bytes=8 * GB)
        card.set_compact(True)
        facts = [label for label in card.findChildren(QLabel)
                 if label.objectName() == "model_facts"]
        self.assertEqual(len(facts), 1)
        self.assertTrue(facts[0].isHidden())
        self.assertIn("2B · Q4_K_M · 32K context", card.toolTip())
        card.deleteLater()

    def test_card_tooltip_too_big_and_unknown_ram(self):
        model = {"name": "huge.gguf",
                 "path": self.models / "huge.gguf",
                 "size": 4 * GB}
        card = ModelCard(model, "too big", free_bytes=1 * GB)
        self.assertIn("Too big: needs more RAM than is free.",
                      card.toolTip())
        card.deleteLater()
        card = ModelCard(model, "unknown", free_bytes=None)
        self.assertIn("free RAM unknown", card.toolTip())
        self.assertIn("fit is unknown", card.toolTip())
        card.deleteLater()

    def test_focus_shows_tooltip_and_blur_hides_it(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        make_fake_gguf(self.models / "beta.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        alpha = window.model_cards[str(self.models / "alpha.gguf")]
        beta = window.model_cards[str(self.models / "beta.gguf")]
        with mock.patch("fyrn.gui.QToolTip.showText") as show, \
                mock.patch("fyrn.gui.QToolTip.hideText") as hide:
            alpha.setFocus()
            # The focus tooltip is deferred 100 ms (task 7:
            # showText from inside focusInEvent never renders),
            # so a fixed 100 ms wait is exactly on the boundary
            # and races under load: wait for the call instead.
            shown_ok = pump_until(lambda: show.called, 2000)
            self.assertTrue(shown_ok,
                            "focusing a card did not show its detail")
            shown = show.call_args[0][1]
            self.assertIn("Full path:", shown)
            beta.setFocus()
            hidden_ok = pump_until(lambda: hide.called, 2000)
            self.assertTrue(hidden_ok,
                            "leaving a card did not hide its detail")


class MicroInteractionTests(GuiTestCase):
    def test_press_dip_shrinks_and_restores(self):
        # The dip is a 75 ms animation, and a fixed wait after the
        # press is a race under load: the animation may not have
        # run yet when the assertion fires. Poll until the geometry
        # settles instead: while the button is held, the dipped
        # size is the settled state, and after release the rest
        # size is. No assertion reads a transient.
        host = QWidget()
        self.windows.append(host)
        button = PressDipButton("Test", host)
        button.setGeometry(10, 10, 120, 40)
        host.show()
        QTest.qWait(100)
        rest = QRect(button.geometry())
        QTest.mousePress(button, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier,
                         button.rect().center())
        dipped = None
        for _ in range(40):  # up to 2 s for the dip to settle
            current = QRect(button.geometry())
            if (current.width() < rest.width()
                    and current.height() < rest.height()):
                dipped = current
                break
            QTest.qWait(50)
        self.assertIsNotNone(dipped, "button never dipped")
        self.assertEqual(dipped.center(), rest.center())
        QTest.mouseRelease(button, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier,
                           button.rect().center())
        restored = pump_until(
            lambda: button.geometry() == rest, 2000)
        self.assertTrue(restored, "button never came back to rest")
        self.assertEqual(button.geometry(), rest)

    def test_press_dip_skipped_under_reduced_motion(self):
        set_reduce_motion(True)
        host = QWidget()
        self.windows.append(host)
        button = PressDipButton("Test", host)
        button.setGeometry(10, 10, 120, 40)
        host.show()
        QTest.qWait(100)
        rest = QRect(button.geometry())
        QTest.mousePress(button, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier,
                         button.rect().center())
        QTest.qWait(120)
        self.assertEqual(button.geometry(), rest)
        QTest.mouseRelease(button, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier,
                           button.rect().center())

    def _two_cards(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        make_fake_gguf(self.models / "beta.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        alpha = window.model_cards[str(self.models / "alpha.gguf")]
        beta = window.model_cards[str(self.models / "beta.gguf")]
        # Park the cursor in empty space first: the mouse position
        # is global, so a cursor left over a card by the previous
        # test would lift this window's card before rest_y is read.
        QTest.mouseMove(window, QPoint(550, 400))
        # Then wait for the layout to settle: the cards' rest
        # position drifts a few pixels over the first ~200 ms
        # (scroll-area sizing), and a fixed pump is not enough
        # under load — poll until alpha's y holds still.
        stable = 0
        last_y = None
        for _ in range(40):  # up to 2 s
            y = alpha.geometry().y()
            if y == last_y:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
                last_y = y
            QTest.qWait(50)
        return window, alpha, beta

    def test_card_hover_lift_and_drop(self):
        # The lift is a 200 ms animation, and a fixed wait after
        # the hover is a race under load: the animation may not
        # have finished when the assertion fires. Poll until the
        # geometry settles instead. No assertion reads a transient.
        window, alpha, beta = self._two_cards()
        rest_y = alpha.geometry().y()
        QTest.mouseMove(alpha, alpha.rect().center())
        lifted = pump_until(
            lambda: alpha.geometry().y() == rest_y - 4, 2000)
        self.assertTrue(lifted, "card never lifted on hover")
        self.assertEqual(alpha.geometry().y(), rest_y - 4)
        QTest.mouseMove(beta, beta.rect().center())
        dropped = pump_until(
            lambda: alpha.geometry().y() == rest_y, 2000)
        self.assertTrue(dropped, "card never dropped after hover")
        self.assertEqual(alpha.geometry().y(), rest_y)

    def test_card_lift_does_not_bob_in_bottom_strip(self):
        # The cursor parked in the bottom 4 px: the lift moves the
        # card out from under it, which must not bob the card up
        # and down forever.
        window, alpha, _beta = self._two_cards()
        rest_y = alpha.geometry().y()
        strip = QPoint(alpha.width() // 2, alpha.height() - 2)
        QTest.mouseMove(alpha, strip)
        lifted = pump_until(
            lambda: alpha.geometry().y() == rest_y - 4, 2000)
        self.assertTrue(lifted, "card never lifted on hover")
        first = alpha.geometry().y()
        self.assertEqual(first, rest_y - 4)
        QTest.qWait(300)
        second = alpha.geometry().y()
        self.assertEqual(second, first, "card bobbed in the strip")
        # Moving truly away drops it (the hover timer catches the
        # exit, since no leave event fires from the strip).
        QTest.mouseMove(window, QPoint(550, 400))
        dropped = pump_until(
            lambda: alpha.geometry().y() == rest_y, 2000)
        self.assertTrue(dropped, "card never dropped after hover")
        self.assertEqual(alpha.geometry().y(), rest_y)

    def test_card_lift_skipped_under_reduced_motion(self):
        set_reduce_motion(True)
        window, alpha, _beta = self._two_cards()
        rest_y = alpha.geometry().y()
        QTest.mouseMove(alpha, alpha.rect().center())
        QTest.qWait(300)
        self.assertEqual(alpha.geometry().y(), rest_y)


class LabelBandTests(GuiTestCase):
    def test_card_labels_have_no_darker_band(self):
        # Labels inside a card used to paint the darker global
        # widget background behind their text; they must show the
        # card's own background instead.
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        card = window.model_cards[str(self.models / "alpha.gguf")]
        # Wait for the stagger fade to settle: a grab mid-fade
        # reads the right colour at slightly less than full
        # opacity, and this test is about the colour.
        settled = pump_until(
            lambda: card.graphicsEffect() is None, 2000)
        self.assertTrue(settled, "the card never settled")
        name = [label for label in card.findChildren(QLabel)
                if label.text() == "alpha.gguf"][0]
        # The label's right end, past the text: card background.
        point = name.mapTo(
            card, QPoint(name.width() - 5, name.height() // 2))
        image = card.grab().toImage()
        self.assertEqual(image.pixel(point), QColor("#1c2836").rgb())

    def test_empty_state_labels_transparent(self):
        window = self.make_window()
        self.wait_scan(window)
        self.assertIn(
            "QFrame#empty_state QLabel { "
            "background-color: transparent; }",
            window._empty_state.styleSheet())


class SkeletonTests(GuiTestCase):
    def test_skeleton_is_shape_blocks_not_text(self):
        # A skeleton is a shape, not a label: grey bars where the
        # name, size, and badge will land, and no text at all.
        skeleton = SkeletonCard()
        try:
            texts = [label.text()
                     for label in skeleton.findChildren(QLabel)]
            self.assertEqual(texts, [])
            self.assertEqual(len(skeleton.bars), 3)
            for bar in skeleton.bars:
                self.assertEqual(bar.objectName(), "skeleton_bar")
            self.assertIn("#2a3644", skeleton.styleSheet())
        finally:
            skeleton.deleteLater()


class KeyboardTests(GuiTestCase):
    def _two_models(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        make_fake_gguf(self.models / "beta.gguf", 2048)

    def _cards(self, window):
        return (window.model_cards[str(self.models / "alpha.gguf")],
                window.model_cards[str(self.models / "beta.gguf")])

    def test_cards_take_strong_focus(self):
        self._two_models()
        window = self.make_window()
        self.wait_scan(window)
        for card in window.model_cards.values():
            self.assertEqual(card.focusPolicy(),
                             Qt.FocusPolicy.StrongFocus)

    def test_tab_moves_between_cards(self):
        self._two_models()
        window = self.make_window()
        self.wait_scan(window)
        alpha, beta = self._cards(window)
        alpha.setFocus()
        QTest.qWait(50)
        self.assertTrue(alpha.hasFocus())
        QTest.keyClick(alpha, Qt.Key.Key_Tab)
        QTest.qWait(50)
        self.assertTrue(beta.hasFocus())

    def test_space_selects_focused_card(self):
        self._two_models()
        window = self.make_window()
        self.wait_scan(window)
        alpha, _beta = self._cards(window)
        alpha.setFocus()
        QTest.keyClick(alpha, Qt.Key.Key_Space)
        QTest.qWait(50)
        self.assertEqual(window._selected_path,
                         str(self.models / "alpha.gguf"))
        self.assertTrue(alpha._selected)

    def test_enter_loads_focused_card(self):
        self._two_models()
        window = self.make_window()
        self.wait_scan(window)
        window.controller._free_ram_fn = lambda: 8 * GB
        alpha, _beta = self._cards(window)
        alpha.setFocus()
        QTest.keyClick(alpha, Qt.Key.Key_Return)
        self.wait_load(window)
        self.assertTrue(window.controller.is_loaded)
        self.assertEqual(window.controller.current_name, "alpha.gguf")


class RemoveModelTests(GuiTestCase):
    def _one_model(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        return window

    def _select(self, window, name="alpha.gguf"):
        path = str(self.models / name)
        window.model_cards[path].clicked.emit(path)
        return path

    def test_remove_button_needs_selection(self):
        window = self._one_model()
        self.assertFalse(window.remove_button.isEnabled())
        self._select(window)
        self.assertTrue(window.remove_button.isEnabled())

    def test_remove_no_keeps_file(self):
        window = self._one_model()
        self._select(window)
        window.remove_button.click()
        QTest.qWait(300)
        # The watcher clicks the default button (No).
        self.assertTrue((self.models / "alpha.gguf").is_file())
        self.assertIn(str(self.models / "alpha.gguf"),
                      window.model_cards)
        self.assertTrue(
            any("Remove alpha.gguf" in text and "2 KB" in text
                and "moves the file to the trash" in text
                for _title, text in self.watcher.seen),
            "no remove confirm naming the file, size, and trash "
            "appeared")

    def test_remove_yes_moves_to_trash(self):
        window = self._one_model()
        self._select(window)
        timer = self._answer_yes()
        try:
            window.remove_button.click()
        finally:
            timer.stop()
            self.watcher.enabled = True
        self.wait_scan(window)
        self.assertFalse((self.models / "alpha.gguf").exists())
        self.assertNotIn(str(self.models / "alpha.gguf"),
                         window.model_cards)
        # The file is in the trash, recoverable, not deleted.
        trash_file = (self.data_home / "Trash" / "files"
                      / "alpha.gguf")
        self.assertTrue(trash_file.is_file())
        self.assertTrue((self.data_home / "Trash" / "info"
                         / "alpha.gguf.trashinfo").is_file())
        self.assertIn("moved to the trash",
                      window.statusBar().currentMessage())

    def test_remove_loaded_model_unloads_first(self):
        window = self._one_model()
        self.load_first_model(window)
        self.assertTrue(window.controller.is_loaded)
        self.wait_scan(window)
        self._select(window)
        timer = self._answer_yes()
        try:
            window.remove_button.click()
        finally:
            timer.stop()
            self.watcher.enabled = True
        self.wait_scan(window)
        self.assertFalse(window.controller.is_loaded)
        self.assertFalse((self.models / "alpha.gguf").exists())

    def test_remove_via_card_signal(self):
        window = self._one_model()
        card = window.model_cards[str(self.models / "alpha.gguf")]
        timer = self._answer_yes()
        try:
            card.remove_requested.emit(card.path)
        finally:
            timer.stop()
            self.watcher.enabled = True
        self.wait_scan(window)
        self.assertFalse((self.models / "alpha.gguf").exists())


def _png_bytes(image):
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    return bytes(buf.data())


class FocusRingTests(GuiTestCase):
    def test_stylesheet_has_button_focus_rule(self):
        match = re.search(r"QPushButton:focus\s*\{([^}]*)\}",
                          DARK_STYLESHEET)
        self.assertIsNotNone(match, "no QPushButton:focus rule")
        self.assertIn("#f2c14e", match.group(1))

    def test_focused_button_pixels_differ(self):
        window = self.make_window()
        button = window.import_button  # always enabled
        QTest.qWait(100)
        button.clearFocus()
        QTest.qWait(100)
        self.assertFalse(button.hasFocus())
        plain = button.grab().toImage()
        button.setFocus()
        QTest.qWait(100)
        self.assertTrue(button.hasFocus())
        focused = button.grab().toImage()
        self.assertEqual(plain.size(), focused.size())
        # The top border pixel turns amber when focused.
        amber = QColor("#f2c14e").rgb()
        mid = plain.width() // 2
        self.assertEqual(focused.pixel(mid, 0), amber)
        self.assertNotEqual(plain.pixel(mid, 0), amber)
        # And the whole render differs, not just one pixel.
        self.assertNotEqual(_png_bytes(plain), _png_bytes(focused))


class PullWindowTests(GuiTestCase):
    def make_pull_window(self):
        pull = PullWindow(self.models)
        self.windows.append(pull)
        pull.show()
        return pull

    def test_pull_flow_downloads_file(self):
        server, url = start_range_server(PAYLOAD)
        self.servers.append(server)
        pull = self.make_pull_window()
        pulled = []
        pull.model_pulled.connect(pulled.append)
        pull.input.setText(url)
        pull.pull_button.click()
        ok = pump_until(lambda: pull.worker is None, 10000)
        self.assertTrue(ok, "pull did not finish")
        QTest.qWait(300)  # let the celebrate fade run
        self.assertEqual(file_bytes(self.models / "model.gguf"), PAYLOAD)
        self.assertEqual(pull.progress_bar.value(), 100)
        self.assertIn("Done.", pull.status_label.text())
        self.assertEqual(pulled, [str(self.models / "model.gguf")])

    def test_pull_bad_input_shows_plain_error(self):
        # A bad input is a quiet inline error, not a modal: the
        # user is looking at the field (L3).
        pull = self.make_pull_window()
        pull.input.setText("just some words here")
        pull.pull_button.click()
        QTest.qWait(300)
        self.assertIsNone(pull.worker)
        self.assertEqual(self.watcher.seen, [])
        self.assertIn("could not understand",
                      pull.status_label.text())

    def test_pull_already_there_asks_to_replace(self):
        # A model that is already there asks about pulling over
        # it (default No, the same confirm habit as Remove),
        # instead of refusing outright.
        make_fake_gguf(self.models / "model.gguf", 1024)
        server, url = start_range_server(PAYLOAD)
        self.servers.append(server)
        pull = self.make_pull_window()
        pull.input.setText(url)
        pull.pull_button.click()
        QTest.qWait(300)
        # The watcher clicks the default button (No): no pull
        # starts, and the file is untouched.
        self.assertIsNone(pull.worker)
        self.assertEqual(
            (self.models / "model.gguf").stat().st_size, 1024)
        self.assertTrue(
            any("Pull it again and replace it" in text
                for _title, text in self.watcher.seen),
            "no re-pull confirm appeared")

    def test_download_failure_shows_modal_and_status(self):
        # The one pull error that keeps the modal: a download
        # fails while the user may be elsewhere (L3).
        pull = self.make_pull_window()
        pull._on_failed(
            "The download failed (server said HTTP 404).")
        QTest.qWait(300)
        self.assertIn("HTTP 404", pull.status_label.text())
        self.assertTrue(any("HTTP 404" in text
                            for _title, text in self.watcher.seen))

    def test_pull_resumes_partial(self):
        server, url = start_range_server(PAYLOAD)
        self.servers.append(server)
        part = self.models / "model.gguf.part"
        part.write_bytes(PAYLOAD[:len(PAYLOAD) // 2])
        pull = self.make_pull_window()
        pull.input.setText(url)
        pull.pull_button.click()
        ok = pump_until(lambda: pull.worker is None, 10000)
        self.assertTrue(ok, "resumed pull did not finish")
        self.assertEqual(file_bytes(self.models / "model.gguf"), PAYLOAD)

    def test_pull_cancel_keeps_partial(self):
        server, url = start_range_server(PAYLOAD)
        self.servers.append(server)
        worker = PullWorker(url, self.models / "model.gguf")
        cancelled = []
        worker.cancelled.connect(lambda: cancelled.append(True))
        worker.cancel()  # cancel before it starts: deterministic
        worker.start()
        ok = pump_until(lambda: bool(cancelled), 5000)
        self.assertTrue(ok, "cancelled signal did not fire")
        # Wait for the thread to fully exit before the worker goes
        # out of scope, or Qt aborts ("Destroyed while thread is
        # still running").
        worker.wait()
        self.assertTrue((self.models / "model.gguf.part").exists())

    def test_suggestion_button_fills_input(self):
        pull = self.make_pull_window()
        pull.suggest_button.click()
        QTest.qWait(50)
        self.assertEqual(pull.input.text(), FIRST_PULL_SUGGESTION)

    def test_suggestion_parses_to_a_real_target(self):
        # The suggestion must be a well-formed pull request: a real
        # repo and file that parse_model_input turns into a
        # download URL. (The repo and file were checked against
        # Hugging Face when the suggestion was added; the test pins
        # the shape so a later edit cannot silently break it.)
        target = parse_model_input(FIRST_PULL_SUGGESTION)
        self.assertEqual(
            target.url,
            "https://huggingface.co/bartowski/"
            "Qwen2.5-0.5B-Instruct-GGUF/resolve/main/"
            "Qwen2.5-0.5B-Instruct-Q4_K_M.gguf")
        self.assertEqual(target.filename,
                         "Qwen2.5-0.5B-Instruct-Q4_K_M.gguf")

    def test_help_says_where_models_come_from(self):
        pull = self.make_pull_window()
        texts = " ".join(label.text() for label in
                         pull.findChildren(QLabel))
        self.assertIn("website where models are listed", texts)
        self.assertIn("First time here", texts)

    def test_close_mid_pull_asks_and_no_keeps_pulling(self):
        pull = self.make_pull_window()
        # A worker that exists but never started: deterministic,
        # no network timing involved.
        worker = PullWorker("http://127.0.0.1/model.gguf",
                            self.models / "model.gguf")
        pull.worker = worker
        try:
            pull.close()
            QTest.qWait(300)
            # The watcher clicks the default button (No).
            self.assertTrue(pull.isVisible())
            self.assertFalse(worker.cancel_event.is_set())
            self.assertTrue(
                any("Stop this pull" in text
                    for _title, text in self.watcher.seen),
                "no stop-pull question appeared")
        finally:
            # Drop the stub before tearDown, or its close would
            # ask again with the watcher stopped (and hang).
            pull.worker = None

    def test_close_mid_pull_yes_stops(self):
        pull = self.make_pull_window()
        worker = PullWorker("http://127.0.0.1/model.gguf",
                            self.models / "model.gguf")
        pull.worker = worker
        timer = self._answer_yes()
        try:
            pull.close()
        finally:
            timer.stop()
            self.watcher.enabled = True
        self.assertTrue(worker.cancel_event.is_set())
        self.assertFalse(pull.isVisible())

    def test_reduced_motion_skips_celebration(self):
        pull = self.make_pull_window()
        old = os.environ.get("FYRN_REDUCED_MOTION")
        os.environ["FYRN_REDUCED_MOTION"] = "1"
        try:
            self.assertTrue(reduced_motion())
            pull._celebrate()
            self.assertIsNone(pull.status_label.graphicsEffect())
        finally:
            if old is None:
                del os.environ["FYRN_REDUCED_MOTION"]
            else:
                os.environ["FYRN_REDUCED_MOTION"] = old
        self.assertFalse(reduced_motion())


class SettingsWindowTests(GuiTestCase):
    def test_settings_changes_folder(self):
        make_fake_gguf(self.models / "old.gguf", 1024)
        window = self.make_window()
        self.wait_scan(window)
        self.assertEqual(len(window.model_cards), 1)
        new_folder = self.base / "elsewhere"
        new_folder.mkdir()
        make_fake_gguf(new_folder / "new.gguf", 1024)
        window.show_settings()
        settings = window.settings_window
        self.windows.append(settings)
        settings.folder_edit.setText(str(new_folder))
        settings.save_button.click()
        QTest.qWait(100)
        self.assertEqual(get_models_folder(), new_folder)
        self.assertEqual(window.models_folder, new_folder)
        self.wait_scan(window)
        self.assertEqual(list(window.model_cards),
                         [str(new_folder / "new.gguf")])

    def test_settings_window_is_resizable(self):
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        settings.resize(900, 500)
        QTest.qWait(100)
        self.assertEqual(settings.width(), 900)
        self.assertEqual(settings.height(), 500)

    def test_reduce_motion_switch_saves(self):
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        self.assertFalse(settings.motion_check.isChecked())
        self.assertFalse(get_reduce_motion())
        settings.motion_check.setChecked(True)
        settings.save_button.click()
        QTest.qWait(100)
        self.assertTrue(get_reduce_motion())
        self.assertTrue(reduced_motion())
        # Reopening shows the saved state; unchecking clears it.
        second = SettingsWindow(self.models)
        self.windows.append(second)
        second.show()
        self.assertTrue(second.motion_check.isChecked())
        second.motion_check.setChecked(False)
        second.save_button.click()
        QTest.qWait(100)
        self.assertFalse(get_reduce_motion())
        self.assertFalse(reduced_motion())

    def test_reduce_motion_env_var_still_overrides(self):
        # The switch is off, but the environment variable still
        # turns reduced motion on (it is the override).
        set_reduce_motion(False)
        old = os.environ.get("FYRN_REDUCED_MOTION")
        os.environ["FYRN_REDUCED_MOTION"] = "1"
        try:
            self.assertTrue(reduced_motion())
        finally:
            if old is None:
                del os.environ["FYRN_REDUCED_MOTION"]
            else:
                os.environ["FYRN_REDUCED_MOTION"] = old
        self.assertFalse(reduced_motion())


class SubWindowGeometryTests(GuiTestCase):
    def _reopen(self, make, rect=(50, 60, 700, 400)):
        """Close a window built by make(), build it again, and
        return the second one."""
        first = make()
        self.windows.append(first)
        first.show()
        first.setGeometry(*rect)
        first.close()
        first.deleteLater()
        self.windows.remove(first)
        QTest.qWait(100)
        second = make()
        self.windows.append(second)
        return second

    def test_pull_window_remembers_geometry(self):
        second = self._reopen(lambda: PullWindow(self.models))
        self.assertEqual(second.geometry(), QRect(50, 60, 700, 400))

    def test_howto_window_remembers_geometry(self):
        # How to Use has a 420 px minimum height, so the saved
        # rect must respect it (400 would clamp to 420).
        second = self._reopen(HowToUseWindow, (50, 60, 720, 480))
        self.assertEqual(second.geometry(), QRect(50, 60, 720, 480))

    def test_settings_window_remembers_geometry_via_save(self):
        # Save hides the dialog without a close event, so the
        # geometry must be saved on hide, not only on close.
        first = SettingsWindow(self.models)
        self.windows.append(first)
        first.show()
        first.setGeometry(50, 60, 700, 400)
        first.save_button.click()
        QTest.qWait(100)
        first.deleteLater()
        self.windows.remove(first)
        second = SettingsWindow(self.models)
        self.windows.append(second)
        self.assertEqual(second.geometry(), QRect(50, 60, 700, 400))


class ChatInputTests(GuiTestCase):
    def test_enter_sends(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.load_first_model(window)
        window.tabs.setCurrentIndex(1)
        window.activateWindow()
        window.raise_()
        window.chat_input.setFocus()
        QTest.qWait(100)
        window.chat_input.setPlainText("hi there")
        QTest.keyClick(window.chat_input, Qt.Key.Key_Return)
        self.wait_chat(window)
        self.assertIn("You: hi there",
                      window.transcript.toPlainText())

    def test_shift_enter_inserts_newline_without_sending(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.load_first_model(window)
        window.tabs.setCurrentIndex(1)
        window.activateWindow()
        window.raise_()
        window.chat_input.setFocus()
        QTest.qWait(100)
        window.chat_input.setPlainText("line one")
        cursor = window.chat_input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        window.chat_input.setTextCursor(cursor)
        QTest.keyClick(window.chat_input, Qt.Key.Key_Return,
                       Qt.KeyboardModifier.ShiftModifier)
        QTest.qWait(100)
        self.assertEqual(window.chat_input.toPlainText(),
                         "line one\n")
        self.assertIsNone(window.chat_worker)
        self.assertNotIn("You:",
                         window.transcript.toPlainText())

    def test_input_grows_with_lines(self):
        window = self.make_window()
        window.tabs.setCurrentIndex(1)
        QTest.qWait(100)
        one_line = window.chat_input.height()
        window.chat_input.setPlainText("one\ntwo\nthree\nfour")
        QTest.qWait(100)
        self.assertGreater(window.chat_input.height(), one_line)
        self.assertLessEqual(window.chat_input.height(),
                             ChatInput.MAX_HEIGHT)

    def test_input_is_multiline_widget(self):
        window = self.make_window()
        self.assertIsInstance(window.chat_input, QTextEdit)
        self.assertEqual(window.chat_input.placeholderText(),
                         "Type a message")


class ClearChatTests(GuiTestCase):
    def test_clear_empties_transcript(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.load_first_model(window)
        window.chat_input.setText("hi there")
        window.send_message()
        self.wait_chat(window)
        self.assertIn("You: hi there",
                      window.transcript.toPlainText())
        window.clear_button.click()
        QTest.qWait(50)
        self.assertEqual(window.transcript.toPlainText(), "")
        # Nothing is saved, so clearing asks nothing: no dialog.
        self.assertEqual(self.watcher.seen, [])

    def test_clear_on_empty_transcript_is_a_noop(self):
        window = self.make_window()
        window.clear_button.click()
        QTest.qWait(50)
        self.assertEqual(window.transcript.toPlainText(), "")
        self.assertEqual(self.watcher.seen, [])


class WindowHeightTests(GuiTestCase):
    def test_pull_window_hugs_its_content(self):
        # L10: the v0.1 Pull window carried ~100 px of dead space
        # below the status line; the v0.1.1 suggestion content
        # filled it. Pin the tightness: the status line ends
        # within a layout margin of the window bottom.
        pull = PullWindow(self.models)
        self.windows.append(pull)
        pull.show()
        QTest.qWait(200)
        bottom = pull.status_label.geometry().bottom()
        self.assertLess(pull.height() - bottom, 30)

    def test_settings_window_hugs_its_content(self):
        # L10: the v0.1 Settings window carried a ~100 px empty
        # band above the buttons; the v0.1.1 Reduce motion switch
        # filled it. Pin the tightness: the buttons end within a
        # layout margin of the window bottom, and the stretch
        # between the last help line and the buttons is small.
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        QTest.qWait(200)
        bottom = settings.save_button.geometry().bottom()
        self.assertLess(settings.height() - bottom, 30)
        # The last help line is the update check's: the stretch
        # between it and the buttons must stay small.
        helps = [label for label in settings.findChildren(QLabel)
                 if label.text().startswith(
                     "Fyrn quietly asks GitHub")]
        self.assertEqual(len(helps), 1)
        gap = (settings.save_button.geometry().top()
               - helps[0].geometry().bottom())
        self.assertLess(gap, 30)


class DescriptionTests(GuiTestCase):
    def test_card_shows_description_from_gguf_header(self):
        write_gguf_file(
            self.models / "alpha.gguf",
            {"general.description": "A small model for testing."})
        window = self.make_window()
        self.wait_scan(window)
        card = window.model_cards[str(self.models / "alpha.gguf")]
        labels = [label for label in card.findChildren(QLabel)
                  if label.objectName() == "model_description"]
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0].text(),
                         "A small model for testing.")
        self.assertIn("About: A small model for testing.",
                      card.toolTip())

    def test_card_without_description_shows_none(self):
        # A file with no description gets no description line:
        # descriptions are read from the file, never invented.
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        card = window.model_cards[str(self.models / "alpha.gguf")]
        labels = [label for label in card.findChildren(QLabel)
                  if label.objectName() == "model_description"]
        self.assertEqual(labels, [])
        self.assertNotIn("About:", card.toolTip())

    def test_long_description_is_clipped(self):
        long_text = "A very chatty model. " * 20  # 420 chars
        write_gguf_file(
            self.models / "alpha.gguf",
            {"general.description": long_text})
        window = self.make_window()
        self.wait_scan(window)
        card = window.model_cards[str(self.models / "alpha.gguf")]
        labels = [label for label in card.findChildren(QLabel)
                  if label.objectName() == "model_description"]
        self.assertEqual(len(labels), 1)
        self.assertTrue(labels[0].text().endswith("..."))
        self.assertLessEqual(len(labels[0].text()), 140)


class TrayTests(GuiTestCase):
    def test_no_tray_when_unavailable(self):
        # Offscreen has no system tray: the window simply has no
        # tray icon, and nothing crashes or complains (L6).
        self.assertFalse(QSystemTrayIcon.isSystemTrayAvailable())
        window = self.make_window()
        self.assertIsNone(window.tray)

    def test_tray_tooltip_names_loaded_model_and_ram(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        tip = window._tray_tooltip()
        self.assertIn("no model loaded", tip)
        self.assertIn("RAM:", tip)
        self.load_first_model(window)
        tip = window._tray_tooltip()
        self.assertIn("alpha.gguf", tip)
        self.assertIn("RAM:", tip)

    def test_tray_created_when_available(self):
        # With a tray available, the window gets one with the
        # tooltip set. Offscreen has none, so availability is
        # faked for this test only.
        with mock.patch.object(
                QSystemTrayIcon, "isSystemTrayAvailable",
                return_value=True):
            window = self.make_window()
        self.assertIsNotNone(window.tray)
        self.assertIn("Fyrn:", window.tray.toolTip())
        self.assertIsNotNone(window.tray.contextMenu())

    def test_refresh_stats_updates_tray_tooltip(self):
        # The 2-second stats refresh keeps the tray tooltip
        # current. The tray is a stand-in here: offscreen has
        # none, so it records the tooltip instead.
        window = self.make_window()
        tooltips = []

        class FakeTray:
            def setToolTip(self, text):
                tooltips.append(text)

            def hide(self):
                pass  # closeEvent hides the tray on the way out

        window.tray = FakeTray()
        window._refresh_stats()
        self.assertEqual(len(tooltips), 1)
        self.assertIn("Fyrn:", tooltips[0])

    def test_tray_activation_raises_window(self):
        window = self.make_window()
        with mock.patch.object(window, "bring_to_front") as raise_:
            window._on_tray_activated(
                QSystemTrayIcon.ActivationReason.Trigger)
        self.assertTrue(raise_.called)


class HowToUseTests(GuiTestCase):
    def test_first_steps_is_first(self):
        # A new user must not have to guess which section to read
        # first: the start-here path is first in the list.
        howto = HowToUseWindow()
        self.windows.append(howto)
        howto.show()
        self.assertEqual(howto.section_list.item(0).text(),
                         "First steps")
        howto.section_list.setCurrentRow(0)
        QTest.qWait(50)
        body = howto.text_view.toPlainText()
        self.assertIn("Get a model", body)
        self.assertIn("Load it", body)
        self.assertIn("Chat", body)

    def test_all_sections_present_and_readable(self):
        howto = HowToUseWindow()
        self.windows.append(howto)
        howto.show()
        self.assertEqual(howto.section_list.count(),
                         len(HOW_TO_USE_SECTIONS))
        titles = [howto.section_list.item(i).text()
                  for i in range(howto.section_list.count())]
        self.assertEqual(titles, list(HOW_TO_USE_SECTIONS))
        for row, title in enumerate(titles):
            howto.section_list.setCurrentRow(row)
            QTest.qWait(50)
            body = howto.text_view.toPlainText()
            self.assertTrue(body)
            self.assertEqual(body, HOW_TO_USE_SECTIONS[title])
        # No section name is clipped: the list is wide enough that
        # its horizontal scrollbar never appears.
        self.assertFalse(
            howto.section_list.horizontalScrollBar().isVisible())


class SingleInstanceTests(GuiTestCase):
    def test_second_instance_cannot_take_lock(self):
        first = SingleInstance(self.home)
        second = SingleInstance(self.home)
        self.assertTrue(first.try_take())
        self.assertFalse(second.try_take())
        first.release()
        self.assertTrue(second.try_take())
        second.release()

    def test_notify_running_reaches_server(self):
        first = SingleInstance(self.home)
        second = SingleInstance(self.home)
        self.assertTrue(first.try_take())
        raised = []
        self.assertTrue(first.start_server(lambda: raised.append(True)))
        self.assertTrue(second.notify_running())
        pump_until(lambda: bool(raised), 5000)
        self.assertEqual(raised, [True])
        first.release()

    def test_notify_without_server_fails(self):
        lone = SingleInstance(self.home)
        self.assertFalse(lone.notify_running())


class TopAlignTests(GuiTestCase):
    def test_mixed_height_cards_share_a_row_top(self):
        # One card has a description (taller), two do not: the
        # grid used to center them, so the tall card sat 15 px
        # above its neighbours and the row looked ragged.
        write_gguf_file(
            self.models / "alpha.gguf",
            {"general.description": "A small test model."})
        make_fake_gguf(self.models / "beta.gguf", 2048)
        make_fake_gguf(self.models / "gamma.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        cards = list(window.model_cards.values())
        self.assertEqual(len(cards), 3)
        # Park the cursor in empty space first: the mouse position
        # is global, and a cursor left over a card by the previous
        # test would lift that card 4 px and skew the measurement.
        QTest.mouseMove(window, QPoint(550, 400))
        QTest.qWait(300)  # past the 200 ms drop
        # Wait for the layout to settle (scroll-area sizing
        # drifts a few pixels at first).
        stable = 0
        last = None
        for _ in range(40):
            ys = tuple(card.geometry().y() for card in cards)
            if ys == last:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
                last = ys
            QTest.qWait(50)
        ys = {card.geometry().y() for card in cards}
        self.assertEqual(len(ys), 1,
                         "cards in one row sit at different "
                         "heights: %s" % sorted(ys))


class FailingRunner(FakeRunner):
    """Loads once, then fails every load after that."""

    def __init__(self):
        super().__init__()
        self.fail_next = False

    def load(self, path, progress_cb=None, cancel_event=None,
             display_name=None):
        if self.fail_next:
            raise RunnerError(
                "The model could not be loaded (fake bad file).")
        return super().load(path, progress_cb, cancel_event,
                            display_name)


class FailedLoadTests(GuiTestCase):
    def test_failed_load_resets_to_nothing_loaded(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        make_fake_gguf(self.models / "beta.gguf", 2048)
        runner = FailingRunner()
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        self.wait_scan(window)  # the hero tile is built by the scan
        self.assertIsNotNone(window._hero_card)
        runner.fail_next = True
        beta = str(self.models / "beta.gguf")
        window.model_cards[beta].clicked.emit(beta)
        window.load_button.click()
        self.wait_load(window)
        self.wait_scan(window)
        self.assertFalse(window.controller.is_loaded)
        self.assertIsNone(window._hero_card)
        self.assertEqual(window.transcript.placeholderText(),
                         "Load a model first, then chat here.")
        loaded_labels = [label for label in
                         window.findChildren(QLabel)
                         if label.text() == "Loaded"]
        self.assertEqual(loaded_labels, [])
        self.assertTrue(
            any(title == "Load model"
                for title, _text in self.watcher.seen),
            "no failure modal appeared")


class ReplyButtonTests(GuiTestCase):
    def test_load_unload_remove_disabled_during_a_reply(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        make_fake_gguf(self.models / "beta.gguf", 2048)
        runner = FakeRunner(
            reply_text="one two three four five six seven eight",
            token_delay_s=0.3)
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        beta = str(self.models / "beta.gguf")
        window.model_cards[beta].clicked.emit(beta)
        window.chat_input.setText("go")
        window.send_button.click()
        pump_until(lambda: window.chat_worker is not None, 5000)
        self.assertFalse(window.load_button.isEnabled())
        self.assertFalse(window.unload_button.isEnabled())
        self.assertFalse(window.remove_button.isEnabled())
        self.wait_chat(window)
        self.assertTrue(window.load_button.isEnabled())
        self.assertTrue(window.unload_button.isEnabled())
        self.assertTrue(window.remove_button.isEnabled())

    def test_unload_during_a_reply_says_so_and_keeps_the_model(self):
        # v0.7: on a real PC a greyed Unload during a reply read
        # as a broken button, and the guard returned silently.
        # Unload still refuses mid-reply; now it says why.
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        runner = FakeRunner(
            reply_text="one two three four five six seven eight",
            token_delay_s=0.3)
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        window.chat_input.setText("go")
        window.send_button.click()
        pump_until(lambda: window.chat_worker is not None, 5000)
        self.assertIsNotNone(window.chat_worker)
        # The button is greyed during a reply; the guard is
        # reached through the method (menu, shortcut, or race).
        window.unload_model()
        self.assertIn("Wait for the reply to finish first.",
                      window.statusBar().currentMessage())
        self.assertTrue(window.controller.is_loaded)
        self.wait_chat(window)
        # After the reply, Unload works again.
        self.assertTrue(window.unload_button.isEnabled())
        window.unload_button.click()
        QTest.qWait(100)
        self.assertFalse(window.controller.is_loaded)

    def test_reply_in_progress_shows_on_the_models_tab(self):
        # v0.7: the hero tile's stats line (visible on the Models
        # tab) carries "Reply in progress…" while a reply
        # streams, so a greyed Unload is explained; the line
        # goes away when the reply ends.
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        runner = FakeRunner(
            reply_text="one two three four five six seven eight",
            token_delay_s=0.3)
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        ok = pump_until(lambda: window._hero_card is not None,
                        5000)
        self.assertTrue(ok, "hero tile never appeared")
        hero = window._hero_card
        self.assertNotIn("Reply in progress",
                         hero.stats_label.text())
        window.chat_input.setText("go")
        window.send_button.click()
        pump_until(lambda: window.chat_worker is not None, 5000)
        ok = pump_until(
            lambda: "Reply in progress" in hero.stats_label.text()
            and "Reply in progress" in window.stats_label.text(),
            5000)
        self.assertTrue(ok, "reply indicator never appeared")
        self.wait_chat(window)
        self.assertNotIn("Reply in progress",
                         hero.stats_label.text())
        self.assertNotIn("Reply in progress",
                         window.stats_label.text())


class LoadOfferedTests(GuiTestCase):
    def test_load_not_offered_for_the_loaded_model(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        make_fake_gguf(self.models / "beta.gguf", 2048)
        window = self.make_window()
        self.load_first_model(window)
        # Alpha is loaded and selected: Load is not offered.
        self.assertFalse(window.load_button.isEnabled())
        beta = str(self.models / "beta.gguf")
        window.model_cards[beta].clicked.emit(beta)
        self.assertTrue(window.load_button.isEnabled())


class HistoryTests(GuiTestCase):
    def test_second_message_carries_the_first_turn(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        runner = FakeRunner(reply_text="ok")
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        window.chat_input.setText("first")
        window.send_button.click()
        self.wait_chat(window)
        window.chat_input.setText("second")
        window.send_button.click()
        self.wait_chat(window)
        self.assertEqual(runner.last_history, [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"}])

    def test_clear_empties_history_and_stats(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        runner = FakeRunner(reply_text="ok")
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        window.chat_input.setText("hi there")
        window.send_button.click()
        self.wait_chat(window)
        self.assertIn("Reply: 1 tokens",
                      window.stats_label.text())
        self.assertEqual(len(window._chat_history), 2)
        window.clear_button.click()
        QTest.qWait(50)
        self.assertEqual(window.transcript.toPlainText(), "")
        self.assertNotIn("Reply:", window.stats_label.text())
        self.assertEqual(window._chat_history, [])
        # The next message starts fresh: no history reaches the
        # model for a conversation that no longer exists.
        window.chat_input.setText("after")
        window.send_button.click()
        self.wait_chat(window)
        self.assertEqual(runner.last_history, [])


def make_ollama_store(root, name, tag, digest, description=""):
    """A minimal fake Ollama store with one model."""
    root = Path(root)
    manifest = (root / "manifests" / "registry.ollama.ai"
                / "library" / name / tag)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "layers": [
            {"mediaType": "application/vnd.ollama.image.model",
             "digest": "sha256:" + digest, "size": 2048}]}),
        encoding="utf-8")
    blob = root / "blobs" / ("sha256-" + digest)
    blob.parent.mkdir(parents=True, exist_ok=True)
    write_gguf_file(blob, {"general.description": description}
                    if description else {})
    return root


class OllamaDiscoveryTests(GuiTestCase):
    def setUp(self):
        super().setUp()
        self._old_ollama = os.environ.get("OLLAMA_MODELS")
        self.store = make_ollama_store(
            self.base / "ollama-store", "tiny", "1b", "ab" * 32,
            "Tiny test.")
        os.environ["OLLAMA_MODELS"] = str(self.store)
        set_show_ollama_models(True)

    def tearDown(self):
        if self._old_ollama is None:
            os.environ.pop("OLLAMA_MODELS", None)
        else:
            os.environ["OLLAMA_MODELS"] = self._old_ollama
        super().tearDown()

    def _ollama_cards(self, window):
        return [card for card in window.model_cards.values()
                if card.source == "ollama"]

    def test_ollama_model_appears_with_its_name(self):
        window = self.make_window()
        self.wait_scan(window)
        tiny = [card for card in self._ollama_cards(window)
                if card.model_name == "tiny:1b"]
        self.assertEqual(len(tiny), 1)
        tag = tiny[0].findChild(QLabel, "ollama_tag")
        self.assertIsNotNone(tag)
        self.assertEqual(tag.text(), "Ollama")
        self.assertIn("From Ollama", tiny[0].toolTip())

    def test_ollama_model_loads_under_its_own_name(self):
        window = self.make_window()
        self.wait_scan(window)
        tiny = [card for card in self._ollama_cards(window)
                if card.model_name == "tiny:1b"][0]
        window.controller._free_ram_fn = lambda: 8 * GB
        window._on_card_clicked(tiny.path)
        window.load_button.click()
        self.wait_load(window)
        self.assertTrue(window.controller.is_loaded)
        # The chat attribution shows the model name, not the
        # blob's sha256 file name.
        self.assertEqual(window.controller.current_name, "tiny:1b")

    def test_remove_disabled_for_ollama_models(self):
        window = self.make_window()
        self.wait_scan(window)
        tiny = [card for card in self._ollama_cards(window)
                if card.model_name == "tiny:1b"][0]
        window._on_card_clicked(tiny.path)
        self.assertFalse(window.remove_button.isEnabled())

    def test_toggle_off_hides_ollama_models(self):
        window = self.make_window()
        self.wait_scan(window)
        self.assertTrue(self._ollama_cards(window))
        set_show_ollama_models(False)
        window._refresh_model_list()
        self.wait_scan(window)
        self.assertEqual(self._ollama_cards(window), [])


class ImportWorkerTests(GuiTestCase):
    def _slow_import(self, source, folder, progress_cb=None,
                     cancel_event=None):
        """A stand-in copy that takes ~0.5 s and honours cancel."""
        total = 10
        for step in range(1, total + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise ImportCancelled("Import stopped.")
            time.sleep(0.05)
            if progress_cb is not None:
                progress_cb(step, total)
        dest = Path(folder) / Path(source).name
        dest.write_bytes(b"x")
        return dest

    def test_import_shows_progress_then_finishes(self):
        elsewhere = self.base / "elsewhere.gguf"
        make_fake_gguf(elsewhere, 1024)
        window = self.make_window()
        self.wait_scan(window)
        with mock.patch("fyrn.gui.QFileDialog.getOpenFileName",
                        return_value=(str(elsewhere), "")), \
                mock.patch("fyrn.gui.core.import_gguf",
                           side_effect=self._slow_import):
            window.import_button.click()
            moved = pump_until(
                lambda: window.load_progress.isVisible()
                and window.load_progress.value() > 0, 5000)
            self.assertTrue(moved, "no import progress shown")
            self.wait_import(window)
        self.wait_scan(window)
        self.assertTrue((self.models / "elsewhere.gguf").is_file())

    def test_import_cancel_stops_and_leaves_nothing(self):
        elsewhere = self.base / "elsewhere.gguf"
        make_fake_gguf(elsewhere, 1024)
        window = self.make_window()
        self.wait_scan(window)
        with mock.patch("fyrn.gui.QFileDialog.getOpenFileName",
                        return_value=(str(elsewhere), "")), \
                mock.patch("fyrn.gui.core.import_gguf",
                           side_effect=self._slow_import):
            window.import_button.click()
            pump_until(lambda: window.import_worker is not None,
                       5000)
            self.assertTrue(
                window.import_cancel_button.isVisible())
            window.import_cancel_button.click()
            self.wait_import(window)
        self.assertIn("Import stopped",
                      window.statusBar().currentMessage())
        self.assertFalse((self.models / "elsewhere.gguf").exists())
        self.assertFalse(
            (self.models / "elsewhere.gguf.part").exists())


class BlockingRunner(FakeRunner):
    """A load that blocks 8 s without checking cancel, like the
    real backend's constructor: longer than the 5 s close wait."""

    def load(self, path, progress_cb=None, cancel_event=None,
             display_name=None):
        time.sleep(8)
        return super().load(path, progress_cb, cancel_event,
                            display_name)


class CloseDuringLoadTests(GuiTestCase):
    def test_close_during_a_load_waits_then_closes_cleanly(self):
        make_fake_gguf(self.models / "slow.gguf", 4096)
        window = self.make_window(runner=BlockingRunner())
        self.wait_scan(window)
        window.controller._free_ram_fn = lambda: 8 * GB
        path = sorted(window.model_cards)[0]
        window.model_cards[path].clicked.emit(path)
        window.load_button.click()
        QTimer.singleShot(1000, window.close)
        pending = pump_until(lambda: window._close_pending, 10000)
        self.assertTrue(pending,
                        "close was not deferred while loading")
        self.assertTrue(window.isVisible())
        closed = pump_until(lambda: not window.isVisible(), 10000)
        self.assertTrue(closed,
                        "window did not close after the load")
        # The test process surviving this is the proof: the old
        # code aborted the process (SIGABRT) here.


class SlowUncancellableRunner(FakeRunner):
    """A slow load that cannot be stopped, like the real backend."""

    cancellable_load = False


class NonCancellableLoadTests(GuiTestCase):
    def test_cancel_button_disabled_and_status_says_so(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        runner = SlowUncancellableRunner(load_delay_s=2.0)
        window = self.make_window(runner=runner)
        self.wait_scan(window)
        window.controller._free_ram_fn = lambda: 8 * GB
        path = sorted(window.model_cards)[0]
        window.model_cards[path].clicked.emit(path)
        window.load_button.click()
        pump_until(lambda: window.load_worker is not None, 5000)
        # Wait for the state, not a fixed sleep: the first
        # progress signal (which carries the "cannot be stopped"
        # line) arrives when the worker thread starts emitting,
        # and a fixed 300 ms wait was marginal under suite load.
        pump_until(lambda: "cannot be stopped" in
                   window.statusBar().currentMessage(), 5000)
        self.assertTrue(window.load_cancel_button.isVisible())
        self.assertFalse(window.load_cancel_button.isEnabled())
        self.assertIn("cannot be stopped",
                      window.statusBar().currentMessage())
        self.wait_load(window)


class PartListTests(GuiTestCase):
    def test_part_list_shows_resume_and_delete(self):
        server, url = start_range_server(PAYLOAD)
        self.servers.append(server)
        part = self.models / "model.gguf.part"
        part.write_bytes(PAYLOAD[:len(PAYLOAD) // 2])
        (self.models / "model.gguf.part.url").write_text(url)
        pull = PullWindow(self.models)
        self.windows.append(pull)
        pull.show()
        QTest.qWait(200)
        self.assertTrue(pull.parts_list.isVisible())
        self.assertEqual(pull.parts_list.count(), 1)
        pull.parts_list.setCurrentRow(0)
        pull.part_resume_button.click()
        done = pump_until(lambda: pull.worker is None, 5000)
        self.assertTrue(done, "resumed pull did not finish")
        self.assertTrue((self.models / "model.gguf").is_file())
        self.assertEqual(file_bytes(self.models / "model.gguf"),
                         PAYLOAD)
        self.assertEqual(pull.parts_list.count(), 0)

    def test_delete_clears_part_and_sidecars(self):
        part = self.models / "model.gguf.part"
        part.write_bytes(b"x" * 100)
        (self.models / "model.gguf.part.url").write_text(
            "http://example.com/m.gguf")
        (self.models / "model.gguf.part.etag").write_text("v1")
        pull = PullWindow(self.models)
        self.windows.append(pull)
        pull.show()
        QTest.qWait(200)
        self.assertEqual(pull.parts_list.count(), 1)
        pull.parts_list.setCurrentRow(0)
        pull.part_delete_button.click()
        QTest.qWait(100)
        self.assertFalse(part.exists())
        self.assertFalse(
            (self.models / "model.gguf.part.url").exists())
        self.assertFalse(
            (self.models / "model.gguf.part.etag").exists())
        self.assertFalse(pull.parts_list.isVisible())

    def test_resume_without_saved_url_says_so(self):
        part = self.models / "model.gguf.part"
        part.write_bytes(b"x" * 100)
        pull = PullWindow(self.models)
        self.windows.append(pull)
        pull.show()
        QTest.qWait(200)
        pull.parts_list.setCurrentRow(0)
        pull.part_resume_button.click()
        QTest.qWait(100)
        self.assertIn("cannot be resumed",
                      pull.status_label.text())
        self.assertIsNone(pull.worker)

    def test_no_parts_means_no_section(self):
        pull = PullWindow(self.models)
        self.windows.append(pull)
        pull.show()
        QTest.qWait(200)
        self.assertFalse(pull.parts_list.isVisible())


class KvCacheGuiTests(GuiTestCase):
    def test_card_tooltip_uses_real_headroom(self):
        write_gguf_file(
            self.models / "a.gguf",
            {"general.architecture": "llama"},
            numbers={"llama.block_count": 1,
                     "llama.attention.head_count": 1,
                     "llama.attention.head_count_kv": 1,
                     "llama.attention.key_length": 8,
                     "llama.attention.value_length": 8})
        window = self.make_window()
        self.wait_scan(window)
        card = window.model_cards[str(self.models / "a.gguf")]
        tip = card.toolTip()
        # Real headroom is ~64 MB (a tiny KV cache plus working
        # memory), not the 512 MB fixed fallback.
        self.assertIn("Needs 64", tip)
        self.assertNotIn("512 MB", tip)


class PullQueueTests(GuiTestCase):
    def make_pull_window(self):
        pull = PullWindow(self.models)
        self.windows.append(pull)
        pull.show()
        return pull

    def _url_for(self, server_url, name):
        # The fake server serves its payload at any path, so one
        # server stands in for several models.
        return server_url.replace("model.gguf", name)

    def test_queue_pulls_in_order(self):
        server, url = start_range_server(PAYLOAD)
        self.servers.append(server)
        pull = self.make_pull_window()
        pulled = []
        pull.model_pulled.connect(pulled.append)
        for name in ("one.gguf", "two.gguf", "three.gguf"):
            pull.input.setText(self._url_for(url, name))
            pull.queue_add_button.click()
        self.assertEqual(len(pull.queue), 3)
        self.assertTrue(pull.queue_list.isVisible())
        self.assertEqual(pull.queue_list.count(), 3)
        pull.queue_pull_button.click()
        ok = pump_until(
            lambda: len(pulled) == 3 and not pull._queue_running,
            10000)
        self.assertTrue(ok, "queue did not finish")
        self.assertEqual(pulled, [str(self.models / name)
                                  for name in ("one.gguf", "two.gguf",
                                               "three.gguf")])
        for name in ("one.gguf", "two.gguf", "three.gguf"):
            self.assertEqual(file_bytes(self.models / name), PAYLOAD)
        self.assertTrue(all(item["status"] == "done"
                            for item in pull.queue))
        self.assertIn("Queue done. 3 models pulled.",
                      pull.status_label.text())

    def test_queue_duplicate_refused(self):
        pull = self.make_pull_window()
        pull.input.setText("http://127.0.0.1:9/one.gguf")
        pull.queue_add_button.click()
        pull.input.setText("http://127.0.0.1:9/one.gguf")
        pull.queue_add_button.click()
        self.assertEqual(len(pull.queue), 1)
        self.assertIn("already in the queue",
                      pull.status_label.text())

    def test_queue_remove_drops_item(self):
        pull = self.make_pull_window()
        pull.input.setText("http://127.0.0.1:9/one.gguf")
        pull.queue_add_button.click()
        pull.input.setText("http://127.0.0.1:9/two.gguf")
        pull.queue_add_button.click()
        pull.queue_list.setCurrentRow(0)
        pull.queue_remove_button.click()
        self.assertEqual(len(pull.queue), 1)
        self.assertEqual(pull.queue[0]["target"].filename,
                         "two.gguf")

    def test_queue_failure_stops_queue(self):
        server, url = start_range_server(PAYLOAD)
        self.servers.append(server)
        pull = self.make_pull_window()
        pulled = []
        pull.model_pulled.connect(pulled.append)
        pull.input.setText(self._url_for(url, "one.gguf"))
        pull.queue_add_button.click()
        pull.input.setText("http://127.0.0.1:1/dead.gguf")
        pull.queue_add_button.click()
        pull.input.setText(self._url_for(url, "three.gguf"))
        pull.queue_add_button.click()
        pull.queue_pull_button.click()
        ok = pump_until(
            lambda: pull.worker is None and not pull._queue_running,
            10000)
        self.assertTrue(ok, "queue did not stop after the failure")
        self.assertEqual(pulled, [str(self.models / "one.gguf")])
        self.assertFalse((self.models / "three.gguf").exists())
        self.assertEqual([item["status"] for item in pull.queue],
                         ["done", "failed", "waiting"])
        # The failure modal appeared (the watcher dismissed it).
        self.assertTrue(self.watcher.seen)

    def test_queue_cancel_stops_queue(self):
        # Deterministic: drive the cancel handler with a running
        # queue state, instead of racing a real download.
        pull = self.make_pull_window()
        pull.input.setText("http://127.0.0.1:9/one.gguf")
        pull.queue_add_button.click()
        pull.input.setText("http://127.0.0.1:9/two.gguf")
        pull.queue_add_button.click()
        pull._queue_running = True
        pull._queue_item = pull.queue[0]
        pull.queue[0]["status"] = "downloading"
        pull._on_cancelled()
        self.assertFalse(pull._queue_running)
        self.assertEqual(pull.queue[0]["status"], "stopped")
        self.assertEqual(pull.queue[1]["status"], "waiting")

    def test_empty_queue_means_no_section(self):
        pull = self.make_pull_window()
        self.assertFalse(pull.queue_list.isVisible())


class PullEtaTests(GuiTestCase):
    def make_pull_window(self):
        pull = PullWindow(self.models)
        self.windows.append(pull)
        pull.show()
        return pull

    def test_eta_shows_time_left(self):
        pull = self.make_pull_window()
        pull._on_progress(0, 600 * 1024 * 1024, 1024 * 1024)
        self.assertIn("about 10 min left",
                      pull.progress_stats.text())

    def test_eta_is_smoothed(self):
        pull = self.make_pull_window()
        pull._on_progress(0, 600 * 1024 * 1024, 1024 * 1024)
        self.assertIn("about 10 min left",
                      pull.progress_stats.text())
        # The raw figure is now 1 minute, but the smoothed figure
        # is 0.7 * 600 + 0.3 * 60 = 438 s: about 7 min.
        pull._on_progress(540 * 1024 * 1024, 600 * 1024 * 1024,
                          1024 * 1024)
        self.assertIn("about 7 min left",
                      pull.progress_stats.text())

    def test_no_eta_without_speed(self):
        pull = self.make_pull_window()
        pull._on_progress(50 * 1024 * 1024, 100 * 1024 * 1024, 0)
        self.assertNotIn("left", pull.progress_stats.text())


class BlurValidationTests(GuiTestCase):
    def make_pull_window(self):
        pull = PullWindow(self.models)
        self.windows.append(pull)
        pull.show()
        return pull

    def test_bad_input_errors_on_blur(self):
        pull = self.make_pull_window()
        pull.input.setText("just some words here")
        pull.input.editingFinished.emit()
        self.assertIn("could not understand",
                      pull.status_label.text())

    def test_good_input_clears_error(self):
        pull = self.make_pull_window()
        pull.input.setText("just some words here")
        pull.input.editingFinished.emit()
        self.assertIn("could not understand",
                      pull.status_label.text())
        pull.input.setText("http://127.0.0.1:9/one.gguf")
        pull.input.editingFinished.emit()
        self.assertEqual(pull.status_label.text(),
                         "Nothing pulling yet.")

    def test_empty_input_clears_error(self):
        pull = self.make_pull_window()
        pull.input.setText("just some words here")
        pull.input.editingFinished.emit()
        pull.input.clear()
        pull.input.editingFinished.emit()
        self.assertEqual(pull.status_label.text(),
                         "Nothing pulling yet.")

    def test_validation_skips_during_pull(self):
        pull = self.make_pull_window()
        pull.worker = PullWorker("http://127.0.0.1:9/one.gguf",
                                 self.models / "one.gguf")
        try:
            pull.input.setText("just some words here")
            pull.input.editingFinished.emit()
            self.assertEqual(pull.status_label.text(),
                             "Nothing pulling yet.")
        finally:
            pull.worker = None


class RepullTests(GuiTestCase):
    def make_pull_window(self, **kwargs):
        pull = PullWindow(self.models, **kwargs)
        self.windows.append(pull)
        pull.show()
        return pull

    def test_repull_yes_replaces_file(self):
        payload1 = b"first" * 100
        payload2 = b"second" * 100
        server1, url1 = start_range_server(payload1)
        server2, url2 = start_range_server(payload2)
        self.servers.extend([server1, server2])
        pull = self.make_pull_window()
        pull.input.setText(url1)
        pull.pull_button.click()
        self.assertTrue(pump_until(lambda: pull.worker is None,
                                   10000),
                        "first pull did not finish")
        self.assertEqual(file_bytes(self.models / "model.gguf"),
                         payload1)
        pull.input.setText(url2)
        timer = self._answer_yes()
        try:
            pull.pull_button.click()
        finally:
            timer.stop()
            self.watcher.enabled = True
        self.assertTrue(pump_until(lambda: pull.worker is None,
                                   10000),
                        "re-pull did not finish")
        self.assertEqual(file_bytes(self.models / "model.gguf"),
                         payload2)

    def test_repull_loaded_model_refused(self):
        make_fake_gguf(self.models / "model.gguf", 2048)
        window = self.make_window()
        self.load_first_model(window)
        window.show_pull_window()
        pull = window.pull_window
        self.windows.append(pull)
        server, url = start_range_server(PAYLOAD)
        self.servers.append(server)
        pull.input.setText(url)
        pull.pull_button.click()
        QTest.qWait(300)
        self.assertIsNone(pull.worker)
        self.assertIn("Unload it first", pull.status_label.text())
        self.assertEqual(self.watcher.seen, [])


class FactsCardTests(GuiTestCase):
    def test_real_gguf_shows_facts_line(self):
        write_gguf_file(
            self.models / "real.gguf",
            {"general.architecture": "qwen3"},
            numbers={"qwen3.context_length": 32768},
            tensors=[("w", [2000, 1000000], 0)],
            file_type=15)
        window = self.make_window()
        self.wait_scan(window)
        card = window.model_cards[str(self.models / "real.gguf")]
        facts = card.findChild(QLabel, "model_facts")
        self.assertIsNotNone(facts)
        self.assertEqual(facts.text(), "2B · Q4_K_M · 32K context")
        self.assertIn("Architecture: qwen3", card.toolTip())

    def test_plain_file_shows_no_facts(self):
        make_fake_gguf(self.models / "plain.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        card = window.model_cards[str(self.models / "plain.gguf")]
        self.assertIsNone(card.findChild(QLabel, "model_facts"))


class SearchFilterTests(GuiTestCase):
    def _three_models(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        make_sparse_gguf(self.models / "beta.gguf",
                         int(1.5 * GB))
        make_sparse_gguf(self.models / "gamma.gguf", 4 * GB)
        window = self.make_window()
        self.wait_scan(window)
        return window

    def _visible_names(self, window):
        return sorted(window.model_cards[path].model_name
                      for path in window._visible_paths)

    def test_search_filters_by_name(self):
        window = self._three_models()
        window.search_box.setText("alp")
        self.assertEqual(self._visible_names(window),
                         ["alpha.gguf"])
        window.search_box.clear()
        self.assertEqual(len(window._visible_paths), 3)

    def test_search_is_case_insensitive(self):
        window = self._three_models()
        window.search_box.setText("BETA")
        self.assertEqual(self._visible_names(window),
                         ["beta.gguf"])

    def test_size_filter_buckets(self):
        window = self._three_models()
        window.filter_combo.setCurrentText("Small (under 1 GB)")
        self.assertEqual(self._visible_names(window),
                         ["alpha.gguf"])
        window.filter_combo.setCurrentText("Medium (1-3 GB)")
        self.assertEqual(self._visible_names(window),
                         ["beta.gguf"])
        window.filter_combo.setCurrentText("Large (over 3 GB)")
        self.assertEqual(self._visible_names(window),
                         ["gamma.gguf"])
        window.filter_combo.setCurrentText("All models")
        self.assertEqual(len(window._visible_paths), 3)

    def test_no_match_shows_plain_line(self):
        window = self._three_models()
        window.search_box.setText("zzz")
        self.assertTrue(window._filter_empty_label.isVisible())
        self.assertIn('No models match "zzz".',
                      window._filter_empty_label.text())
        self.assertFalse(window._cards_scroll.isVisible())
        window.search_box.clear()
        self.assertFalse(window._filter_empty_label.isVisible())
        self.assertTrue(window._cards_scroll.isVisible())

    def test_filtered_selection_disables_load_remove(self):
        window = self._three_models()
        path = str(self.models / "alpha.gguf")
        window.model_cards[path].clicked.emit(path)
        self.assertTrue(window.load_button.isEnabled())
        self.assertTrue(window.remove_button.isEnabled())
        window.search_box.setText("gamma")
        self.assertFalse(window.load_button.isEnabled())
        self.assertFalse(window.remove_button.isEnabled())
        window.search_box.clear()
        self.assertTrue(window.load_button.isEnabled())
        self.assertTrue(window.remove_button.isEnabled())

    def test_focus_moves_to_search_when_card_filtered_out(self):
        window = self._three_models()
        alpha = window.model_cards[str(self.models / "alpha.gguf")]
        alpha.setFocus()
        QTest.qWait(50)
        self.assertTrue(alpha.hasFocus())
        window.search_box.setText("gamma")
        QTest.qWait(50)
        self.assertTrue(window.search_box.hasFocus())


class DiffRefreshTests(GuiTestCase):
    def _many_models(self, count=12):
        for index in range(count):
            make_fake_gguf(self.models / ("model-%02d.gguf" % index),
                           2048)
        window = self.make_window()
        window.setMinimumSize(900, 200)
        window.resize(900, 300)
        self.wait_scan(window)
        return window

    def test_refresh_reuses_cards_keeps_scroll_and_focus(self):
        window = self._many_models()
        scroll = window._cards_scroll.verticalScrollBar()
        self.assertTrue(
            pump_until(lambda: scroll.maximum() > 0, 5000),
            "no scroll range with 12 cards in a short window")
        scroll.setValue(scroll.maximum())
        first = window.model_cards[sorted(window.model_cards)[0]]
        first.setFocus()
        QTest.qWait(50)
        self.assertTrue(first.hasFocus())
        before = dict(window.model_cards)
        saved_scroll = scroll.value()
        window._refresh_model_list()
        self.wait_scan(window)
        self.assertEqual(set(window.model_cards), set(before))
        for key, card in before.items():
            self.assertIs(window.model_cards[key], card,
                          "card rebuilt across a refresh: %s" % key)
        self.assertEqual(scroll.value(), saved_scroll)
        self.assertTrue(first.hasFocus())

    def test_changed_model_rebuilds_only_its_card(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        make_fake_gguf(self.models / "beta.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        alpha_before = window.model_cards[
            str(self.models / "alpha.gguf")]
        beta_before = window.model_cards[
            str(self.models / "beta.gguf")]
        make_fake_gguf(self.models / "alpha.gguf", 4096)
        window._refresh_model_list()
        self.wait_scan(window)
        alpha_after = window.model_cards[
            str(self.models / "alpha.gguf")]
        beta_after = window.model_cards[
            str(self.models / "beta.gguf")]
        self.assertIsNot(alpha_after, alpha_before)
        self.assertIs(beta_after, beta_before)


class AttachTests(GuiTestCase):
    def _picture(self):
        pic = self.base / "photo.png"
        pic.write_bytes(b"\x89PNG fake image bytes")
        return pic

    def _loaded_window(self, runner):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        return window

    def test_attach_disabled_without_vision(self):
        window = self._loaded_window(FakeRunner())
        self.assertFalse(window.attach_button.isEnabled())

    def test_attach_disabled_without_model(self):
        window = self.make_window(
            runner=FakeRunner(supports_vision=True))
        self.wait_scan(window)
        self.assertFalse(window.attach_button.isEnabled())

    def test_attach_and_send_image(self):
        runner = FakeRunner(reply_text="a cat",
                            supports_vision=True)
        window = self._loaded_window(runner)
        self.assertTrue(window.attach_button.isEnabled())
        pic = self._picture()
        with mock.patch(
                "fyrn.gui.QFileDialog.getOpenFileName",
                return_value=(str(pic), "")):
            window.attach_button.click()
        self.assertTrue(window.attachment_label.isVisible())
        self.assertIn("photo.png",
                      window.attachment_label.text())
        window.chat_input.setText("what is this?")
        window.send_button.click()
        self.wait_chat(window)
        self.assertEqual(runner.last_images, [str(pic)])
        self.assertIn("Image: photo.png",
                      window.transcript.toPlainText())
        self.assertFalse(window.attachment_label.isVisible())
        self.assertIsNone(window._attached_image)

    def test_remove_attachment(self):
        runner = FakeRunner(supports_vision=True)
        window = self._loaded_window(runner)
        pic = self._picture()
        with mock.patch(
                "fyrn.gui.QFileDialog.getOpenFileName",
                return_value=(str(pic), "")):
            window.attach_button.click()
        self.assertIsNotNone(window._attached_image)
        window.attachment_remove_button.click()
        self.assertIsNone(window._attached_image)
        self.assertFalse(window.attachment_label.isVisible())

    def test_vision_warning_in_load_status(self):
        class WarningRunner(FakeRunner):
            def load(self, path, progress_cb=None,
                     cancel_event=None, display_name=None):
                result = super().load(path, progress_cb,
                                      cancel_event, display_name)
                result["vision_warning"] = (
                    "This is a vision model, but its projector "
                    "file is not next to it.")
                return result

        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window(runner=WarningRunner())
        self.load_first_model(window)
        message = window.statusBar().currentMessage()
        self.assertIn("alpha.gguf loaded.", message)
        self.assertIn("projector file is not next to it", message)


class DragDropTests(GuiTestCase):
    # The mime data must outlive the event: the event holds a
    # pointer to it, and a mime object that dies while the event
    # is in use segfaults the process. Every test keeps its mime
    # in a local until the event is handled.
    def _mime(self, paths):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        return mime

    def _drag_event(self, mime):
        # The 5-argument constructor is the only one this PySide6
        # build offers, and it is marked deprecated: silence that
        # warning here, it is not something the test can fix.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return QDragEnterEvent(
                QPoint(10, 10), Qt.DropAction.CopyAction, mime,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier)

    def _drop_event(self, mime):
        return QDropEvent(
            QPoint(10, 10), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            QEvent.Type.Drop)

    def test_drag_enter_accepts_gguf_rejects_text(self):
        window = self.make_window()
        self.wait_scan(window)
        gguf = self.base / "dropped.gguf"
        gguf.write_bytes(b"x" * 100)
        txt = self.base / "notes.txt"
        txt.write_text("hi")
        mime = self._mime([gguf])
        event = self._drag_event(mime)
        window.dragEnterEvent(event)
        self.assertTrue(event.isAccepted())
        mime = self._mime([txt])
        event = self._drag_event(mime)
        window.dragEnterEvent(event)
        self.assertFalse(event.isAccepted())

    def test_drop_imports_file(self):
        window = self.make_window()
        self.wait_scan(window)
        gguf = self.base / "dropped.gguf"
        gguf.write_bytes(b"x" * 100)
        mime = self._mime([gguf])
        event = self._drop_event(mime)
        window.dropEvent(event)
        self.assertTrue(event.isAccepted())
        self.wait_import(window)
        self.wait_scan(window)
        self.assertTrue((self.models / "dropped.gguf").is_file())
        self.assertIn(str(self.models / "dropped.gguf"),
                      window.model_cards)

    def test_drop_two_files_imports_both(self):
        window = self.make_window()
        self.wait_scan(window)
        first = self.base / "first.gguf"
        second = self.base / "second.gguf"
        first.write_bytes(b"1" * 100)
        second.write_bytes(b"2" * 100)
        mime = self._mime([first, second])
        window.dropEvent(self._drop_event(mime))
        ok = pump_until(
            lambda: (self.models / "first.gguf").is_file()
            and (self.models / "second.gguf").is_file()
            and window.import_worker is None
            and not window._import_pending,
            10000)
        self.assertTrue(ok, "both dropped files did not import")


# ------------------------------------------------------- v0.5: polish


class FocusVsSelectionTests(GuiTestCase):
    def _two_cards(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        make_fake_gguf(self.models / "beta.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        return window

    def test_focused_card_differs_from_selected(self):
        window = self._two_cards()
        alpha = window.model_cards[str(self.models / "alpha.gguf")]
        beta = window.model_cards[str(self.models / "beta.gguf")]
        alpha.clicked.emit(alpha.path)
        beta.setFocus()
        QTest.qWait(100)
        self.assertTrue(beta.hasFocus())
        tokens = current_tokens()
        self.assertIn("2px solid %s" % tokens["accent"],
                      alpha.styleSheet())
        self.assertIn("2px dashed %s" % tokens["focus_ring"],
                      beta.styleSheet())
        self.assertNotEqual(alpha.styleSheet(), beta.styleSheet())

    def test_selected_and_focused_is_solid(self):
        # Selection wins when a card has both: Load acts on the
        # selection, so the selection is the signal that matters.
        window = self._two_cards()
        alpha = window.model_cards[str(self.models / "alpha.gguf")]
        alpha.clicked.emit(alpha.path)
        alpha.setFocus()
        QTest.qWait(100)
        self.assertTrue(alpha.hasFocus())
        self.assertIn("2px solid %s" % current_tokens()["accent"],
                      alpha.styleSheet())
        self.assertNotIn("dashed", alpha.styleSheet())


class SystemTextSizeTests(GuiTestCase):
    def test_large_system_font_scales_the_cards(self):
        app = QApplication.instance()
        old_font = app.font()
        try:
            app.setFont(QFont(app.font().family(), 22))
            make_fake_gguf(self.models / "alpha.gguf", 2048)
            window = self.make_window()
            self.wait_scan(window)
            card = window.model_cards[
                str(self.models / "alpha.gguf")]
            # The name label inherits the application font: no
            # fixed size overrides it.
            self.assertEqual(
                card._name_label.font().pointSize(), 22)
            # And the card grows with the font instead of
            # clipping: four lines at 22 pt is taller than four
            # lines at the default size.
            line = card.fontMetrics().lineSpacing()
            self.assertEqual(card.minimumHeight(), 4 * line + 32)
            self.assertGreater(card.minimumHeight(), 120)
        finally:
            app.setFont(old_font)

    def test_no_fixed_sizes_in_card_styles(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        card = window.model_cards[str(self.models / "alpha.gguf")]
        for label in card.findChildren(QLabel):
            self.assertNotIn("font-size", label.styleSheet())
        self.assertNotIn("font-size", card.styleSheet())
        self.assertNotIn("font-size",
                         QApplication.instance().styleSheet())


class ShortcutTests(GuiTestCase):
    def test_menu_shortcuts_are_set(self):
        window = self.make_window()
        by_text = {}
        for menu_action in window.menuBar().actions():
            menu = menu_action.menu()
            for action in menu.actions():
                by_text[action.text()] = action.shortcut().toString()
        expected = {
            "Settings": "Ctrl+,",
            "Quit": "Ctrl+Q",
            "Load selected model": "Ctrl+L",
            "Pull models": "Ctrl+P",
            "Import a GGUF file": "Ctrl+I",
            "Remove model": "Del",
            "Refresh list": "F5",
            "Unload model": "Ctrl+U",
            "Command palette": "Ctrl+K",
            "How to Use": "F1",
        }
        for text, shortcut in expected.items():
            self.assertEqual(by_text.get(text), shortcut, text)

    def test_ctrl_f_focuses_search(self):
        window = self.make_window()
        self.wait_scan(window)
        window.activateWindow()
        QTest.qWait(100)
        QTest.keyClick(window, Qt.Key.Key_F,
                       Qt.KeyboardModifier.ControlModifier)
        QTest.qWait(100)
        self.assertTrue(window.search_box.hasFocus())

    def test_ctrl_l_loads_selected_model(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        card = window.model_cards[str(self.models / "alpha.gguf")]
        card.clicked.emit(card.path)
        window.activateWindow()
        QTest.qWait(100)
        QTest.keyClick(window, Qt.Key.Key_L,
                       Qt.KeyboardModifier.ControlModifier)
        self.wait_load(window)
        self.assertTrue(window.controller.is_loaded)
        self.assertEqual(window.controller.current_name,
                         "alpha.gguf")


class TooltipTests(GuiTestCase):
    def _assert_all_buttons_have_tooltips(self, window):
        bare = [button.text() for button in
                window.findChildren(QPushButton)
                if not button.toolTip().strip()]
        self.assertEqual(bare, [])

    def test_main_window_buttons_all_have_tooltips(self):
        window = self.make_window()
        self.wait_scan(window)
        self._assert_all_buttons_have_tooltips(window)

    def test_pull_window_buttons_all_have_tooltips(self):
        pull = PullWindow(self.models)
        self.windows.append(pull)
        pull.show()
        self._assert_all_buttons_have_tooltips(pull)

    def test_settings_window_buttons_all_have_tooltips(self):
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        self._assert_all_buttons_have_tooltips(settings)


class PullProgressTests(GuiTestCase):
    def make_pull_window(self):
        pull = PullWindow(self.models)
        self.windows.append(pull)
        pull.show()
        QTest.qWait(100)
        return pull

    def test_stats_line_holds_all_four_numbers(self):
        pull = self.make_pull_window()
        pull._on_progress(25 * 1024 * 1024, 100 * 1024 * 1024,
                          1024 * 1024)
        stats = pull.progress_stats.text()
        self.assertIn("25%", stats)
        self.assertIn("25.0 MB", stats)
        self.assertIn("100 MB", stats)
        self.assertIn("1.0 MB per second", stats)
        self.assertIn("left", stats)
        self.assertTrue(pull.progress_stats.isVisible())
        self.assertIn("Downloading", pull.status_label.text())

    def test_busy_dot_shares_the_progress_row(self):
        pull = self.make_pull_window()
        # The dot only takes its place in the row when it is
        # shown (a hidden widget has no layout position).
        pull._set_busy(True)
        QTest.qWait(100)
        bar = pull.progress_bar.geometry()
        dot = pull.busy_dot.geometry()
        center = dot.center().y()
        self.assertGreaterEqual(center, bar.top())
        self.assertLessEqual(center, bar.bottom())

    def test_progress_bars_carry_no_text(self):
        pull = self.make_pull_window()
        self.assertFalse(pull.progress_bar.isTextVisible())
        window = self.make_window()
        self.assertFalse(window.load_progress.isTextVisible())

    def test_stats_line_hides_when_the_pull_ends(self):
        pull = self.make_pull_window()
        pull._on_progress(25 * 1024 * 1024, 100 * 1024 * 1024,
                          1024 * 1024)
        self.assertTrue(pull.progress_stats.isVisible())
        pull._on_finished(str(self.models / "done.gguf"))
        self.assertFalse(pull.progress_stats.isVisible())


class CommandPaletteTests(GuiTestCase):
    def _palette(self, window=None):
        window = window or self.make_window()
        palette = CommandPalette(window._commands(), parent=window)
        self.windows.append(palette)
        palette.show()
        QTest.qWait(100)
        return palette

    def test_commands_cover_the_app(self):
        window = self.make_window()
        titles = [title for title, _s, _a in window._commands()]
        for wanted in ("Load selected model", "Unload model",
                       "Pull models", "Import a GGUF file",
                       "Remove selected model", "Refresh list",
                       "Focus search", "Settings", "How to Use",
                       "Clear chat", "Save chat",
                       "Check for updates", "Quit"):
            self.assertIn(wanted, titles)
        self.assertEqual(len(titles), len(set(titles)))

    def test_filter_narrows_the_list(self):
        palette = self._palette()
        self.assertGreater(palette.list.count(), 5)
        palette.input.setText("load")
        QTest.qWait(100)
        texts = [palette.list.item(row).text()
                 for row in range(palette.list.count())]
        self.assertEqual(len(texts), 2)
        joined = " ".join(texts)
        self.assertIn("Load selected model", joined)
        self.assertIn("Unload model", joined)

    def test_enter_runs_the_selected_command(self):
        called = []
        window = self.make_window()
        commands = [("Do the thing", "", lambda: called.append(1)),
                    ("Do another thing", "",
                     lambda: called.append(2))]
        palette = CommandPalette(commands, parent=window)
        self.windows.append(palette)
        palette.show()
        QTest.qWait(100)
        palette.input.setText("another")
        QTest.qWait(100)
        QTest.keyClick(palette.input, Qt.Key.Key_Return)
        QTest.qWait(100)
        self.assertEqual(called, [2])

    def test_arrow_keys_move_through_the_list(self):
        palette = self._palette()
        self.assertEqual(palette.list.currentRow(), 0)
        QTest.keyClick(palette.input, Qt.Key.Key_Down)
        self.assertEqual(palette.list.currentRow(), 1)
        QTest.keyClick(palette.input, Qt.Key.Key_Up)
        self.assertEqual(palette.list.currentRow(), 0)


class ChimeMuteTests(GuiTestCase):
    def make_pull_window(self):
        pull = PullWindow(self.models)
        self.windows.append(pull)
        pull.show()
        return pull

    def test_muted_chime_does_not_play(self):
        set_chime_muted(True)
        pull = self.make_pull_window()
        with mock.patch("fyrn.chime.play_chime") as play:
            pull._play_chime()
            play.assert_not_called()

    def test_unmuted_chime_plays_on_finish(self):
        set_chime_muted(False)
        pull = self.make_pull_window()
        with mock.patch("fyrn.chime.play_chime") as play:
            pull._on_finished(str(self.models / "done.gguf"))
            play.assert_called_once()

    def test_settings_window_saves_the_mute(self):
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        settings.chime_check.setChecked(True)
        settings.save_button.click()
        self.assertTrue(get_chime_muted())


class ListMotionTests(GuiTestCase):
    def test_new_cards_stagger_in_then_settle(self):
        for name in ("alpha.gguf", "beta.gguf", "gamma.gguf"):
            make_fake_gguf(self.models / name, 2048)
        window = self.make_window()
        self.wait_scan(window)
        cards = list(window.model_cards.values())
        self.assertEqual(len(cards), 3)
        # At least one card is mid-fade (the stagger sets the
        # effect the moment the scan lands)...
        saw_effect = pump_until(
            lambda: any(card.graphicsEffect() is not None
                        for card in cards),
            2000)
        self.assertTrue(saw_effect, "no card ever had a fade")
        # ...and every effect is gone once the stagger settles.
        settled = pump_until(
            lambda: all(card.graphicsEffect() is None
                        for card in cards),
            5000)
        self.assertTrue(settled, "a card kept its fade effect")

    def test_stagger_skipped_under_reduced_motion(self):
        set_reduce_motion(True)
        for name in ("alpha.gguf", "beta.gguf"):
            make_fake_gguf(self.models / name, 2048)
        window = self.make_window()
        self.wait_scan(window)
        QTest.qWait(500)
        for card in window.model_cards.values():
            self.assertIsNone(card.graphicsEffect())

    def test_flip_flies_a_moved_card(self):
        for name in ("alpha.gguf", "beta.gguf"):
            make_fake_gguf(self.models / name, 2048)
        window = self.make_window()
        self.wait_scan(window)
        QTest.qWait(300)
        key = str(self.models / "alpha.gguf")
        card = window.model_cards[key]
        old = QRect(card.geometry())
        old.translate(0, -100)
        window._flip_cards({key: old})
        self.assertEqual(len(window._flip_anims), 1)
        ghosts = [child for child in window._cards_widget.children()
                  if isinstance(child, QLabel)
                  and child.pixmap() is not None]
        self.assertEqual(len(ghosts), 1)
        self.assertEqual(ghosts[0].geometry(), old)
        done = pump_until(lambda: not window._flip_anims, 2000)
        self.assertTrue(done, "the FLIP never finished")
        ghosts = [child for child in window._cards_widget.children()
                  if isinstance(child, QLabel)
                  and child.pixmap() is not None]
        self.assertEqual(ghosts, [])

    def test_flip_skipped_under_reduced_motion(self):
        set_reduce_motion(True)
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        key = str(self.models / "alpha.gguf")
        card = window.model_cards[key]
        old = QRect(card.geometry())
        old.translate(0, -100)
        window._flip_cards({key: old})
        self.assertEqual(window._flip_anims, [])

    def test_overscroll_glow_flashes_and_fades(self):
        # Models in the folder, so the grid (and its viewport,
        # the glow's parent) is visible at all.
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        window._flash_overscroll("top")
        self.assertTrue(window._overscroll_top.isVisible())
        done = pump_until(
            lambda: not window._overscroll_top.isVisible(), 2000)
        self.assertTrue(done, "the glow never faded")

    def test_wheel_past_the_top_flashes_the_top_glow(self):
        for name in ("a.gguf", "b.gguf", "c.gguf", "d.gguf",
                     "e.gguf", "f.gguf"):
            make_fake_gguf(self.models / name, 2048)
        window = self.make_window()
        self.wait_scan(window)
        QTest.qWait(200)
        viewport = window._cards_scroll.viewport()
        bar = window._cards_scroll.verticalScrollBar()
        bar.setValue(bar.minimum())
        event = QWheelEvent(
            QPoint(10, 10), QPoint(10, 10), QPoint(0, 0),
            QPoint(0, 120), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(viewport, event)
        QTest.qWait(100)
        self.assertTrue(window._overscroll_top.isVisible())


class DensityTests(GuiTestCase):
    def _twelve_models(self):
        for index in range(12):
            make_fake_gguf(self.models / ("m%02d.gguf" % index),
                           2048)

    def test_compact_cards_are_shorter_and_hide_detail(self):
        write_gguf_file(
            self.models / "alpha.gguf",
            {"general.description": "A model with a description."})
        window = self.make_window()
        self.wait_scan(window)
        card = window.model_cards[str(self.models / "alpha.gguf")]
        normal_height = card.height()
        self.assertFalse(card._desc_label.isHidden())
        card.set_compact(True)
        QTest.qWait(100)
        self.assertTrue(card._desc_label.isHidden())
        self.assertLess(card.height(), normal_height)

    def test_compact_grid_has_four_columns(self):
        set_compact_list(False)
        self.assertEqual(grid_columns(), 3)
        set_compact_list(True)
        self.assertEqual(grid_columns(), 4)

    def test_compact_fits_half_again_as_many_cards(self):
        # The bar: at least 50% more cards in the same window
        # height. Measured from real card heights and row counts:
        # 12 models are 4 rows of 3 normally, 3 rows of 4 in
        # compact, and each compact row is shorter too.
        self._twelve_models()
        set_compact_list(False)
        window = self.make_window()
        self.wait_scan(window)
        QTest.qWait(200)
        normal_height = next(
            iter(window.model_cards.values())).height()
        set_compact_list(True)
        window._apply_density()
        QTest.qWait(200)
        compact_height = next(
            iter(window.model_cards.values())).height()
        normal_rows = 4
        compact_rows = 3
        normal_total = normal_rows * (normal_height + 16)
        compact_total = compact_rows * (compact_height + 10)
        ratio = normal_total / compact_total
        self.assertGreaterEqual(
            ratio, 1.5,
            "compact fits only %.2fx as many cards" % ratio)

    def test_settings_window_saves_compact(self):
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        settings.compact_check.setChecked(True)
        settings.save_button.click()
        self.assertTrue(get_compact_list())


class HighContrastTests(GuiTestCase):
    def test_high_contrast_theme_applies_live(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        card = window.model_cards[str(self.models / "alpha.gguf")]
        card.clicked.emit(card.path)
        set_high_contrast(True)
        window._apply_theme()
        self.assertEqual(QApplication.instance().styleSheet(),
                         HIGH_CONTRAST_STYLESHEET)
        self.assertIn("2px solid %s" % HIGH_CONTRAST["accent"],
                      card.styleSheet())
        set_high_contrast(False)
        window._apply_theme()
        self.assertEqual(QApplication.instance().styleSheet(),
                         DARK_STYLESHEET)
        self.assertIn("2px solid %s" % DARK["accent"],
                      card.styleSheet())

    def test_settings_window_saves_high_contrast(self):
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        settings.contrast_check.setChecked(True)
        settings.save_button.click()
        self.assertTrue(get_high_contrast())


class RaiseListenerTests(GuiTestCase):
    def test_raise_socket_signals_the_window(self):
        import socket as stdlib_socket
        sock_path = self.base / "raise.sock"
        listener = RaiseListener(sock_path)
        raised = []
        listener.raise_requested.connect(lambda: raised.append(1))
        listener.start()
        try:
            ok = pump_until(
                lambda: sock_path.exists(), 2000)
            self.assertTrue(ok, "the listener never bound")
            # The socket file appearing does not mean the
            # listener is accepting yet: a connect in that
            # window is refused (a flake seen under suite
            # load). Retry the connect instead of connecting
            # once.
            client = None
            connected = False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    client = stdlib_socket.socket(
                        stdlib_socket.AF_UNIX,
                        stdlib_socket.SOCK_STREAM)
                    client.connect(str(sock_path))
                    connected = True
                    break
                except ConnectionRefusedError:
                    client.close()
                    client = None
                    QTest.qWait(100)
            self.assertTrue(connected,
                            "could not connect to the raise socket")
            client.sendall(b"raise")
            client.close()
            done = pump_until(lambda: bool(raised), 2000)
            self.assertTrue(done, "no raise signal arrived")
        finally:
            listener.requestInterruption()
            listener.wait(2000)


# ------------------------------------------------------------ v0.6
# The hands-on audit fixes (tasks 1-17) and the update check.


class SettingsDialogSizeTests(GuiTestCase):
    """Task 1: the Settings dialog opens at its full content
    size, not crushed to 560x220 with clipped labels and
    9 px slivers for buttons."""

    def test_settings_opens_at_full_content_size(self):
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        QTest.qWait(200)
        self.assertGreaterEqual(
            settings.height(), settings.minimumSizeHint().height())

    def test_settings_labels_and_buttons_are_not_clipped(self):
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        QTest.qWait(200)
        for label in settings.findChildren(QLabel):
            if not label.isVisible():
                continue
            # A word-wrapped label's sizeHint height assumes its
            # sizeHint width; at the dialog's real (wider) width
            # the text fits on fewer lines, so the height to beat
            # is heightForWidth at the actual width.
            if label.wordWrap():
                needed = label.heightForWidth(label.width())
            else:
                needed = label.sizeHint().height()
            self.assertGreaterEqual(
                label.height(), needed - 1,
                "clipped label: %r" % label.text()[:40])
        buttons = {button.text(): button for button in
                   settings.findChildren(QPushButton)}
        for name in ("Save", "Cancel"):
            self.assertIn(name, buttons)
            self.assertGreaterEqual(
                buttons[name].height(),
                buttons[name].sizeHint().height())
            self.assertLess(
                settings.height()
                - buttons[name].geometry().bottom(), 30)


class GridLayoutTests(GuiTestCase):
    """Task 3: at 56 models three cards piled on one spot, two
    hidden, one 8 rows out of place. The cause was the hover
    lift capturing a collapsed geometry mid-relayout and parking
    the card there forever."""

    def _settled_window(self, count):
        for index in range(count):
            make_fake_gguf(self.models / ("m%02d.gguf" % index),
                           2048)
        window = self.make_window()
        self.wait_scan(window)
        # Stagger (12 cards x 40 ms + 200 ms) and FLIP settle.
        QTest.qWait(1000)
        return window

    def test_56_cards_never_overlap(self):
        window = self._settled_window(56)
        cards = list(window.model_cards.values())
        self.assertEqual(len(cards), 56)
        rects = [card.geometry() for card in cards]
        for index, first in enumerate(rects):
            for second in rects[index + 1:]:
                self.assertFalse(
                    first.intersects(second),
                    "cards overlap: %s vs %s"
                    % (first, second))

    def test_56_cards_stay_in_row_major_order(self):
        window = self._settled_window(56)
        cards = list(window.model_cards.values())
        ordered = sorted(
            cards,
            key=lambda c: (c.geometry().y(), c.geometry().x()))
        names = [card.model_name for card in ordered]
        self.assertEqual(names, sorted(names, key=str.lower))

    def test_lift_re_syncs_to_the_true_cell(self):
        window = self._settled_window(3)
        cards = sorted(window.model_cards.values(),
                       key=lambda c: c.model_name)
        card = cards[1]
        true_cell = QRect(card.geometry())
        # The card sits at a stale spot (the collapsed geometry
        # the audit caught), and the lift starts from there.
        wrong = QRect(true_cell)
        wrong.translate(0, 50)
        card.setGeometry(wrong)
        card._start_lift()
        done = pump_until(
            lambda: (card._lift_anim is not None
                     and card._lift_anim.state()
                     == QAbstractAnimation.State.Stopped),
            5000)
        self.assertTrue(done, "the lift never finished")
        expected = QRect(true_cell)
        expected.translate(0, -4)
        self.assertEqual(card.geometry(), expected)

    def test_relayout_cancels_a_lift_in_flight(self):
        window = self._settled_window(3)
        card = sorted(window.model_cards.values(),
                      key=lambda c: c.model_name)[1]
        card._start_lift()
        self.assertIsNotNone(card._lift_anim)
        window._relayout_cards(window._visible_paths)
        self.assertIsNone(card._lift_anim)
        self.assertIsNone(card._lift_rest)


class PullWindowGrowthTests(GuiTestCase):
    """Task 4: the Pull window grows when the queue list
    appears, instead of crushing the lists into the space the
    empty window had."""

    def test_window_grows_when_the_queue_appears(self):
        pull = PullWindow(self.models)
        self.windows.append(pull)
        pull.show()
        QTest.qWait(200)
        before = pull.height()
        self.assertFalse(pull.queue_list.isVisible())
        pull.input.setText("https://example.invalid/one.gguf")
        pull.queue_add_button.click()
        QTest.qWait(200)
        self.assertTrue(pull.queue_list.isVisible())
        self.assertEqual(pull.queue_list.count(), 1)
        self.assertGreater(pull.height(), before)
        # The queue list itself is full height (its 120 px cap),
        # not crushed, and the window hugs its content: the
        # bottom-most visible widget ends near the window bottom.
        # (The status line sits above the queue list, so it is
        # not the bottom widget once the queue is showing.)
        self.assertEqual(pull.queue_list.height(), 120)
        bottom = max(
            widget.geometry().bottom()
            for widget in pull.findChildren(QWidget)
            if widget.parentWidget() is pull
            and widget.isVisibleTo(pull))
        self.assertLess(pull.height() - bottom, 30)


class HiddenModelLoadTests(GuiTestCase):
    """Task 5: Ctrl+L on a model the search is hiding refuses
    in plain words, the way Remove already did."""

    def test_ctrl_l_on_a_hidden_model_refuses(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        make_fake_gguf(self.models / "beta.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        window.controller._free_ram_fn = lambda: 8 * GB
        beta = str(self.models / "beta.gguf")
        window.model_cards[beta].clicked.emit(beta)
        window.search_box.setText("alpha")
        QTest.qWait(200)
        self.assertNotIn(beta, window._visible_paths)
        # The menu action (and its Ctrl+L shortcut) stays
        # enabled so the guard below is reached (task 6).
        self.assertTrue(window._load_action.isEnabled())
        window.load_selected_model()
        self.assertIn("hidden by the search",
                      window.statusBar().currentMessage())
        self.assertFalse(window.controller.is_loaded)
        self.assertIsNone(window.load_worker)


class MenuActionStateTests(GuiTestCase):
    """Task 6: the Models menu actions enable and disable with
    the state, the way the buttons already do."""

    def setUp(self):
        super().setUp()
        self._old_ollama = os.environ.get("OLLAMA_MODELS")
        self.store = make_ollama_store(
            self.base / "ollama-store", "tiny", "1b", "ab" * 32)
        os.environ["OLLAMA_MODELS"] = str(self.store)
        set_show_ollama_models(True)

    def tearDown(self):
        if self._old_ollama is None:
            os.environ.pop("OLLAMA_MODELS", None)
        else:
            os.environ["OLLAMA_MODELS"] = self._old_ollama
        super().tearDown()

    def test_no_selection_disables_load_remove_unload(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        self.assertFalse(window._load_action.isEnabled())
        self.assertFalse(window._remove_action.isEnabled())
        self.assertFalse(window._unload_action.isEnabled())
        self.assertTrue(window._pull_action.isEnabled())
        self.assertTrue(window._refresh_action.isEnabled())
        self.assertTrue(window._import_action.isEnabled())

    def test_selection_enables_load_and_remove(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        path = str(self.models / "alpha.gguf")
        window.model_cards[path].clicked.emit(path)
        self.assertTrue(window._load_action.isEnabled())
        self.assertTrue(window._remove_action.isEnabled())
        self.assertFalse(window._unload_action.isEnabled())

    def test_loaded_model_disables_load_enables_unload(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.load_first_model(window)
        self.assertFalse(window._load_action.isEnabled())
        self.assertTrue(window._unload_action.isEnabled())

    def test_ollama_selection_keeps_remove_enabled(self):
        # The button greys out for an Ollama model (it cannot be
        # removed), but the menu action — and its Del shortcut —
        # stays enabled so the user is told why in plain words.
        window = self.make_window()
        self.wait_scan(window)
        path = [key for key, card in window.model_cards.items()
                if card.model_name == "tiny:1b"][0]
        window.model_cards[path].clicked.emit(path)
        self.assertTrue(window._remove_action.isEnabled())
        self.assertFalse(window.remove_button.isEnabled())


class FocusTooltipTests(GuiTestCase):
    """Task 7: keyboard focus on a card shows its tooltip.
    showText from inside focusInEvent never renders (the event
    is still being delivered); the fix defers it by 100 ms."""

    def test_focus_shows_the_card_tooltip(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        card = window.model_cards[str(self.models / "alpha.gguf")]
        window.activateWindow()
        QTest.qWait(100)
        card.setFocus()
        shown = pump_until(QToolTip.isVisible, 2000)
        self.assertTrue(shown, "no tooltip appeared on focus")

    def test_focus_away_hides_the_tooltip(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        card = window.model_cards[str(self.models / "alpha.gguf")]
        window.activateWindow()
        QTest.qWait(100)
        card.setFocus()
        self.assertTrue(pump_until(QToolTip.isVisible, 2000))
        window.search_box.setFocus()
        QTest.qWait(300)
        self.assertFalse(QToolTip.isVisible())


class UnsupportedArchitectureGuiTests(GuiTestCase):
    """Task 8, through the window: loading an architecture the
    engine does not know fails with a modal that names the
    design, not the file. LFM3 was the real case until v0.7,
    when the engine bump made it load: the test now uses a
    synthetic architecture pinned into the unsupported set."""

    def test_unsupported_load_modal_names_the_design(self):
        write_gguf_file(
            self.models / "qwen99.gguf",
            {"general.architecture": "qwen99"})

        class FailingLlama:
            def __init__(self, model_path, n_threads, n_ctx,
                         verbose, chat_handler=None):
                raise ValueError(
                    "Failed to load model from file: %s"
                    % model_path)

        module = types.ModuleType("llama_cpp")
        module.Llama = FailingLlama
        window = self.make_window(runner=LlamaRunner())
        window.controller._free_ram_fn = lambda: 8 * GB
        with mock.patch.dict(sys.modules, {"llama_cpp": module}), \
                mock.patch("fyrn.runner."
                           "UNSUPPORTED_ARCHITECTURES",
                           frozenset({"qwen99"})):
            self.wait_scan(window)
            path = str(self.models / "qwen99.gguf")
            window.model_cards[path].clicked.emit(path)
            window.load_button.click()
            self.wait_load(window)
        self.assertFalse(window.controller.is_loaded)
        texts = [text for title, text in self.watcher.seen
                 if title == "Load model"]
        self.assertEqual(len(texts), 1)
        self.assertIn("QWEN99", texts[0])
        self.assertIn("does not support yet", texts[0])
        self.assertIn("file itself is fine", texts[0])
        self.assertNotIn("may be broken", texts[0])
        # No filesystem path in the message (a local card's name
        # is its filename, "qwen99.gguf" included, but the folder
        # path never appears).
        self.assertNotIn(str(self.models), texts[0])


class PullCloseTooltipTests(GuiTestCase):
    """Task 9: the Pull window's Close button says what closing
    does to a running download."""

    def test_close_tooltip_names_the_part_file(self):
        pull = PullWindow(self.models)
        self.windows.append(pull)
        pull.show()
        self.assertEqual(
            pull.close_button.toolTip(),
            "Close this window (a running download stops, "
            "keeping its part file)")


class SettingsSavedMessageTests(GuiTestCase):
    """Task 10: a settings-only save says "Settings saved."; a
    save that changed the folder says so once, and the
    settings message never clobbers it."""

    def test_settings_only_save_says_settings_saved(self):
        window = self.make_window()
        self.wait_scan(window)
        window.show_settings()
        settings = window.settings_window
        self.windows.append(settings)
        settings.save_button.click()
        QTest.qWait(100)
        self.assertEqual(window.statusBar().currentMessage(),
                         "Settings saved.")

    def test_folder_change_message_is_not_clobbered(self):
        window = self.make_window()
        self.wait_scan(window)
        new_folder = self.base / "elsewhere"
        new_folder.mkdir()
        make_fake_gguf(new_folder / "new.gguf", 1024)
        window.show_settings()
        settings = window.settings_window
        self.windows.append(settings)
        settings.folder_edit.setText(str(new_folder))
        settings.save_button.click()
        QTest.qWait(100)
        self.assertEqual(
            window.statusBar().currentMessage(),
            "Models folder is now %s." % new_folder)


class LoadStatusNameTests(GuiTestCase):
    """Task 11, through the window: the loading status names
    the Ollama model, never its blob hash."""

    def setUp(self):
        super().setUp()
        self._old_ollama = os.environ.get("OLLAMA_MODELS")
        self.store = make_ollama_store(
            self.base / "ollama-store", "tiny", "1b", "ab" * 32)
        os.environ["OLLAMA_MODELS"] = str(self.store)
        set_show_ollama_models(True)

    def tearDown(self):
        if self._old_ollama is None:
            os.environ.pop("OLLAMA_MODELS", None)
        else:
            os.environ["OLLAMA_MODELS"] = self._old_ollama
        super().tearDown()

    def test_loading_status_names_the_ollama_model(self):
        runner = GatedRunner()
        window = self.make_window(runner=runner)
        self.wait_scan(window)
        window.controller._free_ram_fn = lambda: 8 * GB
        # The sandbox's real Ollama store is also discovered, so
        # pick the test model by name, not by position.
        path = [key for key, card in window.model_cards.items()
                if card.model_name == "tiny:1b"][0]
        window.model_cards[path].clicked.emit(path)
        window.load_button.click()
        started = pump_until(runner.started.is_set, 5000)
        self.assertTrue(started, "the load never started")
        message = window.statusBar().currentMessage()
        self.assertIn("tiny:1b", message)
        self.assertNotIn("sha256", message)
        runner.release.set()
        self.wait_load(window)
        self.assertTrue(window.controller.is_loaded)


class GatedRunner(FakeRunner):
    """A load that waits to be released, so a test can read the
    loading status mid-load."""

    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def load(self, path, progress_cb=None, cancel_event=None,
             display_name=None):
        self.started.set()
        self.release.wait(timeout=10)
        return super().load(path, progress_cb, cancel_event,
                            display_name)


class HowToUseSettingsTests(GuiTestCase):
    """Task 12: the How to Use Settings section names all seven
    settings (it used to say three, then six)."""

    def test_settings_section_names_all_seven_settings(self):
        text = HOW_TO_USE_SECTIONS["Settings"]
        for name in ("models folder", "Ollama", "Reduce motion",
                     "Compact model list", "High-contrast",
                     "chime", "checks for a newer release"):
            self.assertIn(name, text)


class ChatExportTests(GuiTestCase):
    """Task 13 (minimum scope): save the chat to a file the
    user picks. No cross-session history."""

    def test_format_chat_export(self):
        text = format_chat_export(
            "You: hi\nQwen: hello", model_name="Qwen",
            when="2026-11-18 20:00")
        self.assertTrue(text.startswith(
            "Fyrn chat with Qwen\nSaved 2026-11-18 20:00\n\n"))
        self.assertLess(text.index("You: hi"),
                        text.index("Qwen: hello"))
        self.assertTrue(text.endswith("\n"))

    def test_format_chat_export_without_a_model(self):
        text = format_chat_export("You: hi", when="2026-11-18 20:00")
        self.assertIn("Fyrn chat with no model", text)

    def test_save_chat_writes_the_transcript_in_order(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.load_first_model(window)
        window.transcript.setPlainText(
            "You: hello there\nAlpha: hi back")
        dest = self.base / "chat.txt"
        with mock.patch(
                "fyrn.gui.QFileDialog.getSaveFileName",
                return_value=(str(dest), "")):
            window.save_chat_button.click()
        QTest.qWait(100)
        content = dest.read_text(encoding="utf-8")
        self.assertIn("Fyrn chat with alpha", content)
        self.assertLess(content.index("You: hello there"),
                        content.index("Alpha: hi back"))
        self.assertIn("Chat saved to",
                      window.statusBar().currentMessage())

    def test_save_chat_with_no_chat_says_so(self):
        window = self.make_window()
        with mock.patch(
                "fyrn.gui.QFileDialog.getSaveFileName",
                side_effect=AssertionError(
                    "the dialog must not open for an empty chat")):
            window._save_chat()
        self.assertIn("no chat to save yet",
                      window.statusBar().currentMessage())

    def test_save_chat_cancel_writes_nothing(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.load_first_model(window)
        window.transcript.setPlainText("You: hello")
        with mock.patch(
                "fyrn.gui.QFileDialog.getSaveFileName",
                return_value=("", "")):
            window._save_chat()
        self.assertEqual(list(self.base.glob("*.txt")), [])


class HintWordingTests(GuiTestCase):
    """Task 14: the folder hint reads as a sentence."""

    def test_folder_hint_wording(self):
        window = self.make_window()
        labels = [label for label in window.findChildren(QLabel)
                  if label.text().startswith("Models live")]
        self.assertEqual(len(labels), 1)
        self.assertEqual(
            labels[0].text(),
            "Models live in the folder set in the File menu, "
            "under Settings.")


class TabOrderTests(GuiTestCase):
    """Task 15: Tab never lands on the bare scroll area."""

    def test_scroll_area_is_out_of_the_tab_order(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.wait_scan(window)
        self.assertEqual(window._cards_scroll.focusPolicy(),
                         Qt.FocusPolicy.NoFocus)
        window.activateWindow()
        QTest.qWait(100)
        window.search_box.setFocus()
        QTest.qWait(50)
        for _ in range(12):
            focused = QApplication.focusWidget() or window
            QTest.keyClick(focused, Qt.Key.Key_Tab)
            QTest.qWait(50)
            self.assertIsNot(QApplication.focusWidget(),
                             window._cards_scroll)


class EmptyStateHintTests(GuiTestCase):
    """Task 16: the double-click hint hides with the empty
    state and returns with the models."""

    def test_hint_hidden_with_no_models_returns_with_models(self):
        window = self.make_window()
        self.wait_scan(window)
        self.assertFalse(window._hint_label.isVisible())
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window._refresh_model_list()
        self.wait_scan(window)
        self.assertTrue(window._hint_label.isVisible())


class CardWidthTests(GuiTestCase):
    """Task 17: one card is a third of the row, not the full
    width — the hero tile's full width stays meaningful."""

    def _first_card_width(self, count):
        for index in range(count):
            make_fake_gguf(
                self.models / ("model%d.gguf" % index), 2048)
        window = self.make_window()
        self.wait_scan(window)
        QTest.qWait(500)
        widths = [card.width()
                  for card in window.model_cards.values()]
        return window, widths

    def test_one_card_is_a_third_wide(self):
        _window, widths = self._first_card_width(1)
        self.assertEqual(len(widths), 1)
        self.assertLess(widths[0], 500)
        self.assertGreater(widths[0], 250)

    def test_card_width_does_not_depend_on_count(self):
        _one, one_width = self._first_card_width(1)
        _one.close()
        _three, three_widths = self._first_card_width(3)
        self.assertEqual(len(three_widths), 3)
        for width in three_widths:
            self.assertLessEqual(abs(width - one_width[0]), 5)

    def test_two_cards_share_the_row_equally(self):
        _window, widths = self._first_card_width(2)
        self.assertEqual(len(widths), 2)
        # Equal up to a pixel of rounding in the column split.
        self.assertLessEqual(abs(widths[0] - widths[1]), 1)


class UpdateCheckGuiTests(GuiTestCase):
    """The update check through the window: silent when it
    should be, plain when it should speak."""

    def _result(self, state, latest="0.8", reason=None):
        result = {"state": state, "latest": latest,
                  "url": "https://example.invalid/releases",
                  "zip_url": "https://example.invalid/f.zip",
                  "zip_name": "f.zip",
                  "sha256_url":
                      "https://example.invalid/f.zip.sha256"}
        if reason is not None:
            result["reason"] = reason
        return result

    def _check_and_wait(self, window, result, manual):
        with mock.patch("fyrn.updates.check_for_update",
                        return_value=result):
            window.check_for_updates(manual=manual)
            done = pump_until(
                lambda: window._update_check_worker is None, 5000)
        self.assertTrue(done, "the check never finished")

    def test_manual_check_current_reports_up_to_date(self):
        window = self.make_window()
        self._check_and_wait(
            window, self._result("current", "0.7"), manual=True)
        texts = [text for title, text in self.watcher.seen
                 if title == "Check for updates"]
        self.assertEqual(len(texts), 1)
        self.assertIn("up to date", texts[0])
        self.assertIn("0.7", texts[0])

    def test_manual_check_unknown_reports_the_reason(self):
        window = self.make_window()
        self._check_and_wait(
            window,
            self._result("unknown", None,
                         reason="could not reach GitHub: offline"),
            manual=True)
        texts = [text for title, text in self.watcher.seen
                 if title == "Check for updates"]
        self.assertEqual(len(texts), 1)
        self.assertIn("Could not check for updates", texts[0])
        self.assertIn("offline", texts[0])

    def test_manual_check_newer_asks_and_no_means_no_download(self):
        window = self.make_window()
        self._check_and_wait(
            window, self._result("newer"), manual=True)
        texts = [text for title, text in self.watcher.seen
                 if title == "Update available"]
        self.assertEqual(len(texts), 1)
        self.assertIn("Fyrn 0.8 is out", texts[0])
        self.assertIn("You have 0.7", texts[0])
        # The watcher clicks the default (No): nothing downloads.
        QTest.qWait(200)
        self.assertIsNone(window._update_download_worker)

    def test_silent_check_current_is_silent(self):
        window = self.make_window()
        self._check_and_wait(
            window, self._result("current", "0.7"), manual=False)
        self.assertEqual(self.watcher.seen, [])

    def test_silent_check_unknown_is_silent(self):
        window = self.make_window()
        self._check_and_wait(
            window,
            self._result("unknown", None,
                         reason="could not reach GitHub: offline"),
            manual=False)
        self.assertEqual(self.watcher.seen, [])

    def test_silent_check_newer_still_asks(self):
        window = self.make_window()
        self._check_and_wait(
            window, self._result("newer"), manual=False)
        self.assertTrue(any(title == "Update available"
                            for title, _text in self.watcher.seen))

    def test_second_check_while_checking_says_so(self):
        window = self.make_window()

        def slow_check(_version):
            time.sleep(1.0)
            return self._result("current", "0.7")

        with mock.patch("fyrn.updates.check_for_update",
                        side_effect=slow_check):
            window.check_for_updates(manual=True)
            started = pump_until(
                lambda: window._update_check_worker is not None,
                2000)
            self.assertTrue(started)
            window.check_for_updates(manual=True)
            self.assertIn("Already checking for updates",
                          window.statusBar().currentMessage())
            done = pump_until(
                lambda: window._update_check_worker is None, 5000)
        self.assertTrue(done, "the check never finished")

    def test_update_check_setting_defaults_on_and_saves(self):
        self.assertTrue(get_update_check())
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        self.assertTrue(settings.update_check.isChecked())
        settings.update_check.setChecked(False)
        settings.save_button.click()
        QTest.qWait(100)
        self.assertFalse(get_update_check())
        second = SettingsWindow(self.models)
        self.windows.append(second)
        second.show()
        self.assertFalse(second.update_check.isChecked())


def _make_update_zip():
    """A small fake release zip, shaped like Fyrn's."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        info = zipfile.ZipInfo("fyrn/fyrn/__init__.py")
        info.external_attr = 0o100644 << 16
        zf.writestr(info, '__version__ = "0.7"\n')
        info = zipfile.ZipInfo("fyrn/fyrn/gui.py")
        info.external_attr = 0o100644 << 16
        zf.writestr(info, "# new gui\n")
    return buffer.getvalue()


class UpdateDownloadGuiTests(GuiTestCase):
    """The download half, end to end against local servers:
    the zip from a range server, the sidecar from a plain one."""

    def _serve_text(self, text):
        """A plain local server answering every GET with text."""
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever,
                                  daemon=True)
        thread.start()
        self.servers.append(server)
        return ("http://127.0.0.1:%d/sidecar"
                % server.server_address[1])

    def _release(self, zip_bytes, sidecar_text):
        zip_server, zip_url = start_range_server(zip_bytes)
        self.servers.append(zip_server)
        return {"state": "newer", "latest": "0.7",
                "url": "https://example.invalid/releases",
                "zip_url": zip_url.replace("model.gguf",
                                           "fyrn-v0.7.zip"),
                "zip_name": "fyrn-v0.7.zip",
                "sha256_url": self._serve_text(sidecar_text)}

    def test_download_verifies_stages_and_asks_to_restart(self):
        zip_bytes = _make_update_zip()
        digest = hashlib.sha256(zip_bytes).hexdigest()
        release = self._release(
            zip_bytes, "%s  fyrn-v0.7.zip\n" % digest)
        window = self.make_window()
        window._start_update_download(release)
        asked = pump_until(
            lambda: any(title == "Update ready"
                        for title, _text in self.watcher.seen),
            10000)
        self.assertTrue(asked, "no restart question appeared")
        # The watcher clicks the default (No): the update waits
        # for the next restart, staged and verified.
        self.assertIn("will install when Fyrn restarts",
                      window.statusBar().currentMessage())
        record = pending_update(window._home)
        self.assertIsNotNone(record)
        self.assertEqual(record["version"], "0.7")
        self.assertEqual(record["zip_sha256"], digest)

    def test_yes_at_restart_applies_and_restarts(self):
        zip_bytes = _make_update_zip()
        digest = hashlib.sha256(zip_bytes).hexdigest()
        release = self._release(
            zip_bytes, "%s  fyrn-v0.7.zip\n" % digest)
        window = self.make_window()
        app = QApplication.instance()
        quit_calls = []
        app.quit = lambda: quit_calls.append(True)
        try:
            with mock.patch(
                    "fyrn.updates.apply_pending_update",
                    return_value=(True, "0.7")) as apply_mock, \
                    mock.patch(
                        "fyrn.updates.restart_after_exit") \
                    as restart_mock:
                timer = self._answer_yes()
                try:
                    window._start_update_download(release)
                    done = pump_until(
                        lambda: restart_mock.called, 10000)
                finally:
                    timer.stop()
                    self.watcher.enabled = True
        finally:
            del app.quit
        self.assertTrue(done, "no restart was requested")
        apply_mock.assert_called_once_with(window._home)
        restart_mock.assert_called_once()
        self.assertEqual(restart_mock.call_args[0][0],
                         [sys.executable, "-m", "fyrn"])
        self.assertEqual(quit_calls, [True])

    def test_checksum_mismatch_is_refused(self):
        zip_bytes = _make_update_zip()
        release = self._release(
            zip_bytes, "%s  fyrn-v0.7.zip\n" % ("0" * 64))
        window = self.make_window()
        window._start_update_download(release)
        warned = pump_until(
            lambda: any(title == "Update"
                        for title, _text in self.watcher.seen),
            10000)
        self.assertTrue(warned, "no failure warning appeared")
        texts = [text for title, text in self.watcher.seen
                 if title == "Update"]
        self.assertIn("checksum", texts[0].lower())
        # Update words, not pull words: the download helper's
        # message says "not saved as a model. Try pulling it
        # again", which is wrong for an update.
        self.assertNotIn("pulling", texts[0].lower())
        self.assertNotIn("as a model", texts[0])
        self.assertIsNone(pending_update(window._home))

    def test_release_without_a_sidecar_is_refused(self):
        zip_bytes = _make_update_zip()
        digest = hashlib.sha256(zip_bytes).hexdigest()
        release = self._release(
            zip_bytes, "%s  fyrn-v0.7.zip\n" % digest)
        release["sha256_url"] = None
        window = self.make_window()
        window._start_update_download(release)
        warned = pump_until(
            lambda: any(title == "Update"
                        for title, _text in self.watcher.seen),
            10000)
        self.assertTrue(warned, "no failure warning appeared")
        texts = [text for title, text in self.watcher.seen
                 if title == "Update"]
        self.assertIn("no checksum", texts[0])
        self.assertIsNone(pending_update(window._home))

    def test_cancel_stops_the_download(self):
        zip_bytes = _make_update_zip()
        digest = hashlib.sha256(zip_bytes).hexdigest()
        release = self._release(
            zip_bytes, "%s  fyrn-v0.7.zip\n" % digest)

        def blocking_download(url, dest, progress_cb=None,
                              cancel_event=None,
                              expected_sha256=None):
            cancel_event.wait(timeout=10)
            raise PullCancelled("cancelled")

        window = self.make_window()
        with mock.patch("fyrn.pull.download_file",
                        side_effect=blocking_download):
            window._start_update_download(release)
            QTest.qWait(500)
            window._cancel_update_download()
            done = pump_until(
                lambda: window._update_download_worker is None,
                5000)
        self.assertTrue(done, "the download never cancelled")
        self.assertIn("cancelled",
                      window.statusBar().currentMessage())
        self.assertIsNone(pending_update(window._home))

    def test_restart_waits_when_a_model_is_busy(self):
        window = self.make_window()
        window.load_worker = mock.Mock()
        try:
            with mock.patch(
                    "fyrn.updates.apply_pending_update") \
                    as apply_mock:
                window._apply_update_and_restart("0.7")
                apply_mock.assert_not_called()
        finally:
            window.load_worker = None
        self.assertIn("will install when Fyrn restarts",
                      window.statusBar().currentMessage())


if __name__ == "__main__":
    unittest.main()
