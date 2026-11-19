"""Tests for fyrn.tools (v0.8): the calculator, the sandboxed
file tools, the approval-gated command tool, the tool-call text
parser, and the web tools against a stub Forge Browser CLI the
tests create. Nothing here touches the network or a real
browser.
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from fyrn.tools import (ForgeBrowserClient, ToolExecutor,
                        ToolFailure, calculate,
                        find_forge_browser_command, parse_tool_calls)


class CalculatorTests(unittest.TestCase):
    def test_plain_arithmetic(self):
        self.assertEqual(calculate("23 * 4 + 5"), "23 * 4 + 5 = 97")
        self.assertEqual(calculate("10 / 4"), "10 / 4 = 2.5")
        self.assertEqual(calculate("2 ** 10"), "2 ** 10 = 1024")
        self.assertEqual(calculate("-(3 + 4)"), "-(3 + 4) = -7")

    def test_functions_and_constants(self):
        self.assertEqual(calculate("sqrt(16)"), "sqrt(16) = 4")
        self.assertIn("3.14", calculate("pi"))
        self.assertEqual(calculate("floor(2.7)"), "floor(2.7) = 2")

    def test_rejects_non_arithmetic(self):
        for bad in ("__import__('os').system('echo hi')",
                    "open('/etc/passwd').read()",
                    "x + 1",
                    "a.b",
                    "[1, 2][0]",
                    "1 if True else 2",
                    "'text'",
                    "import os",
                    "lambda: 1"):
            with self.assertRaises(ToolFailure, msg=bad):
                calculate(bad)

    def test_rejects_division_by_zero_and_huge_powers(self):
        with self.assertRaises(ToolFailure):
            calculate("1 / 0")
        with self.assertRaises(ToolFailure):
            calculate("10 ** 10 ** 10")

    def test_empty_is_refused(self):
        with self.assertRaises(ToolFailure):
            calculate("")


class ParseToolCallsTests(unittest.TestCase):
    def test_json_block(self):
        calls, found = parse_tool_calls(
            'sure <tool_call>{"name": "calculate", "arguments": '
            '{"expression": "2+2"}}</tool_call>')
        self.assertTrue(found)
        self.assertEqual(calls, [{"name": "calculate",
                                  "arguments":
                                      {"expression": "2+2"}}])

    def test_json_block_with_string_arguments(self):
        calls, found = parse_tool_calls(
            '<tool_call>{"name": "read_file", "arguments": '
            '"{\\"path\\": \\"a.txt\\"}"}</tool_call>')
        self.assertTrue(found)
        self.assertEqual(calls[0]["arguments"], {"path": "a.txt"})

    def test_xml_block(self):
        calls, found = parse_tool_calls(
            "<tool_call>\n<function=read_file>\n"
            "<parameter=path>notes.txt</parameter>\n"
            "</function>\n</tool_call>")
        self.assertTrue(found)
        self.assertEqual(calls, [{"name": "read_file",
                                  "arguments":
                                      {"path": "notes.txt"}}])

    def test_xml_numbers_become_values(self):
        calls, _found = parse_tool_calls(
            "<tool_call><function=calculate>"
            "<parameter=expression>2+2</parameter>"
            "</function></tool_call>")
        self.assertEqual(calls[0]["arguments"],
                         {"expression": "2+2"})

    def test_plain_text_has_no_calls(self):
        calls, found = parse_tool_calls("Just an answer.")
        self.assertFalse(found)
        self.assertEqual(calls, [])


class FileToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.folder = self.base / "tools"
        self.folder.mkdir()
        (self.folder / "notes.txt").write_text(
            "hello fyrn", encoding="utf-8")
        (self.base / "secret.txt").write_text(
            "outside", encoding="utf-8")
        self.executor = ToolExecutor(self.folder)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_write_list_round_trip(self):
        text, _ = self.executor.execute(
            "read_file", {"path": "notes.txt"})
        self.assertEqual(text, "hello fyrn")
        text, summary = self.executor.execute(
            "write_file", {"path": "sub/new.txt",
                           "content": "written by a tool"})
        self.assertIn("Wrote", text)
        self.assertEqual(
            (self.folder / "sub" / "new.txt").read_text(
                encoding="utf-8"),
            "written by a tool")
        text, _ = self.executor.execute("list_folder", {})
        self.assertIn("notes.txt", text)
        self.assertIn("sub/", text)

    def test_dotdot_escape_is_refused(self):
        text, _ = self.executor.execute(
            "read_file", {"path": "../secret.txt"})
        self.assertTrue(text.startswith("Refused:"))
        text, _ = self.executor.execute(
            "write_file", {"path": "../evil.txt", "content": "x"})
        self.assertTrue(text.startswith("Refused:"))
        self.assertFalse((self.base / "evil.txt").exists())

    def test_absolute_outside_path_is_refused(self):
        text, _ = self.executor.execute(
            "read_file", {"path": str(self.base / "secret.txt")})
        self.assertTrue(text.startswith("Refused:"))

    def test_symlink_escape_is_refused(self):
        link = self.folder / "link.txt"
        link.symlink_to(self.base / "secret.txt")
        text, _ = self.executor.execute(
            "read_file", {"path": "link.txt"})
        self.assertTrue(text.startswith("Refused:"))

    def test_missing_and_binary_files(self):
        text, _ = self.executor.execute(
            "read_file", {"path": "nope.txt"})
        self.assertIn("no file", text)
        (self.folder / "blob.bin").write_bytes(b"\x00\x01\x02")
        text, _ = self.executor.execute(
            "read_file", {"path": "blob.bin"})
        self.assertIn("not a text file", text)

    def test_big_file_is_capped_and_says_so(self):
        (self.folder / "big.txt").write_text(
            "a" * (70 * 1024), encoding="utf-8")
        text, _ = self.executor.execute(
            "read_file", {"path": "big.txt"})
        self.assertIn("only the first 64 KB", text)
        self.assertLess(len(text), 70 * 1024)

    def test_unknown_tool_is_a_plain_result(self):
        text, _ = self.executor.execute("fly_to_moon", {})
        self.assertIn("no tool called", text)


class CommandToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "tools"
        self.folder.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_approval_callback_means_refused(self):
        executor = ToolExecutor(self.folder)
        text, _ = executor.execute(
            "run_command", {"command": "touch sentinel"})
        self.assertIn("Refused", text)
        self.assertFalse((self.folder / "sentinel").exists())

    def test_denied_approval_never_executes(self):
        seen = []

        def approve(command, cwd):
            seen.append((command, cwd))
            return False

        executor = ToolExecutor(self.folder,
                                approve_command=approve)
        text, _ = executor.execute(
            "run_command", {"command": "touch sentinel"})
        self.assertIn("said no", text)
        self.assertEqual(seen, [("touch sentinel",
                                 str(self.folder))])
        self.assertFalse((self.folder / "sentinel").exists())

    def test_approved_command_runs_in_the_tools_folder(self):
        executor = ToolExecutor(
            self.folder, approve_command=lambda c, cwd: True)
        text, summary = executor.execute(
            "run_command", {"command": "echo hello-from-tool"})
        self.assertIn("Exit code 0", text)
        self.assertIn("hello-from-tool", text)
        self.assertIn("exit 0", summary)
        text, _ = executor.execute(
            "run_command", {"command": "touch made-it"})
        self.assertTrue((self.folder / "made-it").exists())

    def test_failing_command_reports_its_code(self):
        executor = ToolExecutor(
            self.folder, approve_command=lambda c, cwd: True)
        text, _ = executor.execute(
            "run_command", {"command": "exit 3"})
        self.assertIn("Exit code 3", text)


STUB_CLI = """#!/bin/sh
# A stub Forge Browser CLI: canned answers, Forge Browser's shapes.
case "$1" in
  search)
    echo '[{"title": "Stub Result", "url": "https://example.com/a", "snippet": "A stub snippet.", "engine": "stub"}]'
    ;;
  fetch)
    echo '{"url": "https://example.com/a", "final_url": "https://example.com/a", "title": "Stub Page", "markdown": "Stub page text.", "links": [], "total_chars": 15, "offset": 0, "truncated": false, "next_offset": null, "rendered": false}'
    ;;
  screenshot)
    echo "no rendering engine installed" >&2
    exit 2
    ;;
  app)
    echo "opened $2" >> "$(dirname "$0")/app-calls.log"
    ;;
  *)
    echo "unknown command" >&2
    exit 2
    ;;
esac
"""


def make_stub_forge_browser(base):
    """An installed-looking Forge Browser: a folder with run.sh
    driving the stub CLI, the way the real install lays it out."""
    app_dir = base / "forge-browser-app"
    app_dir.mkdir(parents=True)
    run_sh = app_dir / "run.sh"
    run_sh.write_text(STUB_CLI, encoding="utf-8")
    run_sh.chmod(run_sh.stat().st_mode | stat.S_IEXEC)
    return app_dir


class ForgeBrowserDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_override_folder_is_found(self):
        app_dir = make_stub_forge_browser(self.base)
        command = find_forge_browser_command(str(app_dir))
        self.assertEqual(command, ["sh", str(app_dir / "run.sh")])

    def test_override_run_sh_is_found(self):
        app_dir = make_stub_forge_browser(self.base)
        command = find_forge_browser_command(
            str(app_dir / "run.sh"))
        self.assertEqual(command, ["sh", str(app_dir / "run.sh")])

    def test_missing_browser_is_none(self):
        # A home with no install in it, and no override: not found.
        empty = self.base / "empty-home"
        empty.mkdir()
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(empty)
        try:
            command = find_forge_browser_command(
                str(self.base / "nowhere"))
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
        # PATH may still offer a forge-browser on the test machine;
        # in the sandbox there is none.
        if command is not None:
            self.assertNotIn("nowhere", command[-1])


class WebToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.folder = self.base / "tools"
        self.folder.mkdir()
        self.app_dir = make_stub_forge_browser(self.base)
        self.events = []
        self.executor = ToolExecutor(
            self.folder,
            forge_override=str(self.app_dir),
            on_event=lambda kind, data: self.events.append(
                (kind, data)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_search_uses_the_cli_and_formats_results(self):
        client = ForgeBrowserClient(override=str(self.app_dir))
        self.assertTrue(client.available)
        results = client.search("stub query")
        self.assertEqual(results[0]["title"], "Stub Result")
        text, summary = self.executor.execute(
            "web_search", {"query": "stub query"})
        self.assertIn("Stub Result", text)
        self.assertIn("https://example.com/a", text)
        self.assertIn("1 results", summary)
        web_events = [data for kind, data in self.events
                      if kind == "web"]
        self.assertEqual(web_events[-1]["kind"], "search")
        self.assertEqual(web_events[-1]["query"], "stub query")

    def test_read_fetches_the_page(self):
        text, summary = self.executor.execute(
            "web_read", {"url": "https://example.com/a"})
        self.assertIn("Stub page text.", text)
        self.assertIn("Stub Page", text)
        web_events = [data for kind, data in self.events
                      if kind == "web"]
        self.assertEqual(web_events[-1]["kind"], "page")
        self.assertEqual(web_events[-1]["url"],
                         "https://example.com/a")
        # The stub has no rendering engine: no screenshot, and
        # that is not an error.
        self.assertIsNone(web_events[-1]["screenshot"])

    def test_open_in_browser_calls_app(self):
        client = ForgeBrowserClient(override=str(self.app_dir))
        client.open_in_browser("https://example.com/a")
        log = self.app_dir / "app-calls.log"
        for _ in range(50):
            if log.exists():
                break
            import time as _time
            _time.sleep(0.1)
        self.assertIn("opened https://example.com/a",
                      log.read_text(encoding="utf-8"))

    def test_missing_browser_declines_plainly(self):
        executor = ToolExecutor(
            self.folder, forge_override=str(self.base / "nowhere"))
        # The usual install spot may exist on a dev machine; in
        # the sandbox it does not, so the decline is expected.
        text, _ = executor.execute(
            "web_search", {"query": "anything"})
        if executor.forge.available:
            self.skipTest("a real Forge Browser is installed here")
        self.assertIn("not installed", text)


class TimeToolTests(unittest.TestCase):
    def test_time_tool_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            executor = ToolExecutor(Path(tmp))
            text, _summary = executor.execute(
                "get_current_time", {})
            self.assertIn("current local date and time", text)


if __name__ == "__main__":
    unittest.main()
