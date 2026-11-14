"""GUI tests for Fyrn, run offscreen with a fake runner and
fake .gguf files. No real model, no network (pulls use a local
fake HTTP server)."""

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QPoint, QBuffer, QIODevice, QRect, QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QWidget)

from helpers import ModalWatcher, file_bytes, make_fake_gguf
from helpers import start_range_server
from fyrn.core import HEADROOM_BYTES, fit_status, read_ram_bytes
from fyrn.gui import (DARK_STYLESHEET, FIRST_PULL_SUGGESTION,
                            FIT_BADGES, HOW_TO_USE_SECTIONS, HowToUseWindow,
                            MainWindow, ModelCard, PressDipButton, PullWindow,
                            PullWorker, SettingsWindow, SingleInstance,
                            SkeletonCard, TURN_DIVIDER, get_models_folder,
                            get_reduce_motion, reduced_motion,
                            set_models_folder, set_reduce_motion)
from fyrn.pull import parse_model_input
from fyrn.runner import FakeRunner

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

    def load(self, path, progress_cb=None, cancel_event=None):
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
        os.environ["FYRN_HOME"] = str(self.home)
        os.environ["FYRN_SETTINGS_FILE"] = str(self.settings_file)
        set_models_folder(self.models)
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
                         ["Settings", "Debug report", "Quit"])
        models_items = [a.text() for a in menus["Models"].actions()]
        self.assertEqual(models_items,
                         ["Pull models", "Import a GGUF file",
                          "Remove model", "Refresh list",
                          "Unload model"])
        help_items = [a.text() for a in menus["Help"].actions()]
        self.assertEqual(help_items, ["How to Use", "About"])

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
        # Expected badges come from the core's own fit check against
        # free RAM read just before the scan: a 2 KB file is "Fits"
        # on a roomy machine but genuinely "Tight" when free RAM is
        # under ~683 MB, and the test must not pretend otherwise.
        make_fake_gguf(self.models / "small.gguf", 2048)
        _total, free = read_ram_bytes()
        make_sparse_gguf(self.models / "huge.gguf", free + 4 * GB)
        window = self.make_window()
        self.wait_scan(window)
        small = window.model_cards[str(self.models / "small.gguf")]
        huge = window.model_cards[str(self.models / "huge.gguf")]
        self.assertEqual(
            small.findChild(QLabel, "fit_badge").text(),
            FIT_BADGES[fit_status(2048, free)][0])
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
        self.assertEqual(hero.minimumHeight(), 120)

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
        self.assertEqual(card.minimumHeight(), 96)
        self.assertEqual(card.maximumHeight(), 120)
        self.assertLess(card.height(), 130)

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
            QTest.qWait(100)
            self.assertTrue(show.called,
                            "focusing a card did not show its detail")
            shown = show.call_args[0][1]
            self.assertIn("Full path:", shown)
            beta.setFocus()
            QTest.qWait(100)
            self.assertTrue(hide.called,
                            "leaving a card did not hide its detail")


class MicroInteractionTests(GuiTestCase):
    def test_press_dip_shrinks_and_restores(self):
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
        QTest.qWait(120)  # past the 75 ms down animation
        dipped = button.geometry()
        self.assertLess(dipped.width(), rest.width())
        self.assertLess(dipped.height(), rest.height())
        self.assertEqual(dipped.center(), rest.center())
        QTest.mouseRelease(button, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier,
                           button.rect().center())
        QTest.qWait(150)  # past the 75 ms return animation
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
        window, alpha, beta = self._two_cards()
        rest_y = alpha.geometry().y()
        QTest.mouseMove(alpha, alpha.rect().center())
        QTest.qWait(300)  # past the 200 ms lift
        self.assertEqual(alpha.geometry().y(), rest_y - 4)
        QTest.mouseMove(beta, beta.rect().center())
        QTest.qWait(300)  # past the 200 ms drop
        self.assertEqual(alpha.geometry().y(), rest_y)

    def test_card_lift_does_not_bob_in_bottom_strip(self):
        # The cursor parked in the bottom 4 px: the lift moves the
        # card out from under it, which must not bob the card up
        # and down forever.
        window, alpha, _beta = self._two_cards()
        rest_y = alpha.geometry().y()
        strip = QPoint(alpha.width() // 2, alpha.height() - 2)
        QTest.mouseMove(alpha, strip)
        QTest.qWait(300)
        first = alpha.geometry().y()
        self.assertEqual(first, rest_y - 4)
        QTest.qWait(300)
        second = alpha.geometry().y()
        self.assertEqual(second, first, "card bobbed in the strip")
        # Moving truly away drops it (the hover timer catches the
        # exit, since no leave event fires from the strip).
        QTest.mouseMove(window, QPoint(550, 400))
        QTest.qWait(500)
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
        QTest.qWait(100)
        name = [label for label in card.findChildren(QLabel)
                if label.text() == "alpha.gguf"][0]
        # The label's right end, past the text: card background.
        point = name.mapTo(
            card, QPoint(name.width() - 5, name.height() // 2))
        image = card.grab().toImage()
        self.assertEqual(image.pixel(point), QColor("#1c2836").rgb())

    def test_empty_state_and_skeleton_labels_transparent(self):
        window = self.make_window()
        self.wait_scan(window)
        self.assertIn(
            "QFrame#empty_state QLabel { "
            "background-color: transparent; }",
            window._empty_state.styleSheet())
        skeleton = SkeletonCard()
        self.assertIn(
            "SkeletonCard QLabel { background-color: transparent; }",
            skeleton.styleSheet())
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
                for _title, text in self.watcher.seen),
            "no remove confirm naming the file and size appeared")

    def test_remove_yes_deletes_file(self):
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
        self.assertIn("removed",
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
        pull = self.make_pull_window()
        pull.input.setText("just some words here")
        pull.pull_button.click()
        QTest.qWait(300)
        self.assertIsNone(pull.worker)
        self.assertTrue(self.watcher.seen)

    def test_pull_already_there(self):
        make_fake_gguf(self.models / "model.gguf", 1024)
        server, url = start_range_server(PAYLOAD)
        self.servers.append(server)
        pull = self.make_pull_window()
        pull.input.setText(url)
        pull.pull_button.click()
        QTest.qWait(300)
        self.assertIsNone(pull.worker)
        self.assertTrue(any("already in your models folder" in text
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


class HowToUseTests(GuiTestCase):
    def test_all_sections_present_and_readable(self):
        howto = HowToUseWindow()
        self.windows.append(howto)
        howto.show()
        self.assertEqual(howto.section_list.count(), 6)
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


if __name__ == "__main__":
    unittest.main()
