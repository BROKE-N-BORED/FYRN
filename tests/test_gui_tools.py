"""GUI tests: saved chats, instructions per chat, the web
browsing path in the window (off by default, on at the
user's flip, calls in the transcript and the browser view),
and the settings page. The built-in tools these tests also
covered (file tools, the approval-gated command tool) were
removed in v0.13; what remains is the Forge Browser
connection.

Uses the shared GuiTestCase (temp home, temp models folder,
fake runners), so no real model, network, or browser is
involved. The web tests point the Forge Browser setting at
a stub CLI folder the test creates.
"""

import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtTest import QTest

from helpers import make_fake_gguf
from test_gui import GuiTestCase, pump_until
from test_tools import make_stub_forge_browser

from fyrn.chats import ChatStore
from fyrn.gui import (BrowserWindow, get_forge_browser_path,
                      set_forge_browser_path)
from fyrn.runner import FakeRunner, ToolsUnsupportedError


def setUpModule():
    # The QApplication and the real stylesheet come from the
    # shared GUI test module.
    from test_gui import setUpModule as _setup_module
    _setup_module()


class SavedChatGuiTests(GuiTestCase):
    def _window_with_chat(self):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window()
        self.load_first_model(window)
        window.chat_input.setText("How do I bake bread?")
        window.send_message()
        self.wait_chat(window)
        return window

    def _store(self):
        return ChatStore(home=self.home)

    def test_chat_saves_itself_as_you_talk(self):
        window = self._window_with_chat()
        summaries = self._store().list_chats()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["title"],
                         "How do I bake bread?")
        self.assertEqual(summaries[0]["message_count"], 2)
        # The sidebar shows it too.
        self.assertEqual(window.chat_list.count(), 1)
        self.assertEqual(window.chat_list.item(0).text(),
                         "How do I bake bread?")
        # A second message grows the same chat, not a new one.
        window.chat_input.setText("And how long in the oven?")
        window.send_message()
        self.wait_chat(window)
        summaries = self._store().list_chats()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["message_count"], 4)

    def test_reopen_restores_transcript_and_history(self):
        window = self._window_with_chat()
        window.new_chat()
        QTest.qWait(50)
        self.assertEqual(window.transcript.toPlainText(), "")
        self.assertEqual(window._chat_history, [])
        # Reopen from the sidebar.
        window.chat_list.setCurrentRow(0)
        QTest.qWait(50)
        text = window.transcript.toPlainText()
        self.assertIn("You: How do I bake bread?", text)
        self.assertIn("Hello from the fake model", text)
        self.assertEqual(len(window._chat_history), 2)
        # Sending continues with the saved history as context.
        window.chat_input.setText("Thanks!")
        window.send_message()
        self.wait_chat(window)
        history = window.runner.last_history
        self.assertEqual(
            [m["content"] for m in history],
            ["How do I bake bread?",
             "Hello from the fake model. This is a test reply.",
             "Thanks!"][0:2])

    def test_new_chat_keeps_the_old_one(self):
        window = self._window_with_chat()
        window.new_chat_button.click()
        QTest.qWait(50)
        self.assertEqual(window.transcript.toPlainText(), "")
        self.assertEqual(len(self._store().list_chats()), 1)
        self.assertIsNone(window._current_chat_id)

    def test_rename_chat(self):
        window = self._window_with_chat()
        with mock.patch(
                "fyrn.gui.QInputDialog.getText",
                return_value=("Bread chat", True)):
            window.rename_chat()
        QTest.qWait(50)
        summaries = self._store().list_chats()
        self.assertEqual(summaries[0]["title"], "Bread chat")
        self.assertEqual(window.chat_list.item(0).text(),
                         "Bread chat")
        # A manual title is not overwritten by the next message.
        window.chat_input.setText("More bread questions")
        window.send_message()
        self.wait_chat(window)
        self.assertEqual(self._store().list_chats()[0]["title"],
                         "Bread chat")

    def test_delete_defaults_to_no(self):
        window = self._window_with_chat()
        # The ModalWatcher clicks the default button: No.
        window.delete_chat_button.click()
        QTest.qWait(100)
        self.assertEqual(len(self._store().list_chats()), 1)
        self.assertIn("How do I bake bread?",
                      window.transcript.toPlainText())

    def test_delete_with_yes_removes_it(self):
        window = self._window_with_chat()
        timer = self._answer_yes()
        try:
            window.delete_chat_button.click()
        finally:
            timer.stop()
            self.watcher.enabled = True
        QTest.qWait(50)
        self.assertEqual(self._store().list_chats(), [])
        self.assertEqual(window.transcript.toPlainText(), "")
        self.assertEqual(window.chat_list.count(), 0)

    def test_instructions_save_with_the_chat(self):
        window = self._window_with_chat()
        window.instructions_edit.setPlainText("Be very brief.")
        # The save is debounced: let it land.
        self.assertTrue(pump_until(
            lambda: self._store().list_chats()
            and self._store().list_chats()[0]["instructions"]
            == "Be very brief."))
        window.new_chat()
        QTest.qWait(50)
        self.assertEqual(
            window.instructions_edit.toPlainText(), "")
        window.chat_list.setCurrentRow(0)
        QTest.qWait(50)
        self.assertEqual(
            window.instructions_edit.toPlainText(),
            "Be very brief.")

    def test_clear_empties_the_saved_chat_too(self):
        window = self._window_with_chat()
        window.clear_button.click()
        QTest.qWait(50)
        record = self._store().load(window._current_chat_id)
        self.assertEqual(record["messages"], [])


class BrowsingChatGuiTests(GuiTestCase):
    def _window(self, runner):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        return window

    def _stub_browser(self):
        app_dir = make_stub_forge_browser(self.base / "installed")
        set_forge_browser_path(str(app_dir))
        return app_dir

    def test_web_call_shows_in_transcript(self):
        self._stub_browser()
        runner = FakeRunner(
            supports_tools=True,
            tool_script=[("call", "web_search",
                          {"query": "stub query"}),
                         ("text", "Found one result.")])
        window = self._window(runner)
        # Browsing is off by default (v0.13): the user turns
        # it on for the chat first.
        window.browsing_check.setChecked(True)
        self.assertIn("Web browsing is on for this chat",
                      window.browsing_status_label.text())
        window.chat_input.setText("Search the web for me")
        window.send_message()
        self.wait_chat(window)
        text = window.transcript.toPlainText()
        self.assertIn("Web search - stub query", text)
        self.assertIn(
            'Result: searched for "stub query" (1 results)', text)
        self.assertIn("Found one result.", text)
        # The answer's name prefix comes after the call lines.
        self.assertLess(text.index("Web search - stub query"),
                        text.index("Found one result."))
        # The call lines are in the saved chat too.
        record = ChatStore(home=self.home).load(
            window._current_chat_id)
        roles = [m["role"] for m in record["messages"]]
        self.assertEqual(roles, ["user", "tool", "tool",
                                 "assistant"])

    def test_instructions_reach_the_model(self):
        runner = FakeRunner(supports_tools=True)
        window = self._window(runner)
        window.browsing_check.setChecked(True)
        window.instructions_edit.setPlainText(
            "You are a pirate. Answer like one.")
        window.chat_input.setText("Hello")
        window.send_message()
        self.wait_chat(window)
        self.assertEqual(runner.last_instructions,
                         "You are a pirate. Answer like one.")

    def test_instructions_reach_a_plain_model_too(self):
        runner = FakeRunner()  # no browsing support: plain path
        window = self._window(runner)
        window.instructions_edit.setPlainText(
            "Always answer in French.")
        window.chat_input.setText("Hello")
        window.send_message()
        self.wait_chat(window)
        self.assertEqual(runner.last_instructions,
                         "Always answer in French.")

    def test_stop_cancels_the_browsing_loop(self):
        self._stub_browser()
        runner = FakeRunner(
            supports_tools=True, token_delay_s=0.0,
            tool_script=[("call", "web_search",
                          {"query": "stub query"}),
                         ("sleep", 2.0),
                         ("text", "late answer")])
        window = self._window(runner)
        window.browsing_check.setChecked(True)
        window.chat_input.setText("Search, slowly")
        window.send_message()
        self.assertTrue(pump_until(
            lambda: "Web search" in
            window.transcript.toPlainText()))
        window.stop_button.click()
        self.wait_chat(window)
        self.assertIn("(Reply stopped.)",
                      window.transcript.toPlainText())
        self.assertNotIn("late answer",
                         window.transcript.toPlainText())

    def test_web_tool_fills_the_browser_view(self):
        self._stub_browser()
        runner = FakeRunner(
            supports_tools=True,
            tool_script=[("call", "web_search",
                          {"query": "stub query"}),
                         ("text", "Found one result.")])
        window = self._window(runner)
        window.browsing_check.setChecked(True)
        window.chat_input.setText("Search the web for me")
        window.send_message()
        self.wait_chat(window)
        self.assertIsNotNone(window.browser_window)
        self.assertTrue(window.browser_window.isVisible())
        content = window.browser_window.content_view.toPlainText()
        self.assertIn("Stub Result", content)
        self.assertIn("Search results for: stub query",
                      window.browser_window.status_label.text())
        self.assertIn("Web search - stub query",
                      window.transcript.toPlainText())

    def test_model_without_browsing_chats_as_before(self):
        runner = FakeRunner()  # supports_tools=False
        window = self._window(runner)
        self.assertIn("cannot browse the web",
                      window.browsing_status_label.text())
        window.chat_input.setText("Hello")
        window.send_message()
        self.wait_chat(window)
        text = window.transcript.toPlainText()
        self.assertIn("Hello from the fake model", text)
        self.assertNotIn("Web search", text)

    def test_broken_browsing_path_falls_back_to_a_plain_reply(self):
        class FallbackRunner(FakeRunner):
            def generate_tools(self, *args, **kwargs):
                raise ToolsUnsupportedError("no browsing after all")

        runner = FallbackRunner(supports_tools=True)
        window = self._window(runner)
        window.browsing_check.setChecked(True)
        window.chat_input.setText("Hello")
        window.send_message()
        self.wait_chat(window)
        self.assertIn("Hello from the fake model",
                      window.transcript.toPlainText())
        self.assertTrue(window._browsing_broken)
        self.assertIn("cannot browse the web",
                      window.browsing_status_label.text())


class BrowserWindowTests(GuiTestCase):
    def test_browser_window_opens_and_shows_a_page(self):
        window = self.make_window()
        browser = window.show_browser_window()
        self.assertIsInstance(browser, BrowserWindow)
        self.assertTrue(browser.isVisible())
        browser.show_page("https://example.com/a", "A title",
                          "Some page text.", None)
        self.assertEqual(browser.url_label.text(),
                         "https://example.com/a")
        self.assertEqual(browser.content_view.toPlainText(),
                         "Some page text.")
        self.assertTrue(browser.open_button.isEnabled())
        browser.show_search("cats", "1. A cat page")
        self.assertIn("cats", browser.status_label.text())
        self.assertFalse(browser.open_button.isEnabled())

    def test_open_in_forge_browser_uses_the_cli(self):
        app_dir = make_stub_forge_browser(self.base / "installed")
        set_forge_browser_path(str(app_dir))
        window = self.make_window()
        browser = window.show_browser_window()
        browser.show_page("https://example.com/a", "T", "text",
                          None)
        browser.open_button.click()
        log = app_dir / "app-calls.log"
        self.assertTrue(pump_until(lambda: log.exists()))
        self.assertIn("opened https://example.com/a",
                      log.read_text(encoding="utf-8"))


class ToolSettingsGuiTests(GuiTestCase):
    """The settings page (the class name predates v0.13): the
    tools folder setting is gone with the built-in tools;
    the Forge Browser folder remains and saves."""

    def test_settings_window_saves_the_forge_browser_folder(self):
        from fyrn.gui import SettingsWindow
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        QTest.qWait(50)
        # No tools folder control exists any more (v0.13).
        self.assertFalse(hasattr(settings, "tools_folder_edit"))
        app_dir = make_stub_forge_browser(self.base / "installed")
        settings.forge_path_edit.setText(str(app_dir))
        settings.save_button.click()
        QTest.qWait(50)
        self.assertEqual(get_forge_browser_path(), str(app_dir))


if __name__ == "__main__":
    unittest.main()
