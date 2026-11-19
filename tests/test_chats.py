"""Tests for fyrn.chats: saved-chat storage (v0.8).

Save/load round-trips, auto titles, rename, delete, instructions,
and the model-facing history subset. All in a temp home: no real
chat folder is ever touched.
"""

import json
import tempfile
import unittest
from pathlib import Path

from fyrn.chats import (ChatStore, auto_title, chats_dir,
                        history_from_messages)


class AutoTitleTests(unittest.TestCase):
    def test_short_message_is_the_title(self):
        self.assertEqual(auto_title("How do I bake bread?"),
                         "How do I bake bread?")

    def test_whitespace_collapses(self):
        self.assertEqual(auto_title("  hello\n\n  world  "),
                         "hello world")

    def test_long_message_cuts_at_a_word(self):
        title = auto_title(
            "How do I bake bread at home in a small oven with no "
            "stand mixer at all?")
        self.assertLessEqual(len(title), 48)
        self.assertEqual(
            title, "How do I bake bread at home in a small oven")

    def test_empty_is_new_chat(self):
        self.assertEqual(auto_title(""), "New chat")
        self.assertEqual(auto_title("   "), "New chat")
        self.assertEqual(auto_title(None), "New chat")


class ChatStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.store = ChatStore(home=self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self, text="Hello there"):
        record = self.store.new_record()
        record["messages"] = [
            {"role": "user", "content": text},
            {"role": "assistant", "name": "fake:1b",
             "content": "Hi!"},
            {"role": "tool", "name": "calculate",
             "content": "Tool: calculate - 2+2"},
        ]
        record["title"] = auto_title(text)
        return record

    def test_chats_live_under_the_home(self):
        self.assertEqual(chats_dir(self.home),
                         self.home / "chats")

    def test_save_load_round_trip(self):
        record = self._record()
        record["instructions"] = "Be brief. Always."
        saved = self.store.save(record)
        self.assertTrue(saved["id"])
        loaded = self.store.load(saved["id"])
        self.assertEqual(loaded["title"], "Hello there")
        self.assertEqual(loaded["instructions"], "Be brief. Always.")
        self.assertEqual(len(loaded["messages"]), 3)
        self.assertEqual(loaded["messages"][1]["name"], "fake:1b")
        # The file is plain JSON in the chats folder.
        path = self.home / "chats" / ("%s.json" % saved["id"])
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["title"], "Hello there")

    def test_save_updates_the_same_file(self):
        saved = self.store.save(self._record())
        saved["messages"].append(
            {"role": "user", "content": "More"})
        again = self.store.save(saved)
        self.assertEqual(again["id"], saved["id"])
        files = list((self.home / "chats").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertGreaterEqual(again["updated"], saved["updated"])

    def test_list_is_newest_first_with_counts(self):
        first = self.store.save(self._record("First chat"))
        second = self.store.save(self._record("Second chat"))
        summaries = self.store.list_chats()
        self.assertEqual([s["id"] for s in summaries],
                         [second["id"], first["id"]])
        self.assertEqual(summaries[0]["message_count"], 3)
        self.assertEqual(summaries[0]["title"], "Second chat")

    def test_rename_stops_the_auto_title(self):
        saved = self.store.save(self._record())
        renamed = self.store.rename(saved["id"], "Bread chat")
        self.assertEqual(renamed["title"], "Bread chat")
        self.assertFalse(renamed["title_auto"])
        self.assertEqual(
            self.store.load(saved["id"])["title"], "Bread chat")

    def test_delete_removes_the_file(self):
        saved = self.store.save(self._record())
        self.assertTrue(self.store.delete(saved["id"]))
        self.assertIsNone(self.store.load(saved["id"]))
        self.assertEqual(self.store.list_chats(), [])
        self.assertFalse(self.store.delete(saved["id"]))

    def test_bad_ids_never_escape_the_folder(self):
        self.assertIsNone(self.store.load("../secret"))
        self.assertFalse(self.store.delete("../secret"))
        self.assertIsNone(self.store.load(""))

    def test_damaged_files_are_skipped(self):
        folder = self.home / "chats"
        folder.mkdir(parents=True)
        (folder / "broken.json").write_text("{not json",
                                            encoding="utf-8")
        (folder / "alist.json").write_text("[1, 2]",
                                           encoding="utf-8")
        saved = self.store.save(self._record())
        summaries = self.store.list_chats()
        self.assertEqual([s["id"] for s in summaries],
                         [saved["id"]])
        self.assertIsNone(self.store.load("broken"))

    def test_instructions_default_empty(self):
        saved = self.store.save(self._record())
        self.assertEqual(
            self.store.load(saved["id"])["instructions"], "")


class HistoryTests(unittest.TestCase):
    def test_history_keeps_only_user_and_assistant(self):
        messages = [
            {"role": "user", "content": "q"},
            {"role": "tool", "name": "calculate",
             "content": "Tool: calculate - 2+2"},
            {"role": "assistant", "name": "m", "content": "a"},
        ]
        self.assertEqual(history_from_messages(messages), [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ])

    def test_history_of_nothing(self):
        self.assertEqual(history_from_messages([]), [])
        self.assertEqual(history_from_messages(None), [])


if __name__ == "__main__":
    unittest.main()
