"""Tools the loaded model can call mid-chat (v0.8).

Everything here is Qt-free and stdlib-only, so it can be unit
tested headless. The GUI builds one ToolExecutor and hands its
``execute`` method to the runner; the runner calls it between
chat-completion rounds and feeds the returned text back to the
model as a tool message.

Tools on offer:
- get_current_time: the local date and time.
- calculate: a safe arithmetic evaluator (an AST walk, no eval).
- read_file / write_file / list_folder: text files ONLY inside
  one folder the user picked (the tools folder). Any path that
  escapes that folder — ``..``, a symlink, an absolute path
  outside — is refused, and the refusal text goes back to the
  model (and shows in the transcript).
- web_search / web_read: the installed Forge Browser's command
  line (its ``search --json`` and ``fetch --json`` commands).
  When Forge Browser is not found, the tools decline plainly.
- run_command: proposes a shell command; an approval callback
  (the GUI's modal) must say yes before anything runs. The user
  never opens a terminal — the model runs commands, the user
  approves them.

Every result is a plain string for the model. Refusals and
errors are returned as text too (never raised out of execute),
so the model can react to them and the user sees them.
"""

import ast
import json
import math
import re
import shutil
import subprocess
import time
from pathlib import Path

# How much of a file read_file hands to the model.
READ_CAP_BYTES = 64 * 1024
# How much write_file will write in one call.
WRITE_CAP_BYTES = 1024 * 1024
# How much of a web page web_read hands to the model.
WEB_READ_CAP_CHARS = 12000
# How much command output goes back to the model.
COMMAND_OUTPUT_CAP_CHARS = 16 * 1024
# run_command hard timeout.
COMMAND_TIMEOUT_S = 60


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": (
                "Get the current local date and time on this PC."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": (
                "Do an arithmetic calculation, e.g. '23 * 4 + 5' "
                "or 'sqrt(2) / 2'. Numbers and + - * / // % ** "
                "plus sqrt, sin, cos, tan, log, log10, exp, floor, "
                "ceil, abs, round and the constants pi and e."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "The arithmetic to evaluate.",
                    },
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a text file inside the user's tools folder. "
                "Give the path relative to that folder, e.g. "
                "'notes.txt' or 'sub/plan.txt'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write text to a file inside the user's tools "
                "folder, replacing it if it exists. Give the path "
                "relative to that folder."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_folder",
            "description": (
                "List the files and folders inside the user's "
                "tools folder, or inside a subfolder of it. The "
                "path argument is optional; leave it out for the "
                "tools folder itself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": [],
            },
        },
    },
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
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command on this PC, in the user's "
                "tools folder. The user sees the exact command and "
                "must approve it before it runs; if they say no, "
                "nothing runs. Returns the command's output."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    },
]

TOOL_NAMES = frozenset(
    entry["function"]["name"] for entry in TOOL_DEFINITIONS
)


class ToolFailure(Exception):
    """A plain-words tool failure, shown to the user and the
    model. execute() turns these into result text."""


# ----------------------------------------------------------------
# Calculator: an AST walk over arithmetic only.
# ----------------------------------------------------------------

_CALC_FUNCTIONS = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
    "abs": abs,
    "round": round,
}
_CALC_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
_CALC_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
                ast.Mod, ast.Pow)


def _calc_value(node):
    if isinstance(node, ast.Expression):
        return _calc_value(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(
                node.value, (int, float)):
            raise ToolFailure("That is not arithmetic I can do.")
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(
            node.op, _CALC_BINOPS):
        left = _calc_value(node.left)
        right = _calc_value(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
        if isinstance(node.op, ast.Mod):
            return left % right
        # Power: keep it small, so a stray 10**10**10 cannot hang.
        if abs(right) > 1000 or abs(left) > 10 ** 6:
            raise ToolFailure("That power is too big to work out.")
        return left ** right
    if isinstance(node, ast.UnaryOp) and isinstance(
            node.op, (ast.UAdd, ast.USub)):
        value = _calc_value(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.Name) and node.id in _CALC_CONSTANTS:
        return _CALC_CONSTANTS[node.id]
    if isinstance(node, ast.Call) and isinstance(
            node.func, ast.Name) and node.func.id in _CALC_FUNCTIONS:
        if node.keywords:
            raise ToolFailure("That is not arithmetic I can do.")
        args = [_calc_value(arg) for arg in node.args]
        return _CALC_FUNCTIONS[node.func.id](*args)
    raise ToolFailure("That is not arithmetic I can do.")


def calculate(expression):
    """Evaluate an arithmetic expression string, safely.

    Raises ToolFailure with a plain sentence for anything that is
    not plain arithmetic (names, calls to other functions,
    attribute access, statements, division by zero, ...)."""
    if not isinstance(expression, str) or not expression.strip():
        raise ToolFailure("There is nothing to calculate.")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        raise ToolFailure("That is not arithmetic I can do.")
    try:
        value = _calc_value(tree)
    except ToolFailure:
        raise
    except (ArithmeticError, ValueError, TypeError, OverflowError):
        raise ToolFailure("That calculation does not work out.")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{expression.strip()} = {value}"


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
# The executor: one place that runs every tool.
# ----------------------------------------------------------------

class ToolExecutor:
    """Runs tool calls and returns result text for the model.

    tools_folder: the only folder the file tools may touch.
    forge_override: optional Forge Browser location override.
    approve_command: callable(command, cwd) -> bool, consulted
        before run_command executes anything. None means commands
        are refused outright.
    on_event: callable(kind, data) — kind "web" carries browser
        view updates ({"kind": "search"|"page", ...}).
    shots_dir: where web screenshots are saved (optional)."""

    def __init__(self, tools_folder, forge_override=None,
                 approve_command=None, on_event=None, shots_dir=None):
        self.tools_folder = Path(tools_folder).expanduser()
        self._approve_command = approve_command
        self._on_event = on_event
        self._shots_dir = Path(shots_dir) if shots_dir else None
        self.forge = ForgeBrowserClient(override=forge_override)

    # -- plumbing ---------------------------------------------

    def execute(self, name, arguments):
        """Run one tool call. Returns (result_text, summary):
        result_text goes back to the model; summary is one short
        line for the transcript. Never raises — a refusal or a
        failure is a result like any other."""
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
                pass  # a broken view must never break the tool

    def _dispatch(self, name, args):
        handler = {
            "get_current_time": self._time,
            "calculate": self._calculate,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "list_folder": self._list_folder,
            "web_search": self._web_search,
            "web_read": self._web_read,
            "run_command": self._run_command,
        }.get(name)
        if handler is None:
            raise ToolFailure(f"There is no tool called {name}.")
        return handler(args)

    # -- the simple tools --------------------------------------

    def _time(self, args):
        text = time.strftime(
            "%A, %B %d, %Y, %I:%M %p %Z", time.localtime())
        return f"The current local date and time is {text}.", text

    def _calculate(self, args):
        expression = args.get("expression", "")
        result = calculate(expression)
        return result, result

    # -- file tools (sandboxed to the tools folder) ------------

    def _inside(self, raw_path):
        """Resolve a model-given path inside the tools folder.

        Returns the resolved Path. Raises ToolFailure when the
        path escapes the folder — by .., by symlink, or by being
        an absolute path outside it."""
        base = self.tools_folder.resolve()
        raw = str(raw_path or "").strip()
        if not raw:
            return base
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        resolved = candidate.resolve()
        if resolved != base and base not in resolved.parents:
            raise ToolFailure(
                f"Refused: {raw} is outside the tools folder "
                f"({base}). The file tools only work inside that "
                "folder.")
        return resolved

    def _relative(self, path):
        base = self.tools_folder.resolve()
        try:
            return str(path.relative_to(base)) or "."
        except ValueError:
            return str(path)

    def _read_file(self, args):
        path = self._inside(args.get("path", ""))
        if not path.is_file():
            raise ToolFailure(
                f"There is no file at {self._relative(path)} in "
                "the tools folder.")
        try:
            with open(path, "rb") as fh:
                blob = fh.read(READ_CAP_BYTES + 1)
        except OSError:
            raise ToolFailure(
                f"{self._relative(path)} could not be read.")
        truncated = len(blob) > READ_CAP_BYTES
        blob = blob[:READ_CAP_BYTES]
        if b"\x00" in blob:
            raise ToolFailure(
                f"{self._relative(path)} is not a text file, so "
                "I cannot read it.")
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolFailure(
                f"{self._relative(path)} is not a text file, so "
                "I cannot read it.")
        if truncated:
            text += ("\n[The file is bigger than 64 KB; only the "
                     "first 64 KB is shown.]")
        summary = f"read {self._relative(path)} " \
                  f"({len(blob)} bytes)"
        return text, summary

    def _write_file(self, args):
        path = self._inside(args.get("path", ""))
        content = args.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        blob = content.encode("utf-8")
        if len(blob) > WRITE_CAP_BYTES:
            raise ToolFailure(
                "That is more than 1 MB of text; too much to write "
                "in one go.")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError:
            raise ToolFailure(
                f"{self._relative(path)} could not be written.")
        summary = f"wrote {self._relative(path)} ({len(blob)} bytes)"
        return f"Wrote {len(blob)} bytes to {self._relative(path)}.", \
            summary

    def _list_folder(self, args):
        path = self._inside(args.get("path", ""))
        if not path.is_dir():
            raise ToolFailure(
                f"{self._relative(path)} is not a folder in the "
                "tools folder.")
        try:
            entries = sorted(path.iterdir(),
                             key=lambda p: (not p.is_dir(), p.name))
        except OSError:
            raise ToolFailure(
                f"{self._relative(path)} could not be listed.")
        lines = []
        for entry in entries[:200]:
            if entry.is_dir():
                lines.append(f"{entry.name}/")
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"{entry.name} ({size} bytes)")
        if len(entries) > 200:
            lines.append("[... and more; only the first 200 "
                         "entries are shown.]")
        if not lines:
            lines.append("(the folder is empty)")
        text = f"Contents of {self._relative(path)}:\n" + \
            "\n".join(lines)
        return text, f"listed {self._relative(path)} " \
                     f"({len(entries)} entries)"

    # -- web tools (Forge Browser) ------------------------------

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

    # -- run a command (approval-gated) --------------------------

    def _run_command(self, args):
        command = str(args.get("command", "")).strip()
        if not command:
            raise ToolFailure("There is no command to run.")
        cwd = self.tools_folder
        if self._approve_command is None:
            raise ToolFailure(
                "Refused: commands cannot run here, because there "
                "is nobody to approve them.")
        try:
            cwd.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        approved = self._approve_command(command, str(cwd))
        if not approved:
            raise ToolFailure(
                "The user said no to that command, so it was not "
                "run.")
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", command], cwd=str(cwd),
                capture_output=True, text=True,
                timeout=COMMAND_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise ToolFailure(
                f"The command ran for {COMMAND_TIMEOUT_S} seconds "
                "and was stopped.")
        except OSError:
            raise ToolFailure("The command could not be started.")
        output = (proc.stdout or "") + (proc.stderr or "")
        output = output.strip()
        if len(output) > COMMAND_OUTPUT_CAP_CHARS:
            output = output[:COMMAND_OUTPUT_CAP_CHARS] + \
                "\n[Output cut off.]"
        if not output:
            output = "(no output)"
        text = f"Exit code {proc.returncode}.\n{output}"
        return text, f"ran: {command} (exit {proc.returncode})"
