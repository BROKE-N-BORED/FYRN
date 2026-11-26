"""GUI tests for the v0.12 repairs: the one-tap Phone deck
(the Models tab button that turns the bridge and sharing on
for real, and a deck dialog that only shows a code for a
bridge verified up and shared) and the Settings pages that
replaced the one long scrolling column.

Same sandbox as test_gui.py: a temp home, a temp models
folder, FakeRunner everywhere, and a stand-in bridge service
that records what the window asks of it. The real bind
behavior and the real one-tap flow are covered by the
parent's drives; test_bridge.py covers the service itself.
"""

import unittest
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea

import fyrn.gui as gui
from fyrn.gui import (DARK_STYLESHEET, PhoneDeckDialog,
                      SettingsWindow, get_bridge_enabled,
                      get_bridge_share, get_compact_list,
                      get_reply_length, set_bridge_enabled,
                      set_bridge_share)
from test_gui import GuiTestCase

DECK_URL = "http://192.168.1.42:11434/deck"

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance()
    if _app is None:
        _app = QApplication([])
    _app.setStyleSheet(DARK_STYLESHEET)


class FakeBridgeService:
    """Records what the window asks of the bridge without
    binding a port. start() reads the share_fn the window
    passed, like the real service reads its bind address."""

    CREATED = []

    def __init__(self, controller, list_models_fn, **kwargs):
        self.controller = controller
        self.kwargs = kwargs
        self.generating = False
        self.start_error = ""
        self.started = False
        self.stopped = False
        self.shared = False
        self.port = 11434
        FakeBridgeService.CREATED.append(self)

    def start(self):
        self.started = True
        share_fn = self.kwargs.get("share_fn")
        self.shared = bool(share_fn and share_fn())
        return True

    def stop(self):
        self.stopped = True

    @property
    def running(self):
        return self.started and not self.stopped


class DeadBridgeService(FakeBridgeService):
    """A bridge whose start always fails, the way a taken
    port makes the real one fail."""

    def start(self):
        self.start_error = (
            "port 11434 is already in use. If Ollama is "
            "running, close it, or turn the bridge off and "
            "on again.")
        return False


class FakeServiceTestCase(GuiTestCase):
    def setUp(self):
        super().setUp()
        FakeBridgeService.CREATED.clear()
        self._real_service = gui.BridgeService
        gui.BridgeService = FakeBridgeService
        self.addCleanup(self._restore_service)

    def _restore_service(self):
        gui.BridgeService = self._real_service


class OneTapEnableTests(FakeServiceTestCase):
    def test_main_window_has_a_phone_deck_button(self):
        window = self.make_window()
        self.assertEqual(window.phone_deck_button.text(),
                         "Phone deck")
        self.assertTrue(
            window.phone_deck_button.toolTip())

    def test_one_tap_from_fresh_state_persists_and_shares(self):
        # Fresh profile state: bridge off, sharing off, no
        # service. One tap turns both on, saves both, and
        # starts the bridge shared.
        self.assertFalse(get_bridge_enabled())
        self.assertFalse(get_bridge_share())
        window = self.make_window()
        self.assertIsNone(window._bridge)
        service = window._enable_phone_deck()
        self.assertIsNotNone(service)
        self.assertTrue(get_bridge_enabled())
        self.assertTrue(get_bridge_share())
        self.assertIs(window._bridge, service)
        self.assertTrue(service.shared)
        self.assertEqual(len(FakeBridgeService.CREATED), 1)

    def test_one_tap_rebinds_an_unshared_bridge(self):
        # The bridge was already on, this PC only: the tap
        # saves sharing and restarts it shared.
        set_bridge_enabled(True)
        window = self.make_window()
        first = window._bridge
        self.assertFalse(first.shared)
        service = window._enable_phone_deck()
        self.assertTrue(first.stopped)
        self.assertIsNotNone(service)
        self.assertIsNot(service, first)
        self.assertTrue(service.shared)
        self.assertTrue(get_bridge_share())
        self.assertEqual(len(FakeBridgeService.CREATED), 2)

    def test_failed_start_reports_its_reason(self):
        gui.BridgeService = DeadBridgeService
        window = self.make_window()
        service = window._enable_phone_deck()
        self.assertIsNone(service)
        self.assertIsNone(window._bridge)
        self.assertIn("already in use",
                      window._bridge_start_error)
        # The settings were still saved: the failure is the
        # port, not the tap, and the dialog says which.
        self.assertTrue(get_bridge_enabled())
        self.assertTrue(get_bridge_share())


class StopSharingTests(FakeServiceTestCase):
    def test_stop_sharing_persists_off_and_rebinds_local(self):
        window = self.make_window()
        service = window._enable_phone_deck()
        self.assertTrue(service.shared)
        window._stop_phone_deck_sharing()
        self.assertFalse(get_bridge_share())
        # The bridge keeps its own state: still on, now
        # serving this PC only.
        self.assertTrue(get_bridge_enabled())
        self.assertTrue(service.stopped)
        self.assertIsNotNone(window._bridge)
        self.assertFalse(window._bridge.shared)

    def test_dialog_stop_button_stops_sharing(self):
        window = self.make_window()
        window._enable_phone_deck()
        dialog = PhoneDeckDialog(
            DECK_URL, window,
            on_stop_sharing=window._stop_phone_deck_sharing)
        self.windows.append(dialog)
        dialog.show()
        self.assertTrue(hasattr(dialog, "qr_label"))
        dialog.stop_button.click()
        QTest.qWait(50)
        self.assertFalse(get_bridge_share())
        self.assertIn("Sharing is off",
                      dialog.url_label.text())
        self.assertFalse(dialog.qr_label.isVisible())
        self.assertFalse(dialog.stop_button.isVisible())


class ProblemDialogTests(GuiTestCase):
    def test_problem_dialog_shows_reason_and_no_code(self):
        problem = ("port 11434 is already in use. If Ollama "
                   "is running, close it, or turn the bridge "
                   "off and on again.")
        dialog = PhoneDeckDialog(None, problem=problem)
        self.windows.append(dialog)
        dialog.show()
        self.assertEqual(dialog.url_label.text(), problem)
        self.assertIn("could not start sharing",
                      dialog.help_label.text())
        self.assertFalse(hasattr(dialog, "qr_label"))
        self.assertFalse(dialog.retry_requested)
        dialog.retry_button.click()
        QTest.qWait(50)
        self.assertTrue(dialog.retry_requested)


class SettingsPageTests(GuiTestCase):
    def _on_page(self, widget, page):
        parent = widget.parent()
        while parent is not None:
            if parent is page:
                return True
            parent = parent.parent()
        return False

    def test_four_pages_in_order(self):
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        names = [settings.page_list.item(row).text()
                 for row in range(settings.page_list.count())]
        self.assertEqual(names, ["Models and downloads",
                                 "Chat and tools",
                                 "Bridge and phone",
                                 "Display and updates"])
        self.assertEqual(settings.pages.count(), 4)
        self.assertEqual(len(settings.page_widgets), 4)

    def test_no_whole_dialog_scroll(self):
        # The v0.11 defect: the settings were one long
        # scrolling column. The pages replaced it; no scroll
        # area remains in the dialog.
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        self.assertEqual(settings.findChildren(QScrollArea),
                         [])

    def test_controls_live_on_their_pages(self):
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        for widget in (settings.folder_edit,
                       settings.ollama_check,
                       settings.chime_check):
            self.assertTrue(
                self._on_page(widget, settings.models_page),
                widget)
        for widget in (settings.tools_folder_edit,
                       settings.forge_path_edit,
                       settings.reply_length_combo,
                       settings.idle_combo):
            self.assertTrue(
                self._on_page(widget, settings.chat_page),
                widget)
        for widget in (settings.bridge_check,
                       settings.share_check,
                       settings.deck_url_edit,
                       settings.deck_button):
            self.assertTrue(
                self._on_page(widget, settings.bridge_page),
                widget)
        for widget in (settings.motion_check,
                       settings.compact_check,
                       settings.contrast_check,
                       settings.update_check):
            self.assertTrue(
                self._on_page(widget, settings.display_page),
                widget)

    def test_page_list_switches_the_stack(self):
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        self.assertEqual(settings.pages.currentIndex(), 0)
        settings.page_list.setCurrentRow(2)
        QTest.qWait(50)
        self.assertEqual(settings.pages.currentIndex(), 2)
        self.assertIs(settings.pages.currentWidget(),
                      settings.bridge_page)

    def test_one_change_per_page_saves_and_reopens(self):
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        # Chat and tools page.
        index = settings.reply_length_combo.findData(2048)
        settings.reply_length_combo.setCurrentIndex(index)
        # Bridge and phone page.
        settings.bridge_check.setChecked(True)
        # Display and updates page.
        settings.compact_check.setChecked(True)
        settings.save_button.click()
        QTest.qWait(100)
        self.assertEqual(get_reply_length(), 2048)
        self.assertTrue(get_bridge_enabled())
        self.assertTrue(get_compact_list())
        second = SettingsWindow(self.models)
        self.windows.append(second)
        second.show()
        self.assertEqual(
            second.reply_length_combo.currentData(), 2048)
        self.assertTrue(second.bridge_check.isChecked())
        self.assertTrue(second.compact_check.isChecked())


class SettingsDeckButtonTests(FakeServiceTestCase):
    def test_settings_button_runs_the_windows_flow(self):
        window = self.make_window()
        calls = []
        window.open_phone_deck = lambda: calls.append(True)
        window.show_settings()
        settings = window.settings_window
        self.windows.append(settings)
        settings._show_phone_deck()
        self.assertEqual(calls, [True])
        # Afterwards the checkboxes show the saved state
        # (both off here: the stand-in flow changed nothing).
        self.assertEqual(settings.bridge_check.isChecked(),
                         get_bridge_enabled())
        self.assertEqual(settings.share_check.isChecked(),
                         get_bridge_share())

    def test_settings_button_standalone_says_not_running(self):
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        seen = {}

        class RecordingDialog:
            def __init__(self, url, parent=None, **kwargs):
                seen["url"] = url
                seen["kwargs"] = kwargs

            def exec(self):
                return 0

        with mock.patch.object(gui, "PhoneDeckDialog",
                               RecordingDialog):
            settings._show_phone_deck()
        self.assertIsNone(seen["url"])
        self.assertIn("not running", seen["kwargs"]["problem"])


if __name__ == "__main__":
    unittest.main()
