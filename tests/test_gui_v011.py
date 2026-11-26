"""GUI tests for the v0.11 additions: the home-network
sharing setting, the phone deck address in Settings, the QR
dialog, and the window's bridge rebind when sharing changes.

Same sandbox as test_gui.py: a temp home, a temp models
folder, FakeRunner everywhere. The bridge is a stand-in that
records what the window asks of it, like the v0.9 tests; the
real bind behavior is covered in test_bridge.py.
"""

import unittest
from unittest import mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import fyrn.gui as gui
from fyrn.gui import (DARK_STYLESHEET, PhoneDeckDialog,
                      SettingsWindow, get_bridge_enabled,
                      get_bridge_share, qr_pixmap,
                      set_bridge_enabled, set_bridge_share)
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


class ShareSettingTests(GuiTestCase):
    def test_share_defaults_off_and_round_trips(self):
        self.assertFalse(get_bridge_share())
        set_bridge_share(True)
        self.assertTrue(get_bridge_share())
        set_bridge_share(False)
        self.assertFalse(get_bridge_share())


class ShareSettingsWindowTests(GuiTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            gui, "deck_url", lambda port: DECK_URL)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_share_checkbox_and_url_field(self):
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        # Off by default: no address in the field. The button
        # itself is always ready (v0.12): pressing it turns
        # the bridge and sharing on for real, so it no longer
        # waits on this checkbox or on Save.
        self.assertFalse(settings.share_check.isChecked())
        self.assertEqual(settings.deck_url_edit.text(), "")
        self.assertEqual(
            settings.deck_url_edit.placeholderText(),
            "Sharing is off")
        self.assertTrue(settings.deck_button.isEnabled())
        # Turning it on fills the address and enables the
        # button.
        settings.share_check.setChecked(True)
        self.assertEqual(settings.deck_url_edit.text(), DECK_URL)
        self.assertTrue(settings.deck_button.isEnabled())
        # Saving persists it, and a fresh dialog shows it.
        settings.save_button.click()
        QTest.qWait(100)
        self.assertTrue(get_bridge_share())
        second = SettingsWindow(self.models)
        self.windows.append(second)
        second.show()
        self.assertTrue(second.share_check.isChecked())
        self.assertEqual(second.deck_url_edit.text(), DECK_URL)

    def test_share_off_saves_off(self):
        set_bridge_share(True)
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        self.assertTrue(settings.share_check.isChecked())
        settings.share_check.setChecked(False)
        settings.save_button.click()
        QTest.qWait(100)
        self.assertFalse(get_bridge_share())


class PhoneDeckDialogTests(GuiTestCase):
    def test_dialog_shows_the_url_and_a_code(self):
        dialog = PhoneDeckDialog(DECK_URL)
        self.windows.append(dialog)
        self.assertEqual(dialog.url_label.text(), DECK_URL)
        self.assertTrue(hasattr(dialog, "qr_label"))
        self.assertFalse(dialog.qr_label.pixmap().isNull())

    def test_dialog_without_an_address_says_so(self):
        dialog = PhoneDeckDialog(None)
        self.windows.append(dialog)
        self.assertIn("not found", dialog.url_label.text())
        self.assertFalse(hasattr(dialog, "qr_label"))

    def test_qr_pixmap_decodes_back_to_the_url(self):
        try:
            import cv2
        except ImportError:
            self.skipTest("opencv is not on this box")
        pixmap = qr_pixmap(DECK_URL, pixel_size=560)
        path = self.base / "qr.png"
        self.assertTrue(pixmap.save(str(path)))
        frame = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        self.assertIsNotNone(frame)
        detector = cv2.QRCodeDetector()
        decoded, _points, _straight = detector.detectAndDecode(
            frame)
        self.assertEqual(decoded, DECK_URL)


class ShareWindowWiringTests(GuiTestCase):
    def setUp(self):
        super().setUp()
        FakeBridgeService.CREATED.clear()
        self._real_service = gui.BridgeService
        gui.BridgeService = FakeBridgeService
        self.addCleanup(self._restore_service)

    def _restore_service(self):
        gui.BridgeService = self._real_service

    def test_bridge_created_with_a_share_fn(self):
        set_bridge_enabled(True)
        window = self.make_window()
        self.assertIsNotNone(window._bridge)
        self.assertIn("share_fn", window._bridge.kwargs)
        self.assertFalse(window._bridge.shared)

    def test_saving_a_share_change_rebinds_the_bridge(self):
        set_bridge_enabled(True)
        window = self.make_window()
        first = window._bridge
        self.assertFalse(first.shared)
        window.show_settings()
        settings = window.settings_window
        self.windows.append(settings)
        settings.share_check.setChecked(True)
        settings.save_button.click()
        QTest.qWait(100)
        # The old service was stopped and a new one started,
        # now shared: the setting applied on Save, with no
        # app restart.
        self.assertTrue(first.stopped)
        self.assertIsNotNone(window._bridge)
        self.assertIsNot(window._bridge, first)
        self.assertTrue(window._bridge.shared)
        self.assertEqual(len(FakeBridgeService.CREATED), 2)

    def test_saving_without_a_share_change_keeps_the_bridge(self):
        set_bridge_enabled(True)
        set_bridge_share(True)
        window = self.make_window()
        first = window._bridge
        self.assertTrue(first.shared)
        window.show_settings()
        settings = window.settings_window
        self.windows.append(settings)
        settings.save_button.click()
        QTest.qWait(100)
        self.assertFalse(first.stopped)
        self.assertIs(window._bridge, first)
        self.assertEqual(len(FakeBridgeService.CREATED), 1)


if __name__ == "__main__":
    unittest.main()
