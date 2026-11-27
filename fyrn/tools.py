"""The Forge Browser web connection (v0.8; the built-in
tools were removed in v0.13).

This module is Fyrn's one outside connection: when the user
turns web browsing on for a chat, the model can search the
web and read pages through the installed Forge Browser's
command line. That is the whole of it. Fyrn has no built-in
tools of its own: no calculator, no clock, no file tools, no
command runner. (Before v0.13 a small built-in set rode
along with every chat; Joshua ordered them out, and the
browsing connection stayed because it is the one
integration he asked for.)

Everything here is Qt-free and stdlib-only, so it can be
unit tested headless. The GUI builds one BrowserToolExecutor
and hands its ``execute`` method to the runner; the runner
calls it between chat-completion rounds and feeds the
returned text back to the model as a tool message.

When Forge Browser is not found, the web calls decline
plainly: the refusal text goes back to the model as the
call's result, and the chat carries on without the web.

Every result is a plain string for the model. Refusals and
errors are returned as text too (never raised out of
execute), so the model can react to them and the user sees
them.
"""

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

# How much of a web page web_read hands to the model.
WEB_READ_CAP_CHARS = 12000


WEB_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web with Forge Browser. Returns the "
                "top results with titles, links and snippets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_read",
            "description": (
                "Read a web page with Forge Browser and return "
                "its text. Give the full URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                },
                "required": ["url"],
            },
        },
    },
]

TOOL_NAMES = frozenset(
    entry["function"]["name"] for entry in WEB_TOOL_DEFINITIONS
)


class ToolFailure(Exception):
    """A plain-words failure from the web connection, shown
    to the user and the model. execute() turns these into
    result text."""


# ----------------------------------------------------------------
# Tool-call text parsing (fallback for models whose binding
# returns the call as text in the reply instead of parsed
# tool_calls — some chat templates render calls as XML).
# ----------------------------------------------------------------

_TOOL_CALL_JSON_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_TOOL_CALL_XML_RE = re.compile(
    r"<tool_call>\s*<function=([^>\s]+)>(.*?)</function>\s*"
    r"</tool_call>", re.DOTALL)
_TOOL_PARAM_RE = re.compile(
    r"<parameter=([^>\s]+)>(.*?)</parameter>", re.DOTALL)


def _coerce_param(value):
    """XML tool-call parameters arrive as text; turn numbers and
    JSON back into values when they clearly are."""
    text = value.strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def parse_tool_calls(text):
    """Pull tool calls out of a model reply's text.

    Returns (calls, found): calls is a list of
    {"name": str, "arguments": dict}; found is True when the text
    held at least one recognisable call block. Two shapes are
    recognised: the JSON block (<tool_call>{"name": ...,
    "arguments": {...}}</tool_call>) and the XML block some
    templates render (<tool_call><function=name>
    <parameter=k>v</parameter></function></tool_call>)."""
    calls = []
    if not text:
        return calls, False
    for match in _TOOL_CALL_JSON_RE.finditer(text):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        name = data.get("name")
        args = data.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if isinstance(name, str) and name:
            calls.append({"name": name,
                          "arguments": args if isinstance(
                              args, dict) else {}})
    if calls:
        return calls, True
    for match in _TOOL_CALL_XML_RE.finditer(text):
        args = {}
        for param in _TOOL_PARAM_RE.finditer(match.group(2)):
            args[param.group(1)] = _coerce_param(param.group(2))
        calls.append({"name": match.group(1), "arguments": args})
    return calls, bool(calls)


# ----------------------------------------------------------------
# Forge Browser command line.
# ----------------------------------------------------------------

def forge_browser_candidates(override=None):
    """Where the Forge Browser app may live, best first.

    An override (a folder, or the run.sh inside one) wins. Then
    the usual install spot for the app, then a forge-browser
    command on PATH."""
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    home_app = Path.home() / ".local" / "share" / "forge-browser-app"
    candidates.append(home_app)
    on_path = shutil.which("forge-browser")
    if on_path:
        candidates.append(Path(on_path))
    return candidates


def find_forge_browser_command(override=None):
    """The command prefix that runs Forge Browser's CLI, e.g.
    ["sh", "/home/user/.local/share/forge-browser-app/run.sh"],
    or None when Forge Browser is not installed."""
    for candidate in forge_browser_candidates(override):
        try:
            if candidate.is_dir():
                run_sh = candidate / "run.sh"
                if run_sh.is_file():
                    return ["sh", str(run_sh)]
                cli = candidate / "forge-browser"
                if cli.is_file():
                    return [str(cli)]
            elif candidate.is_file():
                if candidate.name == "run.sh":
                    return ["sh", str(candidate)]
                return [str(candidate)]
        except OSError:
            continue
    return None


class ForgeBrowserClient:
    """Runs the installed Forge Browser's CLI commands and hands
    back parsed results. Raises ToolFailure with a plain sentence
    when Forge Browser is missing or a command fails."""

    def __init__(self, override=None, timeout_s=90):
        self._override = override
        self._timeout_s = timeout_s
        self._prefix = find_forge_browser_command(override)

    @property
    def available(self):
        return self._prefix is not None

    @property
    def command(self):
        return list(self._prefix) if self._prefix else None

    def refresh(self):
        self._prefix = find_forge_browser_command(self._override)
        return self.available

    def _run(self, args, timeout_s=None):
        if self._prefix is None:
            raise ToolFailure(
                "Forge Browser is not installed, so I cannot use "
                "the web. The user can install Forge Browser, or "
                "set its folder in Settings.")
        try:
            proc = subprocess.run(
                self._prefix + list(args),
                capture_output=True, text=True,
                timeout=timeout_s or self._timeout_s)
        except subprocess.TimeoutExpired:
            raise ToolFailure(
                "Forge Browser took too long and was stopped.")
        except OSError:
            raise ToolFailure("Forge Browser could not be started.")
        if proc.returncode != 0:
            line = (proc.stderr or proc.stdout or "").strip()
            line = line.splitlines()[0] if line else "unknown error"
            raise ToolFailure(f"Forge Browser said: {line}")
        return proc.stdout

    def search(self, query, max_results=5):
        """A list of {"title", "url", "snippet"} dicts."""
        out = self._run(["search", query, "--max",
                         str(max_results), "--json"])
        try:
            results = json.loads(out)
        except ValueError:
            raise ToolFailure(
                "Forge Browser's search answer could not be read.")
        if not isinstance(results, list):
            raise ToolFailure(
                "Forge Browser's search answer could not be read.")
        return [r for r in results if isinstance(r, dict)]

    def fetch(self, url, max_chars=WEB_READ_CAP_CHARS):
        """A page dict: url, final_url, title, markdown, ..."""
        out = self._run(["fetch", url, "--json", "--max-chars",
                         str(max_chars)])
        try:
            page = json.loads(out)
        except ValueError:
            raise ToolFailure(
                "Forge Browser's page answer could not be read.")
        if not isinstance(page, dict):
            raise ToolFailure(
                "Forge Browser's page answer could not be read.")
        return page

    def screenshot(self, url, out_path):
        """Save a PNG screenshot of a page. Returns the path, or
        None when Forge Browser cannot render one (no rendering
        engine installed) — a missing screenshot is not an error
        worth failing the tool call over."""
        if self._prefix is None:
            return None
        try:
            proc = subprocess.run(
                self._prefix + ["screenshot", url, "--out",
                                str(out_path)],
                capture_output=True, text=True,
                timeout=self._timeout_s)
        except (subprocess.TimeoutExpired, OSError):
            return None
        path = Path(out_path)
        if proc.returncode == 0 and path.is_file() \
                and path.stat().st_size > 0:
            return path
        return None

    def open_in_browser(self, url):
        """Open the real Forge Browser window on a page."""
        if self._prefix is None:
            raise ToolFailure(
                "Forge Browser is not installed, so there is no "
                "browser window to open.")
        try:
            subprocess.Popen(
                self._prefix + ["app", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError:
            raise ToolFailure("Forge Browser could not be opened.")


def format_search_results(results):
    """Search results as plain text for the model."""
    if not results:
        return "The search found no results."
    lines = []
    for index, result in enumerate(results, 1):
        lines.append(f"{index}. {result.get('title', '')}".rstrip())
        lines.append(f"   {result.get('url', '')}")
        snippet = (result.get("snippet") or "").strip()
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)


# ----------------------------------------------------------------


# ----------------------------------------------------------------
# The executor: runs the model's web calls.
# ----------------------------------------------------------------

class BrowserToolExecutor:
    """Runs web_search and web_read through Forge Browser.

    forge_override: optional Forge Browser location override
    (the Settings folder, when the user set one).
    on_event: callable(kind, data) - kind "web" carries
    browser view updates ({"kind": "search"|"page", ...}).
    shots_dir: where web screenshots are saved (optional).
    """

    def __init__(self, forge_override=None, on_event=None,
                 shots_dir=None):
        self._on_event = on_event
        self._shots_dir = Path(shots_dir) if shots_dir else None
        self.forge = ForgeBrowserClient(override=forge_override)

    def execute(self, name, arguments):
        """Run one web call. Returns (result_text, summary):
        result_text goes back to the model; summary is one
        short line for the transcript. Never raises - a
        refusal or a failure is a result like any other, and
        a call to anything that is not a web tool is refused
        by name: Fyrn offers nothing else."""
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            text, summary = self._dispatch(name, arguments)
            return text, summary
        except ToolFailure as exc:
            message = str(exc)
            return message, message

    def _emit(self, kind, data):
        if self._on_event is not None:
            try:
                self._on_event(kind, data)
            except Exception:
                pass  # a broken view must never break the call

    def _dispatch(self, name, args):
        handler = {
            "web_search": self._web_search,
            "web_read": self._web_read,
        }.get(name)
        if handler is None:
            raise ToolFailure(f"There is no tool called {name}.")
        return handler(args)

    def _web_search(self, args):
        query = str(args.get("query", "")).strip()
        if not query:
            raise ToolFailure("The search needs a query.")
        self._emit("web", {"kind": "busy",
                           "text": f"Searching for: {query}"})
        results = self.forge.search(query)
        text = format_search_results(results)
        self._emit("web", {"kind": "search", "query": query,
                           "results": results, "text": text})
        return text, f"searched for \"{query}\" " \
                     f"({len(results)} results)"

    def _web_read(self, args):
        url = str(args.get("url", "")).strip()
        if not url:
            raise ToolFailure("Reading a page needs its URL.")
        self._emit("web", {"kind": "busy",
                           "text": f"Reading: {url}"})
        page = self.forge.fetch(url)
        final_url = page.get("final_url") or url
        title = page.get("title") or ""
        markdown = page.get("markdown") or ""
        screenshot = None
        if self._shots_dir is not None:
            try:
                self._shots_dir.mkdir(parents=True, exist_ok=True)
                shot_path = self._shots_dir / (
                    f"page-{int(time.time() * 1000)}.png")
                screenshot = self.forge.screenshot(final_url,
                                                   shot_path)
            except OSError:
                screenshot = None
        self._emit("web", {
            "kind": "page", "url": final_url, "title": title,
            "markdown": markdown,
            "screenshot": str(screenshot) if screenshot else None,
        })
        text = markdown
        if page.get("truncated"):
            text += ("\n[The page is longer; only part of it is "
                     "shown.]")
        if title:
            text = f"{title}\n{final_url}\n\n{text}"
        return text, f"read {final_url}"
