"""Tests for fyrn.debug_log: secret scrubbing, ring
buffer, and report generation."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fyrn.debug_log import (DebugLogger, is_sensitive_key,
                                  scrub_dict, scrub_text)


class ScrubTests(unittest.TestCase):
    def test_bearer_token(self):
        line = scrub_text("Authorization: Bearer abc123xyz")
        self.assertNotIn("abc123xyz", line)
        self.assertIn("[REDACTED]", line)

    def test_api_key_assignment(self):
        line = scrub_text("api_key=sk-live-value-1234567890")
        self.assertNotIn("sk-live-value", line)
        self.assertIn("[REDACTED]", line)

    def test_password_assignment(self):
        line = scrub_text("password: hunter2")
        self.assertNotIn("hunter2", line)

    def test_sk_style_key(self):
        line = scrub_text("key is sk-abcdefghij1234567890 here")
        self.assertNotIn("sk-abcdefghij", line)

    def test_github_token(self):
        line = scrub_text("token ghp_abcdefghij1234567890")
        self.assertNotIn("ghp_abcdefghij", line)

    def test_token_in_url(self):
        line = scrub_text("https://x.example/a?token=secret123&b=1")
        self.assertNotIn("secret123", line)
        self.assertIn("b=1", line)

    def test_user_password_in_url(self):
        line = scrub_text("https://bob:hunter2@example.com/x")
        self.assertNotIn("hunter2", line)
        self.assertNotIn("bob:hunter2", line)

    def test_clean_text_unchanged(self):
        self.assertEqual(scrub_text("models folder is /home/u/Models"),
                         "models folder is /home/u/Models")

    def test_sensitive_key_names(self):
        self.assertTrue(is_sensitive_key("api_key"))
        self.assertTrue(is_sensitive_key("password"))
        self.assertTrue(is_sensitive_key("auth_token"))
        self.assertFalse(is_sensitive_key("models_folder"))
        self.assertFalse(is_sensitive_key("version"))

    def test_scrub_dict(self):
        out = scrub_dict({
            "api_key": "sk-live-123",
            "models_folder": "/home/u/Models",
            "nested": {"token": "abc", "count": 3},
        })
        self.assertNotIn("sk-live-123", str(out))
        self.assertEqual(out["models_folder"], "/home/u/Models")
        self.assertIn("REDACTED", out["api_key"])
        self.assertNotIn("abc", str(out["nested"]))


class LoggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = os.path.join(self.tmp.name, "logs")
        self.log = DebugLogger("fyrn-test", "0.1.0",
                               log_dir=self.log_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_report_contains_actions_and_context(self):
        self.log.action("user pressed Load")
        self.log.set_setting("models_folder", "/home/u/Models")
        path = self.log.generate_report()
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("fyrn-test", text)
        self.assertIn("user pressed Load", text)
        self.assertIn("/home/u/Models", text)
        self.assertIn("0.1.0", text)

    def test_report_scrubs_secrets(self):
        self.log.set_setting("api_key", "sk-live-secret-value-999")
        self.log.action("tried token=abc123secret")
        path = self.log.generate_report()
        text = Path(path).read_text(encoding="utf-8")
        self.assertNotIn("sk-live-secret-value-999", text)
        self.assertNotIn("abc123secret", text)
        self.assertIn("[REDACTED]", text)

    def test_two_reports_in_one_second_do_not_overwrite(self):
        first = self.log.generate_report()
        second = self.log.generate_report()
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.exists(first))
        self.assertTrue(os.path.exists(second))

    def test_log_file_is_written(self):
        self.log.info("hello log")
        self.assertTrue(os.path.exists(
            os.path.join(self.log_dir, "fyrn-test.log")))

    def test_cli_report(self):
        out = os.path.join(self.tmp.name, "cli-report.txt")
        result = subprocess.run(
            [sys.executable, "-m", "fyrn.debug_log", "report",
             "--app", "fyrn-test", "--version", "0.1.0",
             "--log-dir", self.log_dir, "--out", out],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
