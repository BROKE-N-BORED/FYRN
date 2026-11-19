"""GUI tests for v0.8: saved chats, instructions per chat, the
tool path in the window, the approval gate, and the browser view.

Uses the shared GuiTestCase (temp home, temp models folder, fake
runners), so no real model, network, or browser is involved. The
web test points the Forge Browser setting at a stub CLI folder
the test creates.
"""

import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from helpers import make_fake_gguf
from test_gui import GuiTestCase, pump_until
from test_tools import make_stub_forge_browser

from fyrn.chats import ChatStore
from fyrn.gui import (BrowserWindow, CommandApprovalDialog,
                      get_forge_browser_path, get_tools_folder,
                      set_forge_browser_path, set_tools_folder)
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


class ToolChatGuiTests(GuiTestCase):
    def setUp(self):
        super().setUp()
        self.tools_folder = self.base / "tools"
        self.tools_folder.mkdir()
        (self.tools_folder / "notes.txt").write_text(
            "the launch code is 4419", encoding="utf-8")
        set_tools_folder(str(self.tools_folder))

    def _window(self, runner):
        make_fake_gguf(self.models / "alpha.gguf", 2048)
        window = self.make_window(runner=runner)
        self.load_first_model(window)
        return window

    def _answer_approval(self, approve):
        self.watcher.enabled = False
        timer = QTimer()
        timer.setInterval(80)

        def answer():
            for widget in QApplication.topLevelWidgets():
                if isinstance(widget, CommandApprovalDialog) \
                        and widget.isVisible():
                    timer.stop()
                    button = (widget.run_button if approve
                              else widget.cancel_button)
                    button.click()

        timer.timeout.connect(answer)
        timer.start()
        return timer

    def test_tool_call_shows_in_transcript(self):
        runner = FakeRunner(
            supports_tools=True,
            tool_script=[("call", "read_file",
                          {"path": "notes.txt"}),
                         ("text", "The launch code is 4419.")])
        window = self._window(runner)
        self.assertIn("Tools are on",
                      window.tools_status_label.text())
        window.chat_input.setText("What is the launch code?")
        window.send_message()
        self.wait_chat(window)
        text = window.transcript.toPlainText()
        self.assertIn("Tool: read_file - notes.txt", text)
        self.assertIn("Result: read notes.txt", text)
        self.assertIn("The launch code is 4419.", text)
        # The answer's name prefix comes after the tool lines.
        self.assertLess(text.index("Tool: read_file"),
                        text.index("The launch code is 4419."))
        # The tool lines are in the saved chat too.
        record = ChatStore(home=self.home).load(
            window._current_chat_id)
        roles = [m["role"] for m in record["messages"]]
        self.assertEqual(roles, ["user", "tool", "tool",
                                 "assistant"])

    def test_instructions_reach_the_model(self):
        runner = FakeRunner(supports_tools=True)
        window = self._window(runner)
        window.instructions_edit.setPlainText(
            "You are a pirate. Answer like one.")
        window.chat_input.setText("Hello")
        window.send_message()
        self.wait_chat(window)
        self.assertEqual(runner.last_instructions,
                         "You are a pirate. Answer like one.")

    def test_instructions_reach_a_plain_model_too(self):
        runner = FakeRunner()  # no tool support: the plain path
        window = self._window(runner)
        window.instructions_edit.setPlainText(
            "Always answer in French.")
        window.chat_input.setText("Hello")
        window.send_message()
        self.wait_chat(window)
        self.assertEqual(runner.last_instructions,
                         "Always answer in French.")

    def test_command_runs_only_after_a_yes(self):
        runner = FakeRunner(
            supports_tools=True,
            tool_script=[("call", "run_command",
                          {"command": "touch sentinel.txt"}),
                         ("text", "Done.")])
        window = self._window(runner)
        timer = self._answer_approval(approve=True)
        try:
            window.chat_input.setText("Make the file")
            window.send_message()
            self.wait_chat(window)
        finally:
            timer.stop()
            self.watcher.enabled = True
        self.assertTrue(
            (self.tools_folder / "sentinel.txt").exists())
        self.assertIn("Tool: run_command - touch sentinel.txt",
                      window.transcript.toPlainText())

    def test_command_cancelled_never_runs(self):
        runner = FakeRunner(
            supports_tools=True,
            tool_script=[("call", "run_command",
                          {"command": "touch sentinel.txt"}),
                         ("text", "Okay, not doing that.")])
        window = self._window(runner)
        timer = self._answer_approval(approve=False)
        try:
            window.chat_input.setText("Make the file")
            window.send_message()
            self.wait_chat(window)
        finally:
            timer.stop()
            self.watcher.enabled = True
        self.assertFalse(
            (self.tools_folder / "sentinel.txt").exists())
        self.assertIn("said no", window.transcript.toPlainText())

    def test_stop_cancels_the_tool_loop(self):
        runner = FakeRunner(
            supports_tools=True, token_delay_s=0.0,
            tool_script=[("call", "calculate",
                          {"expression": "1+1"}),
                         ("sleep", 2.0),
                         ("text", "late answer")])
        window = self._window(runner)
        window.chat_input.setText("Add it up, slowly")
        window.send_message()
        self.assertTrue(pump_until(
            lambda: "Tool: calculate" in
            window.transcript.toPlainText()))
        window.stop_button.click()
        self.wait_chat(window)
        self.assertIn("(Reply stopped.)",
                      window.transcript.toPlainText())
        self.assertNotIn("late answer",
                         window.transcript.toPlainText())

    def test_web_tool_fills_the_browser_view(self):
        app_dir = make_stub_forge_browser(self.base / "installed")
        set_forge_browser_path(str(app_dir))
        runner = FakeRunner(
            supports_tools=True,
            tool_script=[("call", "web_search",
                          {"query": "stub query"}),
                         ("text", "Found one result.")])
        window = self._window(runner)
        window.chat_input.setText("Search the web for me")
        window.send_message()
        self.wait_chat(window)
        self.assertIsNotNone(window.browser_window)
        self.assertTrue(window.browser_window.isVisible())
        content = window.browser_window.content_view.toPlainText()
        self.assertIn("Stub Result", content)
        self.assertIn("Search results for: stub query",
                      window.browser_window.status_label.text())
        self.assertIn("Tool: web_search - stub query",
                      window.transcript.toPlainText())

    def test_model_without_tools_chats_as_before(self):
        runner = FakeRunner()  # supports_tools=False
        window = self._window(runner)
        self.assertIn("cannot use tools",
                      window.tools_status_label.text())
        window.chat_input.setText("Hello")
        window.send_message()
        self.wait_chat(window)
        text = window.transcript.toPlainText()
        self.assertIn("Hello from the fake model", text)
        self.assertNotIn("Tool:", text)

    def test_broken_tool_path_falls_back_to_a_plain_reply(self):
        class FallbackRunner(FakeRunner):
            def generate_tools(self, *args, **kwargs):
                raise ToolsUnsupportedError("no tools after all")

        runner = FallbackRunner(supports_tools=True)
        window = self._window(runner)
        window.chat_input.setText("Hello")
        window.send_message()
        self.wait_chat(window)
        self.assertIn("Hello from the fake model",
                      window.transcript.toPlainText())
        self.assertTrue(window._tools_broken)
        self.assertIn("cannot use tools",
                      window.tools_status_label.text())


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
    def test_settings_window_saves_the_tool_settings(self):
        from fyrn.gui import SettingsWindow
        settings = SettingsWindow(self.models)
        self.windows.append(settings)
        settings.show()
        QTest.qWait(50)
        self.assertEqual(settings.tools_folder_edit.text(),
                         get_tools_folder())
        new_folder = str(self.base / "picked-tools")
        settings.tools_folder_edit.setText(new_folder)
        app_dir = make_stub_forge_browser(self.base / "installed")
        settings.forge_path_edit.setText(str(app_dir))
        settings.save_button.click()
        QTest.qWait(50)
        self.assertEqual(get_tools_folder(), new_folder)
        self.assertEqual(get_forge_browser_path(), str(app_dir))


if __name__ == "__main__":
    unittest.main()
