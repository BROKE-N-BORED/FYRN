"""GUI tests for the v0.9 additions: the reply-length setting,
the bridge toggle and window wiring, and idle auto-unload.

Same sandbox as test_gui.py: a temp home, a temp models
folder, FakeRunner everywhere. The bridge's real HTTP behavior
is covered in test_bridge.py; here a stand-in BridgeService
keeps these tests off the real port.
"""

import time
import unittest
from types import SimpleNamespace

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import fyrn.gui as gui
from fyrn.gui import (DARK_STYLESHEET, SettingsWindow,
                      get_bridge_enabled, get_idle_unload_minutes,
                      get_reply_length, set_bridge_enabled,
                      set_idle_unload_minutes, set_reply_length)
from fyrn.runner import FakeRunner
from helpers import make_fake_gguf
from test_gui import GuiTestCase, pump_until

GB = 1024 ** 3

_app = None


def setUpModule():
    # Same app setup as test_gui.py: when the whole suite runs,
    # the app already exists and this just re-applies the look.
    global _app
    _app = QApplication.instance()
    if _app is None:
        _app = QApplication([])
    _app.setStyleSheet(DARK_STYLESHEET)


class FakeBridgeService:
    """Records what the window asks of the bridge without
    binding a port."""

    CREATED = []

    def __init__(self, controller, list_models_fn, **kwargs):
        self.controller = controller
        self.kwargs = kwargs
        self.generating = False
        self.start_error = ""
        self.started = False
        self.stopped = False
        FakeBridgeService.CREATED.append(self)

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.stopped = True

    @property
    def running(self):
        return self.started and not self.stopped

    @property
    def port(self):
        return 11434


class SettingsHelperTests(GuiTestCase):
    def test_reply_length_defaults_and_round_trips(self):
        self.assertEqual(get_reply_length(), 1024)
        set_reply_length(2048)
        self.assertEqual(get_reply_length(), 2048)
        set_reply_length(512)
        self.assertEqual(get_reply_length(), 512)
        # A value outside the offered choices falls back.
        set_reply_length(999)
        self.assertEqual(get_reply_length(), 1024)

    def test_bridge_toggle_defaults_off(self):
        self.assertFalse(get_bridge_enabled())
        set_bridge_enabled(True)
        self.assertTrue(get_bridge_enabled())
        set_bridge_enabled(False)
        self.assertFalse(get_bridge_enabled())

    def test_idle_unload_defaults_off(self):
        self.assertEqual(get_idle_unload_minutes(), 0)
        set_idle_unload_minutes(30)
        self.assertEqual(get_idle_unload_minutes(), 30)
        set_idle_unload_minutes(45)
        self.assertEqual(get_idle_unload_minutes(), 0)


class SettingsWindowV09Tests(GuiTestCase):
    def test_new_settings_save(self):
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        # Defaults on a fresh settings file.
        self.assertEqual(settings.reply_length_combo.currentData(),
                         1024)
        self.assertFalse(settings.bridge_check.isChecked())
        self.assertEqual(settings.idle_combo.currentData(), 0)
        index = settings.reply_length_combo.findData(2048)
        settings.reply_length_combo.setCurrentIndex(index)
        settings.bridge_check.setChecked(True)
        index = settings.idle_combo.findData(30)
        settings.idle_combo.setCurrentIndex(index)
        settings.save_button.click()
        QTest.qWait(100)
        self.assertEqual(get_reply_length(), 2048)
        self.assertTrue(get_bridge_enabled())
        self.assertEqual(get_idle_unload_minutes(), 30)
        # Reopening shows the saved state.
        second = SettingsWindow(self.models)
        self.windows.append(second)
        second.show()
        self.assertEqual(
            second.reply_length_combo.currentData(), 2048)
        self.assertTrue(second.bridge_check.isChecked())
        self.assertEqual(second.idle_combo.currentData(), 30)


class ReplyLengthWiringTests(GuiTestCase):
    # The Settings choices are 512 and up, so these tests use a
    # 600-word reply and the smallest cap: the wiring assertion
    # is last_max_tokens, and the transcript must stop at the
    # cap.

    LONG_REPLY = " ".join("w%d" % i for i in range(600))

    def test_setting_reaches_the_plain_path(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        set_reply_length(512)
        runner = FakeRunner(reply_text=self.LONG_REPLY)
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        window.chat_input.setPlainText("hello")
        window.send_button.click()
        self.wait_chat(window)
        self.assertEqual(runner.last_max_tokens, 512)
        text = window.transcript.toPlainText()
        self.assertIn("w511", text)
        self.assertNotIn("w512", text)

    def test_setting_reaches_the_tool_path(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        set_reply_length(512)
        runner = FakeRunner(
            supports_tools=True,
            tool_script=[("text", self.LONG_REPLY)])
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        # Browsing is off by default (v0.13): turn it on for
        # this chat, as the user now does.
        window.browsing_check.setChecked(True)
        window.chat_input.setPlainText("hello")
        window.send_button.click()
        self.wait_chat(window)
        self.assertEqual(runner.last_max_tokens, 512)
        text = window.transcript.toPlainText()
        self.assertIn("w511", text)
        self.assertNotIn("w512", text)


class BridgeWindowTests(GuiTestCase):
    def setUp(self):
        super().setUp()
        FakeBridgeService.CREATED.clear()
        self._real_service = gui.BridgeService
        gui.BridgeService = FakeBridgeService
        self.addCleanup(self._restore_service)

    def _restore_service(self):
        gui.BridgeService = self._real_service

    def test_bridge_off_by_default_no_service(self):
        window = self.make_window()
        self.assertIsNone(window._bridge)
        self.assertEqual(FakeBridgeService.CREATED, [])

    def test_bridge_starts_when_the_setting_is_on(self):
        set_bridge_enabled(True)
        window = self.make_window()
        self.assertIsNotNone(window._bridge)
        self.assertTrue(window._bridge.started)
        self.assertEqual(len(FakeBridgeService.CREATED), 1)

    def test_turning_it_off_in_settings_stops_it(self):
        set_bridge_enabled(True)
        window = self.make_window()
        bridge = window._bridge
        window.show_settings()
        settings = window.settings_window
        self.windows.append(settings)
        self.assertTrue(settings.bridge_check.isChecked())
        settings.bridge_check.setChecked(False)
        settings.save_button.click()
        QTest.qWait(100)
        self.assertTrue(bridge.stopped)
        self.assertIsNone(window._bridge)

    def test_bridge_generation_blocks_unload_and_send(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.load_first_model(window)
        window._bridge = SimpleNamespace(
            generating=True, stop=lambda: None)
        window.unload_model()
        self.assertTrue(window.controller.is_loaded)
        self.assertIn("bridge",
                      window.statusBar().currentMessage())
        window.chat_input.setPlainText("hello")
        window.send_message()
        self.assertIsNone(window.chat_worker)
        self.assertIn("bridge",
                      window.statusBar().currentMessage())


class IdleUnloadTests(GuiTestCase):
    def _loaded_window(self, runner=None):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        return window

    def test_idle_model_unloads_itself(self):
        set_idle_unload_minutes(15)
        window = self._loaded_window()
        window._last_activity = time.monotonic() - 16 * 60
        window._idle_unload_check()
        self.assertFalse(window.controller.is_loaded)
        self.assertIn("unloaded itself",
                      window.statusBar().currentMessage())
        self.assertIn("free RAM",
                      window.statusBar().currentMessage())

    def test_recent_activity_keeps_the_model(self):
        set_idle_unload_minutes(15)
        window = self._loaded_window()
        window._last_activity = time.monotonic() - 5 * 60
        window._idle_unload_check()
        self.assertTrue(window.controller.is_loaded)

    def test_off_by_default_never_unloads(self):
        window = self._loaded_window()
        window._last_activity = time.monotonic() - 24 * 3600
        window._idle_unload_check()
        self.assertTrue(window.controller.is_loaded)

    def test_activity_resets_the_clock(self):
        # This is the same method the bridge calls on every
        # request, and sending a message calls.
        set_idle_unload_minutes(15)
        window = self._loaded_window()
        window._last_activity = time.monotonic() - 16 * 60
        window._note_activity()
        window._idle_unload_check()
        self.assertTrue(window.controller.is_loaded)

    def test_a_reply_in_progress_blocks_the_unload(self):
        set_idle_unload_minutes(15)
        runner = FakeRunner(
            reply_text=" ".join("word%d" % i for i in range(30)),
            token_delay_s=0.2)
        window = self._loaded_window(runner=runner)
        window.chat_input.setPlainText("hello")
        window.send_button.click()
        self.assertTrue(pump_until(
            lambda: window.chat_worker is not None,
            timeout_ms=2000))
        window._last_activity = time.monotonic() - 16 * 60
        window._idle_unload_check()
        self.assertTrue(window.controller.is_loaded)
        # The scripted reply streams 30 words at 0.2 s each
        # (~6 s), longer than wait_chat's 5 s budget on a fast
        # machine: wait for the worker with room to spare.
        self.assertTrue(pump_until(
            lambda: window.chat_worker is None,
            timeout_ms=20000))
        QTest.qWait(50)
        # The finished reply counts as activity; age the clock
        # again and the unload fires.
        window._last_activity = time.monotonic() - 16 * 60
        window._idle_unload_check()
        self.assertFalse(window.controller.is_loaded)


if __name__ == "__main__":
    unittest.main()
