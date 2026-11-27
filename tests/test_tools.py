"""Tests for fyrn.tools: the Forge Browser web connection
(the only tool calls Fyrn has since v0.13), the tool-call
text parser, and the absence of the removed built-in tools
(the calculator, the file tools, the command runner, the
clock: v0.8-v0.12, ordered out in v0.13). The web tests run
against a stub Forge Browser CLI the tests create. Nothing
here touches the network or a real browser.
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from fyrn.tools import (TOOL_NAMES, WEB_TOOL_DEFINITIONS,
                        BrowserToolExecutor, ForgeBrowserClient,
                        ToolFailure, find_forge_browser_command,
                        parse_tool_calls)


class RemovedToolsTests(unittest.TestCase):
    """The built-in tools are gone (v0.13): the definitions
    on offer are exactly the two web calls, and a call to any
    removed name is refused by name, as a plain result."""

    REMOVED = ("get_current_time", "calculate", "read_file",
               "write_file", "list_folder", "run_command")

    def test_only_the_web_calls_are_offered(self):
        names = [entry["function"]["name"]
                 for entry in WEB_TOOL_DEFINITIONS]
        self.assertEqual(sorted(names), ["web_read", "web_search"])
        self.assertEqual(TOOL_NAMES,
                         frozenset({"web_search", "web_read"}))

    def test_removed_names_are_refused_by_name(self):
        executor = BrowserToolExecutor()
        for name in self.REMOVED:
            text, summary = executor.execute(name, {})
            self.assertEqual(
                text, "There is no tool called %s." % name)
            self.assertEqual(summary, text)


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
        self.app_dir = make_stub_forge_browser(self.base)
        self.events = []
        self.executor = BrowserToolExecutor(
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
        executor = BrowserToolExecutor(
            forge_override=str(self.base / "nowhere"))
        # The usual install spot may exist on a dev machine; in
        # the sandbox it does not, so the decline is expected.
        text, _ = executor.execute(
            "web_search", {"query": "anything"})
        if executor.forge.available:
            self.skipTest("a real Forge Browser is installed here")
        self.assertIn("not installed", text)


if __name__ == "__main__":
    unittest.main()
