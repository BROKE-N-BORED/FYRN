"""Fyrn window: models, pull, settings, chat.

Native Qt desktop window. One function, one window (U1): the main
window holds the Models and Chat tabs, while Pull, Settings, and How
to Use each get their own closable window.

Visible text rules: no em-dashes (U33) and no bare "&" (U11) in any
label, button, or menu text. The suite sweeps for both.
"""

import hashlib
import os
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import (QEvent, QEasingCurve, QLockFile, QPoint,
                            QPropertyAnimation, QRect, QSettings,
                            QSignalBlocker, Qt, QThread, QTimer, Signal)
from shiboken6 import isValid
from PySide6.QtGui import (QAction, QCursor, QGuiApplication, QIcon,
                           QKeySequence, QPixmap, QShortcut)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                               QDialog, QFileDialog, QFrame,
                               QGraphicsOpacityEffect, QGridLayout,
                               QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QProgressBar,
                               QProgressDialog, QPushButton, QScrollArea,
                               QSizePolicy, QSpacerItem, QSystemTrayIcon,
                               QTabWidget, QTextEdit, QToolTip,
                               QVBoxLayout, QWidget)

from . import __version__
from . import core
from .chats import ChatStore, auto_title, history_from_messages
from .core import (HEADROOM_BYTES, ImportCancelled, ModelController,
                   ModelNotFoundError, ModelTooBigError, ensure_home,
                   ensure_models_folder, fit_status, format_chat_export,
                   format_facts, format_size, list_part_files,
                   model_headroom_bytes, move_to_trash,
                   projector_size_bytes, read_ram_bytes, scan_models)
from .discover import discover_ollama_models
# The pull module is imported lazily, inside the methods that use
# it: it costs ~0.4 s of cold start, and nothing needs it until a
# pull actually runs.
from .runner import (GenerationCancelled, LoadCancelled, LlamaRunner,
                     RunnerError, ToolsUnsupportedError,
                     auto_context_tokens, trim_history, warm_engine)
from .theme import (DARK, HIGH_CONTRAST, THEMES, build_stylesheet,
                    fit_badges, rgba_string)
from .tools import ToolExecutor

DEFAULT_WIDTH = 1100
DEFAULT_HEIGHT = 700
SETTINGS_ORG = "BrokeNBored"
SETTINGS_APP = "fyrn"

_HERE = Path(__file__).resolve().parent
ICON_PATH = _HERE.parent / "assets" / "fyrn.png"

# The look: one stylesheet per theme, built from the design
# tokens in fyrn/theme.py and applied by main() across every
# window. No fixed point sizes anywhere: the application font
# (and the user's system text size) shows through. The motion in
# the app is small and cheap: two one-shot 200 ms opacity fades
# (pull finish, newly pulled card), a 150 ms press dip on
# Send/Load/Pull, and a 200 ms 4 px hover lift on cards. The
# reduced-motion fallback skips all of it.
DARK_STYLESHEET = build_stylesheet(DARK)
HIGH_CONTRAST_STYLESHEET = build_stylesheet(HIGH_CONTRAST)


def get_reduce_motion():
    """The Reduce motion setting (Settings window)."""
    settings = get_settings()
    return bool(settings.value("reduce_motion", False, type=bool))


def set_reduce_motion(value):
    settings = get_settings()
    settings.setValue("reduce_motion", bool(value))
    settings.sync()


def reduced_motion():
    """True when motion should be skipped (reduced-motion fallback).

    The Settings window's Reduce motion switch, or the
    FYRN_REDUCED_MOTION=1 environment variable as an override.
    The state changes still happen; only the animation is skipped,
    so the celebration and the new-card pulse become instant.
    """
    if os.environ.get(
            "FYRN_REDUCED_MOTION", "").lower() in ("1", "true",
                                                   "yes"):
        return True
    return get_reduce_motion()


def get_show_ollama_models():
    """Whether the models Ollama already downloaded are listed
    (Settings window). On by default: the models are already on
    the disk, so listing them costs nothing and saves a pull."""
    settings = get_settings()
    return bool(settings.value("show_ollama_models", True,
                               type=bool))


def set_show_ollama_models(value):
    settings = get_settings()
    settings.setValue("show_ollama_models", bool(value))
    settings.sync()


def get_high_contrast():
    """The high-contrast theme (Settings window). Off by
    default: the dark theme is the look Fyrn has always had."""
    settings = get_settings()
    return bool(settings.value("high_contrast", False, type=bool))


def set_high_contrast(value):
    settings = get_settings()
    settings.setValue("high_contrast", bool(value))
    settings.sync()


def get_compact_list():
    """The compact model list (Settings window). Off by
    default: full-size cards, three across."""
    settings = get_settings()
    return bool(settings.value("compact_list", False, type=bool))


def set_compact_list(value):
    settings = get_settings()
    settings.setValue("compact_list", bool(value))
    settings.sync()


def get_chime_muted():
    """The pull-complete chime mute (Settings window). Off by
    default: the chime plays when a pull finishes, per the
    library entry's rule (default on, mute available)."""
    settings = get_settings()
    return bool(settings.value("chime_muted", False, type=bool))


def set_chime_muted(value):
    settings = get_settings()
    settings.setValue("chime_muted", bool(value))
    settings.sync()


def get_update_check():
    """Whether Fyrn checks for a newer release when it starts
    (Settings window). On by default: the standing order is that
    the app checks on launch, silently, and asks before it
    downloads anything."""
    settings = get_settings()
    return bool(settings.value("update_check", True, type=bool))


def set_update_check(value):
    settings = get_settings()
    settings.setValue("update_check", bool(value))
    settings.sync()


def default_tools_folder():
    """Where the file tools work until the user picks a folder:
    a visible folder in the home directory, created on use."""
    return str(Path.home() / "Fyrn files")


def get_tools_folder():
    """The one folder the model's file tools may touch (Settings
    window, or the button on the Chat tab)."""
    settings = get_settings()
    value = settings.value("tools_folder", "", type=str)
    return value if value else default_tools_folder()


def set_tools_folder(value):
    settings = get_settings()
    settings.setValue("tools_folder", str(value))
    settings.sync()


def get_forge_browser_path():
    """An optional override for where Forge Browser is installed
    (Settings window). Empty means: find it in its usual place."""
    settings = get_settings()
    return settings.value("forge_browser_path", "", type=str)


def set_forge_browser_path(value):
    settings = get_settings()
    settings.setValue("forge_browser_path", str(value))
    settings.sync()


def current_tokens():
    """The active theme's tokens: high-contrast when that
    setting is on, the dark theme otherwise."""
    return THEMES["high-contrast" if get_high_contrast()
                  else "dark"]


def current_stylesheet():
    """The active theme's stylesheet."""
    return build_stylesheet(current_tokens())

HOW_TO_USE_SECTIONS = {
    "First steps": (
        "New here? Start here.\n\n"
        "1. Get a model. Open the Models menu and pull one, or "
        "import a .gguf file you already have. The Pull window "
        "suggests a small first model that fits this PC.\n\n"
        "2. Load it. Pick the model on the Models tab and press "
        "Load. Only one model runs at a time.\n\n"
        "3. Chat. Open the Chat tab, type a message, and press "
        "Send.\n\n"
        "That is the whole flow. The other sections below explain "
        "each part in more detail."
    ),
    "Models": (
        "Models are the brains this program runs. They live in your "
        "models folder as .gguf files.\n\n"
        "The Models tab lists every model in the folder, with its "
        "size and a fit badge: Fits means it runs fine on this PC, "
        "Tight means it will run but leaves little spare RAM, Too "
        "big means it will not fit in free RAM. Each card also "
        "shows a short factual line from the model's own file, "
        "such as its size in parameters, its quantization, and "
        "its context length, when the file carries that "
        "information.\n\n"
        "Search the list by name as you type, or narrow it with "
        "the filter beside the search box: by size, or by where "
        "the model came from.\n\n"
        "Fyrn also lists the models Ollama already downloaded on "
        "this PC, so you do not have to pull them again. Those "
        "models stay where Ollama put them: Fyrn can load them, "
        "but it cannot remove them. Turn the listing off in "
        "Settings if you only want your models folder.\n\n"
        "Pick a model and press Load. Double-click a model to load "
        "it straight away. Only one model runs at a time: loading "
        "a second one unloads the first. Press Unload when you are "
        "done to give the RAM back.\n\n"
        "Keyboard works too: Tab moves between models, Space picks "
        "the one in focus, Enter loads it.\n\n"
        "Import brings in a .gguf file from anywhere on the disk, "
        "such as a fine-tuned model you made elsewhere. It copies "
        "the file into your models folder, with progress and a "
        "cancel while it runs. You can also drag a .gguf file "
        "onto the window to import it.\n\n"
        "Remove moves a model to the desktop trash, so you can "
        "get it back if you change your mind. It asks first, "
        "names the file and its size, and a loaded model is "
        "unloaded before it is removed."
    ),
    "Pulling a model": (
        "Pull downloads a model from the internet into your models "
        "folder. Open it from the Models menu.\n\n"
        "Type a Hugging Face model and its file name, like "
        "myname/mymodel mymodel.gguf, or paste a direct link to a "
        ".gguf file. If the text is not a model Fyrn can "
        "understand, the window says so as soon as you move on, "
        "before Pull is pressed. Then press Pull.\n\n"
        "Not sure what to get first? The Pull window suggests a "
        "small first model that fits this PC.\n\n"
        "The window shows progress, how much has downloaded, the "
        "speed, and roughly how long is left. Press Cancel to "
        "stop; the part already downloaded stays, and pulling "
        "again picks up where it stopped instead of starting "
        "over.\n\n"
        "To pull several models, add each one to the queue, then "
        "press Pull queue: they download one after another, in "
        "order. If one fails or is stopped, the rest stay queued.\n\n"
        "Pulling a model that is already in your folder asks "
        "whether to pull it again and replace it. The old file is "
        "kept until the new download finishes. A model that is "
        "loaded right now cannot be replaced until it is unloaded.\n\n"
        "Unfinished downloads are listed at the bottom of the Pull "
        "window: Resume picks one up, Delete clears it away.\n\n"
        "Fyrn checks a finished pull against the checksum Hugging "
        "Face publishes, so a corrupt download never becomes a "
        "model.\n\n"
        "When the pull finishes, the model appears in the Models tab."
    ),
    "Settings": (
        "Settings lives in the File menu. It has nine settings: "
        "the models folder, where pulled and imported models are "
        "kept; whether Ollama's already-downloaded models are "
        "listed; Reduce motion; Compact model list; High-contrast "
        "mode; the pull-complete chime mute; whether Fyrn "
        "checks for a newer release when it starts; the tools "
        "folder; and an optional Forge Browser folder.\n\n"
        "Press Browse to pick a different models folder, then Save. "
        "The Models tab switches to the new folder right away.\n\n"
        "Check Reduce motion if movement on screen bothers you: "
        "the short fades and lifts become instant instead.\n\n"
        "Compact model list makes the cards smaller, so more "
        "models fit on screen at once. High-contrast mode is "
        "black and white with yellow highlights. Mute the "
        "pull-complete chime turns off the short sound that plays "
        "when a pull finishes.\n\n"
        "Check for updates when Fyrn starts is on by default: "
        "Fyrn quietly asks GitHub whether a newer release is out "
        "when it starts, and says so only when one is. Turn it "
        "off to check by hand instead, from the Help menu.\n\n"
        "The tools folder is the one folder a tool-using model "
        "can read and write files in. The Forge Browser folder "
        "is optional: Fyrn finds Forge Browser in its usual "
        "install place on its own, and the setting only matters "
        "when yours lives somewhere else.\n\n"
        "Everything else about how models run (threads, context "
        "size) is set by the program itself to fit this PC."
    ),
    "Chatting": (
        "Load a model first, then open the Chat tab.\n\n"
        "Type a message and press Send (or Enter). The reply streams "
        "in as the model writes it. Press Stop to cut a reply short. "
        "Shift+Enter starts a new line without sending. Clear "
        "empties the transcript.\n\n"
        "Your chats save themselves as you talk. The list on the "
        "left of the Chat tab holds every saved chat, named from "
        "its first message. Click one to reopen it, with its whole "
        "conversation back in place; New chat starts a fresh one; "
        "Rename and Delete work on the open chat. A chat reopens "
        "even when its model is not the one loaded: send, and the "
        "loaded model answers with the saved conversation as its "
        "context. Chats are plain files in ~/.fyrn/chats, one file "
        "per chat.\n\n"
        "The box above the transcript holds instructions for this "
        "chat: tell the model how to act ('You are a patient "
        "teacher', 'Always answer in plain words'), and it follows "
        "them for this chat only. They save with the chat, and a "
        "change applies from your next message. Leave the box "
        "empty for no instructions.\n\n"
        "Some models can see pictures: a vision model has a "
        "projector file (its name starts with mmproj) sitting next "
        "to the model file. With one of those loaded, Attach image "
        "lets you attach a picture, then ask about it in your "
        "message. If the loaded model cannot see, Attach image "
        "stays greyed out.\n\n"
        "If no model is loaded, Send stays greyed out. Load a model "
        "from the Models tab first."
    ),
    "Tools": (
        "Some models can use tools while they answer: they can "
        "look things up and do small jobs, then answer with what "
        "they found. The line above the transcript says whether "
        "the loaded model can. A model that cannot use tools just "
        "chats, exactly as before.\n\n"
        "Every tool call shows in the transcript as it happens: "
        "the tool's name, what it did, and a short result line. "
        "Nothing happens out of sight.\n\n"
        "Files: the model can read and write text files and list "
        "what is there, but only inside the tools folder (Settings, "
        "or the Tools folder button on the Chat tab). Anything "
        "outside that folder is refused, and the refusal shows.\n\n"
        "The web: searches and page reads go through Forge "
        "Browser, the browser app on this PC. The Browser view "
        "button (and the window that opens when a web tool runs) "
        "shows the search or the page as it comes back, and its "
        "Open in Forge Browser button opens the real Forge "
        "Browser on the same page. Without Forge Browser "
        "installed, the web tools say so and the rest still work.\n\n"
        "Commands: the model can ask to run a command. Fyrn shows "
        "the exact command and the folder it runs in, and it runs "
        "only when you press Run: Cancel is the button it lands "
        "on. The output comes back into the chat. You never need "
        "a terminal: the model runs commands, you approve them.\n\n"
        "Time and calculator: the model can ask for the current "
        "date and time, and work out arithmetic."
    ),
    "Stats": (
        "While a model runs, the Chat tab shows live numbers: RAM "
        "used and free, speed in tokens per second, time to first "
        "token, and how long the model took to load.\n\n"
        "These are measurements to watch. They are not controls: the "
        "program adjusts itself behind the scenes.\n\n"
        "The tray icon, when your desktop has one, shows the loaded "
        "model and free RAM at a glance. Click it to bring Fyrn "
        "forward."
    ),
    "If something goes wrong": (
        "A model that will not fit gives a plain message and is not "
        "loaded. Close other programs to free RAM, or pick a smaller "
        "model.\n\n"
        "A pull that fails can be tried again: it resumes from where "
        "it stopped.\n\n"
        "If the program itself misbehaves, use File menu, Debug "
        "report. It writes a report file with the log and the PC "
        "details, with any secrets removed. Send that file when "
        "asking for help."
    ),
}


def get_settings():
    """App settings. Tests point FYRN_SETTINGS_FILE at a temp
    file so they never touch the real settings."""
    override = os.environ.get("FYRN_SETTINGS_FILE")
    if override:
        return QSettings(override, QSettings.Format.IniFormat)
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


def get_models_folder() -> Path:
    """The dedicated models folder from settings (default ~/Models)."""
    settings = get_settings()
    value = settings.value("models_folder", "")
    if value:
        return Path(value)
    return core.default_models_folder()


def set_models_folder(folder) -> None:
    settings = get_settings()
    settings.setValue("models_folder", str(folder))
    settings.sync()


def save_window_geometry(settings, prefix, widget):
    """Save a window's geometry under settings prefix/x, y, width,
    height (U5: windows remember their size and position)."""
    rect = widget.geometry()
    settings.setValue("%s/x" % prefix, rect.x())
    settings.setValue("%s/y" % prefix, rect.y())
    settings.setValue("%s/width" % prefix, rect.width())
    settings.setValue("%s/height" % prefix, rect.height())
    settings.sync()


def restore_window_geometry(settings, prefix, widget):
    """Restore a window's geometry saved by save_window_geometry.
    Does nothing when nothing is saved, or when the saved rect is
    off every screen (a monitor setup that changed since).
    Returns True when a geometry was restored: a caller that
    sizes from content needs to know whether the user's own size
    is already in place."""
    values = [settings.value("%s/%s" % (prefix, key), type=int)
              for key in ("x", "y", "width", "height")]
    if any(value is None for value in values):
        return False
    rect = QRect(*values)
    screens = QGuiApplication.screens()
    if screens and not any(
            screen.availableGeometry().intersects(rect)
            for screen in screens):
        return False
    widget.setGeometry(rect)
    return True


# ---------------------------------------------------------------- workers


class LoadWorker(QThread):
    """Load a model off the UI thread (U30)."""

    # percent, message; percent is -1 when the backend has no
    # percent to give (indeterminate bar, honest stage text).
    progress = Signal(int, str)
    finished = Signal(dict)      # {"load_time_s": float, "warning": str}
    failed = Signal(str)         # plain-words message

    def __init__(self, controller, path, display_name=None):
        super().__init__()
        self.controller = controller
        self.path = path
        self.display_name = display_name
        self.cancel_event = threading.Event()

    def run(self):
        try:
            result = self.controller.load(
                self.path,
                progress_cb=lambda pct, msg: self.progress.emit(pct, msg),
                cancel_event=self.cancel_event,
                display_name=self.display_name)
        except LoadCancelled:
            self.failed.emit("Load cancelled.")
        except (ModelTooBigError, ModelNotFoundError) as exc:
            self.failed.emit(str(exc))
        except RunnerError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # never crash the thread silently
            self.failed.emit("The model could not be loaded (%s)." % exc)
        else:
            self.finished.emit(result)

    def cancel(self):
        self.cancel_event.set()


class ChatWorker(QThread):
    """Stream one reply off the UI thread (U30).

    With tools on offer (v0.8), the worker runs the runner's tool
    loop instead: tool calls and results come back through
    tool_event, and a run_command call blocks in
    request_command_approval until the UI thread answers through
    resolve_approval — the approval modal lives on the UI thread,
    and the command never runs without a yes."""

    token = Signal(str)
    finished = Signal(dict)  # generation stats
    failed = Signal(str)
    tool_event = Signal(str, dict)         # "call" / "result"
    web_event = Signal(str, dict)          # browser view updates
    tools_fallback = Signal()              # tool path failed; plain reply used
    approval_requested = Signal(str, str)  # command, working dir

    def __init__(self, runner, prompt, history=None, images=None,
                 instructions=None, executor=None):
        super().__init__()
        self.runner = runner
        self.prompt = prompt
        self.history = list(history or [])
        self.images = list(images or [])
        self.instructions = instructions
        self.executor = executor
        self.cancel_event = threading.Event()
        self._approval_event = threading.Event()
        self._approval_answer = False

    def run(self):
        emit_token = lambda piece: self.token.emit(piece)
        try:
            if self.executor is not None:
                try:
                    stats = self.runner.generate_tools(
                        self.prompt,
                        history=self.history,
                        instructions=self.instructions,
                        images=self.images,
                        execute_tool=self._execute_tool,
                        on_token=emit_token,
                        on_event=lambda kind, data:
                            self.tool_event.emit(kind, data),
                        cancel_event=self.cancel_event)
                except ToolsUnsupportedError:
                    # The model's template cannot really do tools
                    # after all: this turn still gets its reply,
                    # the plain way, and the window says so once.
                    self.tools_fallback.emit()
                    stats = self.runner.generate(
                        self.prompt,
                        on_token=emit_token,
                        cancel_event=self.cancel_event,
                        history=self.history,
                        images=self.images,
                        instructions=self.instructions)
            else:
                stats = self.runner.generate(
                    self.prompt,
                    on_token=emit_token,
                    cancel_event=self.cancel_event,
                    history=self.history,
                    images=self.images,
                    instructions=self.instructions)
        except GenerationCancelled:
            self.finished.emit({"stopped": True})
        except RunnerError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit("The reply stopped with an error (%s)."
                             % exc)
        else:
            stats["stopped"] = False
            self.finished.emit(stats)

    def _execute_tool(self, name, arguments):
        """Run one tool call for the runner's loop. The executor's
        approval callback is swapped for this worker's bridge, so
        a run_command call asks the UI thread and waits; its web
        events are swapped for this worker's signal, so the
        browser view updates on the UI thread too."""
        executor = self.executor
        previous_approve = executor._approve_command
        previous_event = executor._on_event
        executor._approve_command = self.request_command_approval
        executor._on_event = lambda kind, data: self.web_event.emit(
            kind, data)
        try:
            return executor.execute(name, arguments)
        finally:
            executor._approve_command = previous_approve
            executor._on_event = previous_event

    def request_command_approval(self, command, cwd):
        """Called on the worker thread by the executor: ask the UI
        thread and wait for the answer. A stop reads as a no."""
        self._approval_answer = False
        self._approval_event.clear()
        self.approval_requested.emit(command, cwd)
        while not self._approval_event.wait(0.05):
            if self.cancel_event.is_set():
                return False
        return self._approval_answer

    def resolve_approval(self, approved):
        self._approval_answer = bool(approved)
        self._approval_event.set()

    def cancel(self):
        self.cancel_event.set()
        # A worker waiting on an approval wakes up and reads the
        # cancel as a no.
        self._approval_event.set()


class PullWorker(QThread):
    """Download one model off the UI thread (U30)."""

    progress = Signal(int, int, float)  # downloaded, total (-1 unknown), speed
    finished = Signal(str)              # saved path
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, url, dest):
        super().__init__()
        self.url = url
        self.dest = dest
        self.cancel_event = threading.Event()

    def run(self):
        # The pull module is imported here, not at module level:
        # it costs ~0.4 s of cold start, and nothing needs it
        # until a pull actually runs.
        from .pull import (DownloadError, PullCancelled,
                           download_file, fetch_hf_sha256,
                           parse_hf_url)
        try:
            # A Hugging Face pull is checked against the checksum
            # the site publishes, so a corrupt download never
            # becomes a model. No hash means no check, never a
            # failed pull.
            expected = None
            parsed = parse_hf_url(self.url)
            if parsed is not None:
                expected = fetch_hf_sha256(*parsed)
            download_file(
                self.url, self.dest,
                progress_cb=lambda d, t, s: self.progress.emit(
                    d, -1 if t is None else t, s),
                cancel_event=self.cancel_event,
                expected_sha256=expected)
        except PullCancelled:
            self.cancelled.emit()
        except DownloadError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit("The pull failed (%s)." % exc)
        else:
            self.finished.emit(str(self.dest))

    def cancel(self):
        self.cancel_event.set()


class ImportWorker(QThread):
    """Copy a .gguf file into the models folder off the UI thread
    (U30): a 4 GB copy takes ~40 s, and the window must stay
    alive, with progress and a cancel, while it runs."""

    progress = Signal(int, int)  # copied, total
    finished = Signal(str)       # saved path
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, source, folder):
        super().__init__()
        self.source = source
        self.folder = folder
        self.cancel_event = threading.Event()

    def run(self):
        try:
            dest = core.import_gguf(
                self.source, self.folder,
                progress_cb=lambda c, t: self.progress.emit(c, t),
                cancel_event=self.cancel_event)
        except ImportCancelled:
            self.cancelled.emit()
        except ValueError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit("The import failed (%s)." % exc)
        else:
            self.finished.emit(str(dest))

    def cancel(self):
        self.cancel_event.set()


class UpdateCheckWorker(QThread):
    """Check GitHub for a newer Fyrn release off the UI thread
    (v0.6): the check never blocks startup, and a failed check is
    silent, never a crash or a popup."""

    finished = Signal(dict)  # the check_for_update result

    def __init__(self, current_version):
        super().__init__()
        self.current_version = current_version

    def run(self):
        # The updates module is imported here, not at module
        # level: it costs nothing to import, but keeping the
        # worker self-contained matches the other workers.
        from .updates import check_for_update
        self.finished.emit(check_for_update(self.current_version))


class UpdateDownloadWorker(QThread):
    """Download a release zip and its checksum sidecar off the UI
    thread, verify the SHA-256, and stage the update (v0.6).

    The zip downloads next to the staging dir, not in it: the
    staging step clears the dir first, so a zip already inside
    it would be wiped. A failed verification, a missing sidecar,
    or a cancel all leave the old version exactly as it was.
    """

    progress = Signal(int, int)  # downloaded, total (-1 unknown)
    finished = Signal(dict)      # {"version": ...} on success
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, release, home):
        super().__init__()
        self.release = release
        self.home = Path(home)
        self.cancel_event = threading.Event()

    def run(self):
        from .pull import (DownloadError, PullCancelled,
                           download_file)
        from .updates import (UpdateError, fetch_sidecar,
                              parse_sidecar, stage_update)
        try:
            sha256_url = self.release.get("sha256_url")
            if not sha256_url:
                self.failed.emit(
                    "This release has no checksum to check "
                    "against, so Fyrn will not install it.")
                return
            sidecar_text = fetch_sidecar(sha256_url)
            expected = parse_sidecar(
                sidecar_text, self.release.get("zip_name"))
            if expected is None:
                self.failed.emit(
                    "The update's checksum could not be read, so "
                    "the update was not installed.")
                return
            updates_dir = self.home / "updates"
            updates_dir.mkdir(parents=True, exist_ok=True)
            dest = updates_dir / "download.zip"
            # A stale download from an interrupted attempt never
            # resumes into this one: the checksum would catch a
            # bad resume, but a fresh start is cleaner.
            for suffix in ("", ".part", ".part.url", ".part.etag"):
                stale = Path(str(dest) + suffix)
                if stale.exists():
                    stale.unlink()
            download_file(
                self.release["zip_url"], dest,
                progress_cb=lambda d, t, s: self.progress.emit(
                    d, -1 if t is None else t),
                cancel_event=self.cancel_event,
                expected_sha256=expected)
            stage_update(self.home, dest, sidecar_text,
                         self.release["version"], expected)
        except PullCancelled:
            self.cancelled.emit()
        except DownloadError as exc:
            # download_file's messages are written for model
            # pulls ("not saved as a model. Try pulling it
            # again"): a checksum failure during an update says
            # so in update words instead.
            if "checksum differs" in str(exc):
                self.failed.emit(
                    "The update's download did not match its "
                    "checksum, so it was not installed. The old "
                    "version keeps running.")
            else:
                self.failed.emit(str(exc))
        except UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit("The update failed (%s)." % exc)
        else:
            self.finished.emit({"version": self.release["version"]})

    def cancel(self):
        self.cancel_event.set()


class ScanWorker(QThread):
    """Scan the models folder off the UI thread (U30).

    The disk scan is quick, but it runs on a spinning hard drive,
    so it never blocks the window. While it runs, the Models tab
    shows skeleton tiles (honest loading). Ollama's already-
    downloaded models are listed too, unless the setting says
    otherwise: they are already on the disk, so finding them
    costs nothing and saves a pull.
    """

    finished = Signal(list, list, object)  # models, skipped, free_bytes

    def __init__(self, folder, include_ollama=True):
        super().__init__()
        self.folder = Path(folder)
        self.include_ollama = include_ollama

    def run(self):
        models, skipped = scan_models(self.folder)
        if self.include_ollama:
            discovered, warnings = discover_ollama_models()
            seen = {str(m["path"]) for m in models}
            for model in discovered:
                if str(model["path"]) not in seen:
                    models.append(model)
                    seen.add(str(model["path"]))
            skipped.extend(warnings)
        models.sort(key=lambda m: m["name"].lower())
        _total, free_bytes = read_ram_bytes()
        self.finished.emit(models, skipped, free_bytes)


# ------------------------------------------------------------ single instance


class SingleInstance:
    """One GUI window at a time (U27).

    A QLockFile in the home folder says who owns the window; a
    QLocalServer lets a second launch ask the running copy to come
    forward instead of starting a second window. A stale lock from
    a crashed run is taken over.
    """

    def __init__(self, home=None):
        self.home = home or ensure_home()
        self.lock_path = self.home / "app.lock"
        self.lock = QLockFile(str(self.lock_path))
        self.lock.setStaleLockTime(1000)
        digest = hashlib.md5(str(self.home).encode("utf-8")).hexdigest()
        self.server_name = "fyrn-%s" % digest[:12]
        self.server = None
        self._sockets = []
        self._on_raise = None

    def try_take(self) -> bool:
        """True if this process now owns the single-instance lock."""
        if self.lock.tryLock(0):
            return True
        if self.lock.removeStaleLockFile():
            return self.lock.tryLock(0)
        return False

    def release(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None
        self.lock.unlock()

    def start_server(self, on_raise) -> bool:
        """Listen for raise requests from later launches."""
        # QtNetwork is imported here, not at module level: it is
        # only needed once the window exists, and importing it
        # with the module costs cold-start time for every launch.
        from PySide6.QtNetwork import QLocalServer
        self._on_raise = on_raise
        # A crashed run leaves its socket behind; remove it first or
        # listen fails with "Address in use".
        QLocalServer.removeServer(self.server_name)
        self.server = QLocalServer()
        self.server.newConnection.connect(self._accept_connection)
        return self.server.listen(self.server_name)

    def _accept_connection(self):
        if self.server is None:
            return
        while True:
            sock = self.server.nextPendingConnection()
            if sock is None:
                return
            self._sockets.append(sock)
            sock.readyRead.connect(
                lambda s=sock: self._read_message(s))  # noqa: E731
            sock.disconnected.connect(
                lambda s=sock: self._drop_socket(s))  # noqa: E731
            self._read_message(sock)  # the message may already be waiting

    def _read_message(self, sock):
        data = bytes(sock.readAll())
        if b"raise" in data and self._on_raise is not None:
            self._on_raise()

    def _drop_socket(self, sock):
        if sock in self._sockets:
            self._sockets.remove(sock)
        sock.deleteLater()

    def notify_running(self) -> bool:
        """Ask the running copy to raise its window.

        True if it answered. Retries for a few seconds: a first
        instance that has taken the lock but not yet started its
        server answers once it is up.
        """
        from PySide6.QtNetwork import QLocalSocket
        for _ in range(10):
            sock = QLocalSocket()
            sock.connectToServer(self.server_name)
            if sock.waitForConnected(200):
                sock.write(b"raise")
                sock.flush()
                sock.waitForBytesWritten(200)
                sock.disconnectFromServer()
                return True
            time.sleep(0.2)
        return False


class RaiseListener(QThread):
    """Listens on ~/.fyrn/raise.sock for the pre-Qt fast path
    (fyrn/__main__.py): a second launch sends b"raise" here,
    and this thread emits raise_requested, which the main
    window connects to bring_to_front. A QThread, so the signal
    crosses to the GUI thread safely. Only the first instance
    runs one: it holds the fcntl lock, so a stale socket file
    from a crashed run is safe to remove here."""

    raise_requested = Signal()

    def __init__(self, sock_path, parent=None):
        super().__init__(parent)
        self._sock_path = Path(sock_path)
        self._server = None

    def run(self):
        import socket
        try:
            self._sock_path.unlink()
        except OSError:
            pass  # no stale socket, or it went away on its own
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        try:
            server.bind(str(self._sock_path))
        except OSError:
            return  # another listener won the race; nothing to do
        server.listen(1)
        server.settimeout(0.5)
        while not self.isInterruptionRequested():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                if conn.recv(16):
                    self.raise_requested.emit()
            except OSError:
                pass
            finally:
                conn.close()
        try:
            server.close()
            self._sock_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- windows

# The suggested first model for a new user: a real repo and file,
# checked against Hugging Face on 2026-11-13 (Q4_K_M, 0.40 GB, so
# it fits this PC easily). Never invent a suggestion: if this one
# ever goes away, the button must go away with it, not guess a
# replacement.
FIRST_PULL_SUGGESTION = (
    "bartowski/Qwen2.5-0.5B-Instruct-GGUF "
    "Qwen2.5-0.5B-Instruct-Q4_K_M.gguf")


class PressDipButton(QPushButton):
    """A button that dips 3% when pressed (entry 001): scale to
    0.97 over 150 ms, split 75 ms down and 75 ms back, ease-out.
    One widget, one shot: the animation moves geometry only and
    never re-activates the layout (the size hint does not change),
    so nothing reflows. Skipped under reduced motion. Used for
    Send, Load, and Pull only; every other button stays still.
    """

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self._dip_anim = None
        self._dip_rest = None

    def mousePressEvent(self, event):  # noqa: N802 - Qt naming
        super().mousePressEvent(event)
        if reduced_motion() or not self.isEnabled():
            return
        # Reuse the stored rest when a return is still in flight:
        # the current geometry mid-return is not the rest.
        if self._dip_rest is None:
            self._dip_rest = QRect(self.geometry())
        rest = self._dip_rest
        dx = max(1, rest.width() * 3 // 200)
        dy = max(1, rest.height() * 3 // 200)
        shrunk = QRect(rest).adjusted(dx, dy, -dx, -dy)
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(75)
        anim.setStartValue(self.geometry())
        anim.setEndValue(shrunk)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._dip_anim = anim
        anim.start()

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt naming
        super().mouseReleaseEvent(event)
        self._end_dip()

    def _end_dip(self):
        if self._dip_rest is None:
            return
        rest = self._dip_rest
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(75)
        anim.setStartValue(self.geometry())
        anim.setEndValue(rest)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(
            lambda a=anim, r=rest: self._on_dip_finished(a, r))
        self._dip_anim = anim
        anim.start()

    def _on_dip_finished(self, anim, rest):
        # Only the current animation may clear the rest: a stale
        # return finishing after a new press must not un-rest the
        # button.
        if self._dip_anim is anim and self._dip_rest == rest:
            self._dip_rest = None


class PullWindow(QDialog):
    """Pull a model: its own window (U1).

    One input field, explained in plain words underneath (U20).
    Progress, MB downloaded, speed, and time left while it runs
    (U26), and the pull resumes after a failure or a cancel (U24).
    Several models can be queued and pulled in order, one after
    the other, with no further clicks.
    """

    model_pulled = Signal(str)  # saved path

    def __init__(self, models_folder, log=None, parent=None,
                 is_loaded=None):
        super().__init__(parent)
        self.models_folder = Path(models_folder)
        self.log = log
        # is_loaded(path) says whether a model file is loaded right
        # now: a loaded model cannot be re-pulled over (the engine
        # is reading it). The main window passes its controller's
        # answer; None means "nothing is loaded".
        self._is_loaded = is_loaded
        self.worker = None
        self._celebrate_anim = None
        # The pull queue: [{"target": DownloadTarget,
        # "status": "waiting"|"downloading"|"done"|"failed"|
        # "stopped"}]. One worker pulls them in order.
        self.queue = []
        self._queue_running = False
        self._queue_item = None
        # Smoothed ETA, reset for each download: the raw figure
        # jumps with every chunk, so the displayed time is an
        # average of recent ones.
        self._eta_smoothed = None
        # True while the status line shows a validation error from
        # the field losing focus, so a good input restores the
        # idle line instead of clobbering pull news.
        self._validation_error = False
        self.setWindowTitle("Pull a model")
        self.setMinimumSize(560, 300)
        self.resize(640, 340)
        restore_window_geometry(get_settings(), "pull_window", self)

        layout = QVBoxLayout(self)

        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "myname/mymodel mymodel.gguf  or  https://.../model.gguf")
        self.input.returnPressed.connect(self._start_pull)
        # Check the moment the field loses focus: a typo caught
        # here saves a long pull that starts and fails.
        self.input.editingFinished.connect(self._validate_input)
        layout.addWidget(self.input)

        help_label = QLabel(
            "Models come from Hugging Face, a website where models "
            "are listed. Type a model and its file name, like "
            "myname/mymodel mymodel.gguf, or paste a direct link to "
            "a .gguf file. It saves into your models folder.")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

        # A new user with no models and no Hugging Face knowledge
        # needs a real first model to type, not just the shape of
        # one: one tap fills the field with the suggestion.
        suggest_label = QLabel(
            "First time here? Try this small model first. It fits "
            "this PC easily (about 400 MB).")
        suggest_label.setWordWrap(True)
        layout.addWidget(suggest_label)

        suggest_row = QHBoxLayout()
        self.suggest_button = QPushButton("Try Qwen2.5 0.5B first")
        self.suggest_button.setToolTip(
            "Fill in a small model that fits this PC easily")
        self.suggest_button.clicked.connect(self._fill_suggestion)
        suggest_row.addWidget(self.suggest_button)
        suggest_row.addStretch(1)
        layout.addLayout(suggest_row)

        buttons = QHBoxLayout()
        self.pull_button = PressDipButton("Pull")
        self.pull_button.setToolTip(
            "Download the model in the field")
        self.pull_button.clicked.connect(self._start_pull)
        self.queue_add_button = QPushButton("Add to queue")
        self.queue_add_button.setToolTip(
            "Add the model in the field to the queue, to pull "
            "later")
        self.queue_add_button.clicked.connect(self._add_to_queue)
        self.queue_pull_button = QPushButton("Pull queue")
        self.queue_pull_button.setToolTip(
            "Download every model in the queue, in order")
        self.queue_pull_button.clicked.connect(self._start_queue)
        self.queue_pull_button.setEnabled(False)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setToolTip(
            "Stop this download (it can be resumed later)")
        self.cancel_button.clicked.connect(self._cancel_pull)
        self.cancel_button.setEnabled(False)
        self.close_button = QPushButton("Close")
        self.close_button.setToolTip(
            "Close this window (a running download stops, keeping "
            "its part file)")
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.pull_button)
        buttons.addWidget(self.queue_add_button)
        buttons.addWidget(self.queue_pull_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        # The progress row: bar, busy dot, and the numbers in one
        # tidy block (percent, MB, speed, ETA). The bar itself
        # carries no text: the numbers live in the stats line,
        # where they are always readable whatever the bar's fill
        # is doing underneath them.
        progress_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        progress_row.addWidget(self.progress_bar, 1)
        self.busy_dot = QLabel("\u25cf")
        self.busy_dot.setObjectName("busy_dot")
        self.busy_dot.setStyleSheet(
            "color: %s;" % current_tokens()["accent"])
        self.busy_dot.setVisible(False)
        progress_row.addWidget(self.busy_dot)
        layout.addLayout(progress_row)

        self.progress_stats = QLabel("")
        self.progress_stats.setObjectName("progress_stats")
        self.progress_stats.setWordWrap(True)
        self.progress_stats.setVisible(False)
        layout.addWidget(self.progress_stats)

        self.status_label = QLabel("Nothing pulling yet.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # The pull queue: models waiting their turn, each with its
        # state (Waiting, Downloading, Done, Failed). Hidden when
        # the queue is empty, so the window stays tight.
        self.queue_label = QLabel("Queue")
        self.queue_label.setWordWrap(True)
        layout.addWidget(self.queue_label)
        self.queue_list = QListWidget()
        self.queue_list.setObjectName("queue_list")
        self.queue_list.setMaximumHeight(120)
        self.queue_list.itemSelectionChanged.connect(
            self._update_queue_buttons)
        layout.addWidget(self.queue_list)
        queue_buttons = QHBoxLayout()
        self.queue_remove_button = QPushButton("Remove from queue")
        self.queue_remove_button.setToolTip(
            "Take the selected model out of the queue")
        self.queue_remove_button.clicked.connect(self._remove_queued)
        queue_buttons.addWidget(self.queue_remove_button)
        queue_buttons.addStretch(1)
        layout.addLayout(queue_buttons)
        self._refresh_queue_list()

        # Unfinished downloads: a .part file in the models folder
        # is a pull that stopped halfway. It is listed here with
        # its size, so it can be resumed or cleared away instead
        # of sitting on the disk unseen. Hidden when there are
        # none, so the window stays tight.
        self.parts_label = QLabel("Unfinished downloads")
        self.parts_label.setWordWrap(True)
        layout.addWidget(self.parts_label)
        self.parts_list = QListWidget()
        self.parts_list.setObjectName("part_list")
        self.parts_list.setMaximumHeight(120)
        self.parts_list.itemSelectionChanged.connect(
            self._update_part_buttons)
        layout.addWidget(self.parts_list)
        parts_buttons = QHBoxLayout()
        self.part_resume_button = QPushButton("Resume")
        self.part_resume_button.setToolTip(
            "Pick up this unfinished download where it stopped")
        self.part_resume_button.clicked.connect(self._resume_part)
        self.part_delete_button = QPushButton("Delete")
        self.part_delete_button.setToolTip(
            "Throw this unfinished download away")
        self.part_delete_button.clicked.connect(self._delete_part)
        parts_buttons.addWidget(self.part_resume_button)
        parts_buttons.addWidget(self.part_delete_button)
        parts_buttons.addStretch(1)
        layout.addLayout(parts_buttons)
        self._close_pending = False
        self._refresh_part_list()

    def _fill_suggestion(self):
        """Fill the field with the suggested first model."""
        self.input.setText(FIRST_PULL_SUGGESTION)
        self.input.setFocus()

    def _fit_to_content(self):
        """Grow the window to its content (task 4): when the
        queue or unfinished list appears, the window grows so
        every control stays readable, instead of crushing to
        slivers at its first size. Bounded by the screen, so a
        long queue never pushes the window off-screen; the lists
        cap at 120 px each, so the window stays a sane size.
        Grow-only: the window never shrinks under the user when
        a list empties. Before the window is shown there is
        nothing to fit, so construction-time calls are no-ops
        and never fight the restored geometry."""
        if not self.isVisible():
            return
        hint = self.sizeHint()
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(max(hint.width(), self.minimumWidth()),
                        available.width())
            height = min(max(hint.height(), self.minimumHeight()),
                         available.height())
        else:
            width, height = hint.width(), hint.height()
        self.resize(max(self.width(), width),
                    max(self.height(), height))

    def apply_theme(self, tokens):
        """Re-skin the pull window's busy dot for a theme change
        (the rest of the window follows the app stylesheet)."""
        self.busy_dot.setStyleSheet("color: %s;" % tokens["accent"])

    def _set_busy(self, busy):
        self.busy_dot.setVisible(busy)
        self.pull_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.input.setEnabled(not busy)
        self._update_queue_buttons()

    def _validate_input(self):
        """Check the field the moment it loses focus: a bad model
        name or URL is an inline error right away, not a surprise
        when Pull is pressed. Never runs during a download (the
        status line belongs to the pull then)."""
        if self.worker is not None:
            return
        from .pull import parse_model_input
        text = self.input.text().strip()
        if not text:
            if self._validation_error:
                self._validation_error = False
                self.status_label.setText("Nothing pulling yet.")
            return
        try:
            parse_model_input(text)
        except ValueError as exc:
            self._validation_error = True
            self.status_label.setText(str(exc))
            return
        if self._validation_error:
            self._validation_error = False
            self.status_label.setText("Nothing pulling yet.")

    def _confirm_replace(self, dest):
        """True when an existing model may be pulled over: a
        loaded model is refused outright (the engine is reading
        it), and anything else asks first, default No, the same
        confirm habit as Remove."""
        if (self._is_loaded is not None
                and self._is_loaded(dest)):
            self.status_label.setText(
                "%s is loaded right now. Unload it first, then "
                "pull it again." % dest.name)
            return False
        answer = QMessageBox.question(
            self, "Pull a model",
            "%s is already in your models folder.\n\n"
            "Pull it again and replace it? The old file is kept "
            "until the new download finishes."
            % dest.name,
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _start_pull(self):
        if self.worker is not None:
            return
        from .pull import parse_model_input
        try:
            target = parse_model_input(self.input.text())
        except ValueError as exc:
            # One pattern for pull errors (L3): the user is looking
            # at the field, so a bad input is a quiet inline error
            # in the status line, not a modal. The field keeps
            # focus so the fix is one edit away.
            self.status_label.setText(str(exc))
            self.input.setFocus()
            return
        dest = ensure_models_folder(self.models_folder) / target.filename
        if dest.exists() and not self._confirm_replace(dest):
            return
        self._start_download(target.url, dest)

    def _start_download(self, url, dest):
        """Run a download in the worker: the shared path for a
        fresh pull and for resuming an unfinished one."""
        if self.log is not None:
            self.log.action("pull started: %s" % Path(dest).name)
        self._eta_smoothed = None
        self._set_busy(True)
        self.progress_bar.setValue(0)
        self.status_label.setText("Starting download...")
        self.worker = PullWorker(url, dest)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(self._on_cancelled)
        self.worker.start()

    # ------------------------------------------------------------ queue

    def _add_to_queue(self):
        """Queue the model in the field, to pull in order later.
        The field clears so the next one can be typed straight
        away. A model already in the folder asks about replacing
        it, the same as Pull does."""
        from .pull import parse_model_input
        try:
            target = parse_model_input(self.input.text())
        except ValueError as exc:
            self.status_label.setText(str(exc))
            self.input.setFocus()
            return
        dest = ensure_models_folder(self.models_folder) / target.filename
        if dest.exists() and not self._confirm_replace(dest):
            return
        if any(item["target"].filename == target.filename
               and item["status"] in ("waiting", "downloading")
               for item in self.queue):
            self.status_label.setText(
                "%s is already in the queue." % target.filename)
            return
        self.queue.append({"target": target, "status": "waiting"})
        self.input.clear()
        self._refresh_queue_list()
        self._update_queue_buttons()
        self.status_label.setText(
            "%s added to the queue." % target.filename)

    def _start_queue(self):
        """Pull every waiting model in the queue, in order, with
        no further clicks."""
        if self.worker is not None or self._queue_running:
            return
        if not any(item["status"] == "waiting"
                   for item in self.queue):
            return
        self._queue_running = True
        self._start_next_queued()

    def _start_next_queued(self):
        """Start the next waiting item, or finish the queue."""
        waiting = [item for item in self.queue
                   if item["status"] == "waiting"]
        if not waiting:
            self._end_queue()
            return
        item = waiting[0]
        item["status"] = "downloading"
        self._queue_item = item
        self._refresh_queue_list()
        dest = (ensure_models_folder(self.models_folder)
                / item["target"].filename)
        self._start_download(item["target"].url, dest)

    def _end_queue(self):
        """The queue is done: every waiting item pulled."""
        self._queue_running = False
        self._queue_item = None
        done = sum(1 for item in self.queue
                   if item["status"] == "done")
        self._set_busy(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        if done == 1:
            self.status_label.setText(
                "Queue done. 1 model pulled.")
        else:
            self.status_label.setText(
                "Queue done. %d models pulled." % done)
        self._celebrate()
        self._play_chime()
        self._refresh_part_list()
        self._maybe_close_pending()

    def _queue_position(self):
        """(index, total) of the item now downloading, for the
        status line: "2 of 3"."""
        total = len(self.queue)
        if self._queue_item is None:
            return None
        try:
            index = self.queue.index(self._queue_item) + 1
        except ValueError:
            return None
        return index, total

    def _refresh_queue_list(self):
        """Rebuild the queue list from the queue state. Hidden
        when the queue is empty."""
        show = bool(self.queue)
        self.queue_label.setVisible(show)
        self.queue_list.setVisible(show)
        self.queue_remove_button.setVisible(show)
        self.queue_list.clear()
        if not show:
            return
        for item in self.queue:
            text = "%s - %s" % (item["target"].filename,
                                item["status"].capitalize())
            list_item = QListWidgetItem(text)
            list_item.setData(Qt.ItemDataRole.UserRole,
                              item["target"].filename)
            self.queue_list.addItem(list_item)
        self._update_queue_buttons()
        self._fit_to_content()

    def _update_queue_item(self, item):
        """Refresh one queue item's line in place (its progress
        changes every chunk; rebuilding the whole list would drop
        the selection each time)."""
        for row in range(self.queue_list.count()):
            list_item = self.queue_list.item(row)
            if (list_item.data(Qt.ItemDataRole.UserRole)
                    == item["target"].filename):
                list_item.setText(
                    "%s - %s" % (item["target"].filename,
                                 item["status"].capitalize()))
                return

    def _update_queue_buttons(self):
        waiting = any(item["status"] == "waiting"
                      for item in self.queue)
        self.queue_pull_button.setEnabled(
            waiting and self.worker is None
            and not self._queue_running)
        # Remove works on a selected item that is not the one now
        # downloading.
        selected_item = self._selected_queue_item()
        self.queue_remove_button.setEnabled(
            selected_item is not None
            and selected_item is not self._queue_item)

    def _selected_queue_item(self):
        items = self.queue_list.selectedItems()
        if not items:
            return None
        filename = items[0].data(Qt.ItemDataRole.UserRole)
        for item in self.queue:
            if item["target"].filename == filename:
                return item
        return None

    def _remove_queued(self):
        """Drop the selected item from the queue (never the one
        now downloading)."""
        item = self._selected_queue_item()
        if item is None or item is self._queue_item:
            return
        self.queue.remove(item)
        self._refresh_queue_list()
        self._update_queue_buttons()

    def _refresh_part_list(self):
        """List the unfinished downloads, or hide the section
        when there are none."""
        parts = list_part_files(self.models_folder)
        show = bool(parts)
        self.parts_label.setVisible(show)
        self.parts_list.setVisible(show)
        self.part_resume_button.setVisible(show)
        self.part_delete_button.setVisible(show)
        self.parts_list.clear()
        if not show:
            return
        for part in parts:
            item = QListWidgetItem(
                "%s (%s downloaded)"
                % (part["name"], format_size(part["size"])))
            item.setData(Qt.ItemDataRole.UserRole, str(part["path"]))
            self.parts_list.addItem(item)
        self._update_part_buttons()
        self._fit_to_content()

    def _update_part_buttons(self):
        selected = bool(self.parts_list.selectedItems())
        self.part_resume_button.setEnabled(
            selected and self.worker is None)
        self.part_delete_button.setEnabled(selected)

    def _selected_part_path(self):
        items = self.parts_list.selectedItems()
        if not items:
            return None
        return Path(items[0].data(Qt.ItemDataRole.UserRole))

    def _resume_part(self):
        """Pick up an unfinished download from its saved address."""
        if self.worker is not None:
            return
        part_path = self._selected_part_path()
        if part_path is None:
            return
        from .pull import read_part_url
        url = read_part_url(part_path)
        if url is None:
            self.status_label.setText(
                "That download cannot be resumed: its address was "
                "not saved. Delete it and pull the model again.")
            return
        dest = part_path.with_name(
            part_path.name[:-len(".part")])
        if dest.exists():
            self.status_label.setText(
                "%s is already in your models folder." % dest.name)
            return
        self.input.setText(url)
        self._start_download(url, dest)
        self._update_part_buttons()

    def _delete_part(self):
        """Clear an unfinished download away, with its sidecars."""
        part_path = self._selected_part_path()
        if part_path is None:
            return
        name = part_path.name
        try:
            size = part_path.stat().st_size
        except OSError:
            size = 0
        for path in (part_path,
                     Path(str(part_path) + ".url"),
                     Path(str(part_path) + ".etag")):
            try:
                path.unlink()
            except OSError:
                pass
        if self.log is not None:
            self.log.action("deleted unfinished download: %s" % name)
        self.status_label.setText(
            "Deleted %s (freed %s)." % (name, format_size(size)))
        self._refresh_part_list()

    def _on_progress(self, downloaded, total, speed):
        # Where this pull sits in the queue: "2 of 3: ...". The
        # status line carries the state; the numbers live in the
        # stats line, one tidy block: percent, MB, speed, ETA.
        prefix = ""
        position = self._queue_position()
        if position is not None:
            prefix = "%d of %d: " % position
        self.status_label.setText("%sDownloading..." % prefix)
        self.progress_stats.setVisible(True)
        if total and total > 0:
            self.progress_bar.setRange(0, 100)
            pct = int(downloaded * 100 / total)
            self.progress_bar.setValue(pct)
            # Time left, smoothed: the raw figure jumps with every
            # chunk, so the displayed time is an average of recent
            # ones, and it only shows when the speed is real.
            eta_text = ""
            if speed > 0:
                raw_eta = (total - downloaded) / speed
                if self._eta_smoothed is None:
                    self._eta_smoothed = raw_eta
                else:
                    self._eta_smoothed = (
                        0.7 * self._eta_smoothed + 0.3 * raw_eta)
                from .pull import format_eta
                eta = format_eta(self._eta_smoothed)
                if eta is not None:
                    eta_text = " · %s" % eta
            self.progress_stats.setText(
                "%d%% · %s of %s · %s per second%s"
                % (pct, format_size(downloaded),
                   format_size(total), format_size(int(speed)),
                   eta_text))
            if self._queue_item is not None:
                self._queue_item["status"] = (
                    "downloading (%d%%)" % pct)
                self._update_queue_item(self._queue_item)
                self._queue_item["status"] = "downloading"
        else:
            self.progress_bar.setRange(0, 0)  # unknown total: busy bar
            self.progress_stats.setText(
                "%s downloaded · %s per second"
                % (format_size(downloaded),
                   format_size(int(speed))))

    def _on_finished(self, path):
        self._drop_worker()
        self.progress_stats.setVisible(False)
        self._refresh_part_list()
        if self.log is not None:
            self.log.action("pull finished: %s" % path)
        self.model_pulled.emit(path)
        if self._queue_running:
            # The queue moves on to the next model on its own.
            if self._queue_item is not None:
                self._queue_item["status"] = "done"
                self._update_queue_item(self._queue_item)
            self._start_next_queued()
            return
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.status_label.setText("Done. The model is in your models "
                                  "folder.")
        self._celebrate()
        self._play_chime()
        self._set_busy(False)
        self._maybe_close_pending()

    def _maybe_close_pending(self):
        """Close this window if a close was asked for while a pull
        was running and the pull has now stopped."""
        if self._close_pending and self.worker is None:
            self._close_pending = False
            self.close()

    def _play_chime(self):
        """The pull-complete chime (two soft notes), unless muted
        in Settings. The sound is generated in code and played
        through QtMultimedia; when the machine has no sound
        backend, play_chime returns False and the visual signal
        (the status fade + tray message) is the whole signal —
        never a fake chime."""
        if get_chime_muted():
            return
        from .chime import play_chime
        play_chime()

    def _celebrate(self):
        """A small quiet celebration when a pull finishes: one
        200 ms fade of the status line, opacity only, then the
        effect is removed so the label paints normally afterwards.
        Skipped entirely under reduced motion.
        """
        if reduced_motion():
            return
        effect = QGraphicsOpacityEffect(self.status_label)
        self.status_label.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(200)
        anim.setStartValue(0.3)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(
            lambda: self.status_label.setGraphicsEffect(None))
        self._celebrate_anim = anim
        anim.start()

    def _on_failed(self, message):
        # A download fails while the user may be elsewhere, so this
        # is the one pull error that keeps the modal: status line
        # for the trace, modal for the attention (L3).
        # A failure stops the queue: the models still waiting stay
        # queued, so the user can fix the problem and press Pull
        # queue again.
        if self._queue_item is not None:
            self._queue_item["status"] = "failed"
            self._update_queue_item(self._queue_item)
        self._queue_running = False
        self._queue_item = None
        self.progress_bar.setRange(0, 100)
        self.progress_stats.setVisible(False)
        self.status_label.setText(message)
        self._set_busy(False)
        self._drop_worker()
        self._refresh_part_list()
        if self.log is not None:
            self.log.error("pull failed: %s", message)
        QMessageBox.warning(self, "Pull a model", message)
        self._maybe_close_pending()

    def _on_cancelled(self):
        # A cancel stops the queue too: the partial download is
        # kept, and the models still waiting stay queued.
        if self._queue_item is not None:
            self._queue_item["status"] = "stopped"
            self._update_queue_item(self._queue_item)
        self._queue_running = False
        self._queue_item = None
        self.progress_bar.setRange(0, 100)
        self.progress_stats.setVisible(False)
        self.status_label.setText(
            "Pull stopped. The part already downloaded stays, and "
            "pulling again picks up where it stopped.")
        self._set_busy(False)
        self._drop_worker()
        self._refresh_part_list()
        self._maybe_close_pending()

    def _drop_worker(self):
        """Forget the pull worker, after it has fully stopped.

        The finished/failed/cancelled signal fires while the thread
        is still exiting; dropping the last reference there destroys
        the QThread mid-exit and Qt aborts the process ("Destroyed
        while thread is still running"). wait() first.
        """
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.wait()

    def _cancel_pull(self):
        if self.worker is not None:
            self.worker.cancel()
            self.status_label.setText("Stopping...")

    def hideEvent(self, event):  # noqa: N802 - Qt naming
        # Save on hide as well as close: a dialog hidden by
        # accept()/reject() never gets a close event.
        save_window_geometry(get_settings(), "pull_window", self)
        super().hideEvent(event)

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        save_window_geometry(get_settings(), "pull_window", self)
        if self.worker is not None:
            # Closing mid-pull stops the download: ask first,
            # default No, and say the part already downloaded
            # stays (it does, and the next pull resumes from it).
            # With a queue running, the rest of the queue stops
            # too (the models still waiting stay queued).
            if self._queue_running:
                message = ("Stop this pull and the rest of the "
                           "queue? The part already downloaded "
                           "stays, and pulling again picks up "
                           "where it stopped.")
            else:
                message = ("Stop this pull? The part already "
                           "downloaded stays, and pulling again "
                           "picks up where it stopped.")
            answer = QMessageBox.question(
                self, "Pull a model", message,
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(5000)
            if self.worker is not None and self.worker.isRunning():
                # A network read can block past the wait: closing
                # now would destroy a running thread and abort the
                # process. Stay open and close when it stops.
                self._close_pending = True
                self.status_label.setText(
                    "Stopping the pull. This window will close "
                    "when it stops.")
                event.ignore()
                return
        super().closeEvent(event)


class SettingsWindow(QDialog):
    """Settings: its own resizable pop-up window (U1).

    The models folder, Reduce motion, the Ollama listing, and
    the display, sound, and update settings: compact list,
    high-contrast mode, the pull-complete chime mute, and the
    startup update check. The window is resizable and
    maximizable; it opens at its content size, unless a saved
    size of the user's own exists (that one is honoured
    exactly).
    """

    folder_changed = Signal(str)
    settings_saved = Signal()

    def __init__(self, current_folder, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        # The folder this window opened with: _save only
        # announces a folder change when the folder actually
        # changed (task 10).
        self._initial_folder = str(current_folder)
        restored = restore_window_geometry(
            get_settings(), "settings_window", self)

        layout = QVBoxLayout(self)

        label = QLabel("Models folder")
        layout.addWidget(label)

        row = QHBoxLayout()
        self.folder_edit = QLineEdit(str(current_folder))
        self.folder_edit.setReadOnly(True)
        browse_button = QPushButton("Browse")
        browse_button.setToolTip(
            "Pick a different folder for your models")
        browse_button.clicked.connect(self._browse)
        row.addWidget(self.folder_edit)
        row.addWidget(browse_button)
        layout.addLayout(row)

        help_label = QLabel(
            "Pulled and imported models are kept here. Pick any "
            "folder you like.")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

        self.motion_check = QCheckBox("Reduce motion")
        self.motion_check.setChecked(get_reduce_motion())
        layout.addWidget(self.motion_check)

        motion_help = QLabel(
            "Less movement on screen. Animations become instant "
            "instead.")
        motion_help.setWordWrap(True)
        layout.addWidget(motion_help)

        self.ollama_check = QCheckBox(
            "Show models Ollama already downloaded")
        self.ollama_check.setChecked(get_show_ollama_models())
        layout.addWidget(self.ollama_check)

        ollama_help = QLabel(
            "Fyrn also lists the models Ollama downloaded, so you "
            "do not have to pull them again. Uncheck to list only "
            "your models folder.")
        ollama_help.setWordWrap(True)
        layout.addWidget(ollama_help)

        self.compact_check = QCheckBox("Compact model list")
        self.compact_check.setChecked(get_compact_list())
        layout.addWidget(self.compact_check)

        compact_help = QLabel(
            "Smaller cards, more models on screen at once. The "
            "description and facts stay in each card's tooltip.")
        compact_help.setWordWrap(True)
        layout.addWidget(compact_help)

        self.contrast_check = QCheckBox("High-contrast mode")
        self.contrast_check.setChecked(get_high_contrast())
        layout.addWidget(self.contrast_check)

        contrast_help = QLabel(
            "Black and white with yellow highlights. Every bit of "
            "text is at least 7 to 1 against its background.")
        contrast_help.setWordWrap(True)
        layout.addWidget(contrast_help)

        self.chime_check = QCheckBox(
            "Mute the pull-complete chime")
        self.chime_check.setChecked(get_chime_muted())
        layout.addWidget(self.chime_check)

        chime_help = QLabel(
            "A short chime plays when a pull finishes. Check this "
            "to turn it off.")
        chime_help.setWordWrap(True)
        layout.addWidget(chime_help)

        self.update_check = QCheckBox(
            "Check for updates when Fyrn starts")
        self.update_check.setChecked(get_update_check())
        layout.addWidget(self.update_check)

        update_help = QLabel(
            "Fyrn quietly asks GitHub whether a newer release is "
            "out when it starts, and says so only when one is. "
            "Nothing downloads without asking first.")
        update_help.setWordWrap(True)
        layout.addWidget(update_help)

        tools_label = QLabel("Tools folder")
        layout.addWidget(tools_label)

        tools_row = QHBoxLayout()
        self.tools_folder_edit = QLineEdit(get_tools_folder())
        self.tools_folder_edit.setReadOnly(True)
        tools_browse = QPushButton("Browse")
        tools_browse.setToolTip(
            "Pick the folder the model's file tools may use")
        tools_browse.clicked.connect(self._browse_tools_folder)
        tools_row.addWidget(self.tools_folder_edit)
        tools_row.addWidget(tools_browse)
        layout.addLayout(tools_row)

        tools_help = QLabel(
            "When a model that can use tools is loaded, it can "
            "read and write text files, but only inside this "
            "folder. Nothing outside it can be touched.")
        tools_help.setWordWrap(True)
        layout.addWidget(tools_help)

        forge_label = QLabel("Forge Browser folder (optional)")
        layout.addWidget(forge_label)

        forge_row = QHBoxLayout()
        self.forge_path_edit = QLineEdit(get_forge_browser_path())
        self.forge_path_edit.setPlaceholderText(
            "Found on its own when this is empty")
        forge_browse = QPushButton("Browse")
        forge_browse.setToolTip(
            "Pick the folder Forge Browser is installed in")
        forge_browse.clicked.connect(self._browse_forge_folder)
        forge_row.addWidget(self.forge_path_edit)
        forge_row.addWidget(forge_browse)
        layout.addLayout(forge_row)

        forge_help = QLabel(
            "The model's web tools go through Forge Browser. Fyrn "
            "finds it in its usual install place on its own; set "
            "this only if yours lives somewhere else.")
        forge_help.setWordWrap(True)
        layout.addWidget(forge_help)

        layout.addStretch(1)

        buttons = QHBoxLayout()
        self.save_button = QPushButton("Save")
        self.save_button.setToolTip(
            "Save these settings")
        self.save_button.clicked.connect(self._save)
        cancel_button = QPushButton("Cancel")
        cancel_button.setToolTip(
            "Close without saving")
        cancel_button.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

        # Size the dialog from its content (task 1): the old
        # hardcoded 560x220 crushed it (labels clipped, Save and
        # Cancel 9 px slivers). A saved geometry is honoured
        # exactly instead: it is the user's own size, and the
        # dialog stays freely resizable either way (no minimum
        # pins it to its content). The height is capped to the
        # screen, so a large system text size never makes a
        # window taller than the display.
        #
        # The height comes from totalHeightForWidth at the real
        # width, not from sizeHint: the help labels wrap, and
        # sizeHint measures them at the layout's narrow width
        # (301 px), which overestimates the height by ~140 px at
        # the dialog's real width and leaves a dead band above
        # the buttons (the L10 gap the hugs test guards).
        self.setMinimumSize(480, 180)
        if not restored:
            width = max(self.width(), self.sizeHint().width())
            height = layout.totalHeightForWidth(width)
            if height <= 0:
                height = self.sizeHint().height()
            screen = QApplication.primaryScreen()
            if screen is not None:
                max_height = (screen.availableGeometry().height()
                              - 40)
                height = min(height, max_height)
            self.resize(width, height)

    def hideEvent(self, event):  # noqa: N802 - Qt naming
        # Save on hide: Save and Cancel hide the dialog without a
        # close event, and those are the paths that matter here.
        save_window_geometry(
            get_settings(), "settings_window", self)
        super().hideEvent(event)

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        save_window_geometry(
            get_settings(), "settings_window", self)
        super().closeEvent(event)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Pick a models folder", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)

    def _browse_tools_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Pick the tools folder",
            self.tools_folder_edit.text())
        if folder:
            self.tools_folder_edit.setText(folder)

    def _browse_forge_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Pick the Forge Browser folder",
            self.forge_path_edit.text() or str(Path.home()))
        if folder:
            self.forge_path_edit.setText(folder)

    def _save(self):
        folder = Path(self.folder_edit.text())
        try:
            ensure_models_folder(folder)
        except OSError as exc:
            QMessageBox.warning(
                self, "Settings",
                "That folder could not be used (%s)." % exc)
            return
        set_models_folder(folder)
        set_reduce_motion(self.motion_check.isChecked())
        set_show_ollama_models(self.ollama_check.isChecked())
        set_compact_list(self.compact_check.isChecked())
        set_high_contrast(self.contrast_check.isChecked())
        set_chime_muted(self.chime_check.isChecked())
        set_update_check(self.update_check.isChecked())
        set_tools_folder(self.tools_folder_edit.text())
        set_forge_browser_path(self.forge_path_edit.text().strip())
        # Only a real folder change is announced (task 10): a
        # settings-only save used to claim the folder had moved.
        if str(folder) != self._initial_folder:
            self.folder_changed.emit(str(folder))
        self.settings_saved.emit()
        self.accept()


class HowToUseWindow(QDialog):
    """Built-in How to Use index (U22)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("How to Use")
        self.setMinimumSize(640, 420)
        self.resize(720, 480)
        restore_window_geometry(get_settings(), "howto_window", self)

        layout = QHBoxLayout(self)
        self.section_list = QListWidget()
        # Wide enough for the longest title ("If something goes
        # wrong") so no section name is clipped with a scrollbar.
        self.section_list.setMinimumWidth(220)
        for title in HOW_TO_USE_SECTIONS:
            self.section_list.addItem(QListWidgetItem(title))
        self.section_list.currentTextChanged.connect(self._show_section)
        layout.addWidget(self.section_list, 1)

        self.text_view = QTextEdit()
        self.text_view.setReadOnly(True)
        layout.addWidget(self.text_view, 3)

        self.section_list.setCurrentRow(0)

    def hideEvent(self, event):  # noqa: N802 - Qt naming
        save_window_geometry(get_settings(), "howto_window", self)
        super().hideEvent(event)

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        save_window_geometry(get_settings(), "howto_window", self)
        super().closeEvent(event)

    def _show_section(self, title):
        self.text_view.setPlainText(
            HOW_TO_USE_SECTIONS.get(title, ""))


class CommandApprovalDialog(QDialog):
    """The run_command gate (v0.8): the exact command the model
    wants to run, and where it will run. Run is never the default
    button — Cancel is — and nothing runs unless the user clicks
    Run. The user approves commands here; they never open a
    terminal."""

    def __init__(self, command, cwd, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Run this command?")
        self.setModal(True)
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "The model wants to run this command. It runs only "
            "if you press Run.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.command_view = QPlainTextEdit(command)
        self.command_view.setReadOnly(True)
        self.command_view.setMaximumHeight(110)
        layout.addWidget(self.command_view)
        where = QLabel(f"It will run in this folder: {cwd}")
        where.setWordWrap(True)
        layout.addWidget(where)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.run_button = QPushButton("Run")
        self.run_button.setToolTip("Run this command now")
        self.run_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setToolTip("Do not run it")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()


class BrowserWindow(QDialog):
    """The browser view (v0.8): what the model's web tools are
    doing, live — the current search or page, the text Forge
    Browser brought back, a screenshot when one comes back, and
    a button that opens the real Forge Browser on the same page.
    """

    open_requested = Signal(str)  # URL to open in Forge Browser

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Browser view")
        self.setMinimumSize(560, 420)
        self.resize(680, 520)
        restore_window_geometry(
            get_settings(), "browser_window", self)
        self._current_url = ""
        layout = QVBoxLayout(self)
        self.status_label = QLabel("Nothing has been looked up yet.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.url_label = QLabel("")
        self.url_label.setWordWrap(True)
        layout.addWidget(self.url_label)
        self.screenshot_label = QLabel()
        self.screenshot_label.setVisible(False)
        layout.addWidget(self.screenshot_label)
        self.content_view = QTextEdit()
        self.content_view.setReadOnly(True)
        layout.addWidget(self.content_view, 1)
        buttons = QHBoxLayout()
        self.open_button = QPushButton("Open in Forge Browser")
        self.open_button.setToolTip(
            "Open this page in the real Forge Browser window")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(
            lambda: self.open_requested.emit(self._current_url))
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def show_busy(self, text):
        self.status_label.setText(text)

    def show_search(self, query, text):
        self.status_label.setText(f"Search results for: {query}")
        self._current_url = ""
        self.url_label.setText("")
        self.screenshot_label.setVisible(False)
        self.content_view.setPlainText(text)
        self.open_button.setEnabled(False)

    def show_page(self, url, title, markdown, screenshot=None):
        self.status_label.setText(title or "Page")
        self._current_url = url or ""
        self.url_label.setText(url or "")
        self.content_view.setPlainText(markdown or "")
        shown = False
        if screenshot:
            pixmap = QPixmap(str(screenshot))
            if not pixmap.isNull():
                self.screenshot_label.setPixmap(pixmap.scaledToWidth(
                    520, Qt.TransformationMode.SmoothTransformation))
                shown = True
        self.screenshot_label.setVisible(shown)
        self.open_button.setEnabled(bool(url))

    def hideEvent(self, event):  # noqa: N802 - Qt naming
        save_window_geometry(
            get_settings(), "browser_window", self)
        super().hideEvent(event)

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        save_window_geometry(
            get_settings(), "browser_window", self)
        super().closeEvent(event)


class CommandPalette(QDialog):
    """Every command in one searchable list (entry 025): type a
    few letters, press Enter. One keystroke opens it (Ctrl+K),
    and the list narrows as you type, so nothing in the app is
    more than a few keystrokes away. Commands are (title,
    shortcut text, callable) triples, supplied by the main
    window; the palette itself knows nothing about what they
    do."""

    def __init__(self, commands, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Commands")
        self.setMinimumSize(480, 300)
        self.resize(520, 340)
        self._commands = list(commands)
        self._filtered = list(commands)

        layout = QVBoxLayout(self)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Type a command")
        self.input.setClearButtonEnabled(True)
        self.input.textChanged.connect(self._filter)
        self.input.installEventFilter(self)
        layout.addWidget(self.input)

        self.list = QListWidget()
        self.list.itemActivated.connect(self._run_current)
        layout.addWidget(self.list)
        self._rebuild_list()

    def _rebuild_list(self):
        self.list.clear()
        for title, shortcut, _action in self._filtered:
            text = title if not shortcut else "%s  (%s)" % (
                title, shortcut)
            self.list.addItem(QListWidgetItem(text))
        if self._filtered:
            self.list.setCurrentRow(0)

    def _filter(self, text):
        needle = text.strip().lower()
        if not needle:
            self._filtered = list(self._commands)
        else:
            self._filtered = [
                command for command in self._commands
                if needle in command[0].lower()]
        self._rebuild_list()

    def _run_current(self):
        row = self.list.currentRow()
        if 0 <= row < len(self._filtered):
            _title, _shortcut, action = self._filtered[row]
            self.accept()
            action()

    def eventFilter(self, watched, event):  # noqa: N802 - Qt naming
        # Arrow keys move through the list while the input keeps
        # focus, so typing and choosing never need the mouse.
        if watched is self.input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Down:
                row = min(self.list.currentRow() + 1,
                          self.list.count() - 1)
                self.list.setCurrentRow(row)
                return True
            if key == Qt.Key.Key_Up:
                row = max(self.list.currentRow() - 1, 0)
                self.list.setCurrentRow(row)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._run_current()
                return True
        return super().eventFilter(watched, event)


# ---------------------------------------------------------------- tiles

# Bento grid numbers (entry 005): 3 columns at this window width,
# 16 px gaps, 12 px corners. Cards size to their content; the hero
# tile spans the full width and earns it with live stats. Compact
# mode (Settings) packs more in: 4 columns, 10 px gaps, shorter
# cards with the description and facts hidden (they stay in the
# tooltip).
GRID_COLUMNS = 3
GRID_SPACING = 16
GRID_COLUMNS_COMPACT = 4
GRID_SPACING_COMPACT = 10


def grid_columns():
    """The grid's column count for the current density."""
    return (GRID_COLUMNS_COMPACT if get_compact_list()
            else GRID_COLUMNS)


def grid_spacing():
    """The grid's gap for the current density."""
    return GRID_SPACING_COMPACT if get_compact_list() else GRID_SPACING


# The fit badges for the dark theme, kept under this name for the
# tests; a card under another theme uses fit_badges(tokens).
FIT_BADGES = fit_badges(DARK)


class ModelCard(QFrame):
    """One model as a bento tile: name big, size below, fit badge
    front and center, and the description from the file's own
    GGUF header when it has one (never invented). Click to
    select, double-click to load. Keyboard works too: Tab moves
    between cards, Space picks the focused card, Enter loads it."""

    # A description longer than this is clipped with an ellipsis:
    # a card is a glance, not a page.
    DESCRIPTION_MAX = 140

    clicked = Signal(str)         # path
    double_clicked = Signal(str)  # path
    remove_requested = Signal(str)  # path

    def __init__(self, model, status, loaded=False, hero=False,
                 free_bytes=None, parent=None, tokens=None,
                 compact=None):
        super().__init__(parent)
        # The theme this card draws with: passed in by the main
        # window, or the active theme when a test builds a card
        # on its own. apply_theme swaps it live.
        self._tokens = tokens or current_tokens()
        # Compact mode (Settings): shorter cards, description and
        # facts hidden (they stay in the tooltip).
        self._compact = (get_compact_list() if compact is None
                         else compact)
        self.path = str(model["path"])
        # The display name (an Ollama model's name, or the file
        # name) and where the model came from: "folder" for a file
        # in the models folder, "ollama" for one Ollama downloaded.
        self.model_name = model["name"]
        self.model_size = model["size"]
        self.source = model.get("source", "folder")
        # What this card shows, kept so a refresh can tell whether
        # the card is still true (and reuse the widget) or must be
        # rebuilt: a re-pulled file has a new size, a load makes a
        # hero. The fit status is not part of the match: free RAM
        # moves on its own, and the badge updates in place.
        self._status = status
        self._loaded = loaded
        self._hero = hero
        self._selected = False
        # Hover detail (entry 017): the full path, RAM needed vs
        # free, and why it fits or not, with no click needed. The
        # data is already in memory, so the preview is instant.
        self.setToolTip(
            self._build_tooltip(model, status, free_bytes))
        # Hover lift (entry 001): the card rises 4 px over 200 ms.
        # The hover timer guards the bottom 4 px strip: the lift
        # moves the card out from under a cursor parked there,
        # which would otherwise bob the card up and down forever.
        self._lift_anim = None
        self._lift_rest = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(150)
        self._hover_timer.timeout.connect(self._check_hover)
        # Keyboard focus: Tab lands on cards, so picking and
        # loading a model never needs the mouse.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        # The description, from the file's own header: clipped to
        # a glance, and absent entirely when the file has none.
        self.description = (model.get("description") or "").strip()
        if len(self.description) > self.DESCRIPTION_MAX:
            self.description = (self.description[:137] + "...")
        # The facts line, from the file's own header too
        # ("2B · Q4_K_M · 32K context"): absent entirely when the
        # header carries nothing to show.
        self.facts_text = format_facts(model.get("facts") or {})
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 10, 12, 10)
        self._layout.setSpacing(4)

        # No fixed point sizes on any label: the application font
        # (and the user's system text size) shows through, and the
        # name's weight, not a bigger size, marks it as the title.
        self._name_label = QLabel(model["name"])
        self._name_label.setWordWrap(True)
        self._layout.addWidget(self._name_label)

        self._size_label = QLabel(format_size(model["size"]))
        self._layout.addWidget(self._size_label)

        self._desc_label = None
        if self.description:
            self._desc_label = QLabel(self.description)
            self._desc_label.setObjectName("model_description")
            self._desc_label.setWordWrap(True)
            self._layout.addWidget(self._desc_label)

        self._facts_label = None
        if self.facts_text:
            self._facts_label = QLabel(self.facts_text)
            self._facts_label.setObjectName("model_facts")
            self._facts_label.setWordWrap(True)
            self._layout.addWidget(self._facts_label)

        badge_row = QHBoxLayout()
        self._badge_label = QLabel("")
        self._badge_label.setObjectName("fit_badge")
        badge_row.addWidget(self._badge_label)
        self._loaded_label = None
        if loaded:
            self._loaded_label = QLabel("Loaded")
            badge_row.addWidget(self._loaded_label)
        self._ollama_label = None
        if self.source == "ollama":
            # A quiet marker: this model came from Ollama's store,
            # so Fyrn can load it but not remove it.
            self._ollama_label = QLabel("Ollama")
            self._ollama_label.setObjectName("ollama_tag")
            badge_row.addWidget(self._ollama_label)
        badge_row.addStretch(1)
        self._layout.addLayout(badge_row)
        # The hero tile earns its size with live stats (load time,
        # then speed once a reply exists), updated by the main
        # window's stats refresh. Plain cards have no stats line.
        self.stats_label = None
        if hero:
            self.stats_label = QLabel("")
            self.stats_label.setObjectName("hero_stats")
            self.stats_label.setWordWrap(True)
            self._layout.addWidget(self.stats_label)
        self._layout.addStretch(1)

        self._apply_label_styles()
        self._apply_compact()
        self._apply_heights()

        # Clicks on the labels must reach the card, not stop at the
        # label: the whole tile is the thing you tap.
        for child in self.findChildren(QLabel):
            child.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._apply_frame()

    def _apply_label_styles(self):
        """Every label's style, from this card's theme tokens.
        Re-run by apply_theme when the theme changes live."""
        tokens = self._tokens
        self._name_label.setStyleSheet("font-weight: bold;")
        muted = "color: %s;" % tokens["text_muted"]
        self._size_label.setStyleSheet(muted)
        if self._desc_label is not None:
            self._desc_label.setStyleSheet(muted)
        if self._facts_label is not None:
            self._facts_label.setStyleSheet(muted)
        if self.stats_label is not None:
            self.stats_label.setStyleSheet(muted)
        self._apply_badge_style()
        if self._loaded_label is not None:
            self._loaded_label.setStyleSheet(
                "color: %s; font-weight: bold; border: 1px solid %s; "
                "padding: 2px 9px; border-radius: 8px;"
                % (tokens["accent"], tokens["accent"]))
        if self._ollama_label is not None:
            self._ollama_label.setStyleSheet(
                "color: %s; border: 1px solid %s; padding: 2px 9px; "
                "border-radius: 8px;"
                % (tokens["text_muted"], tokens["border_dim"]))

    def _apply_badge_style(self):
        """The fit badge's text and background, from this card's
        theme tokens and current fit status."""
        badge_text, badge_color = fit_badges(self._tokens)[self._status]
        self._badge_label.setText(badge_text)
        self._badge_label.setStyleSheet(
            "background-color: %s; color: %s; font-weight: bold; "
            "padding: 3px 10px; border-radius: 8px;"
            % (badge_color, self._tokens["badge_text"]))

    def _apply_heights(self):
        """Card height from the font's line height, not fixed
        pixels: a card holds name, size, and badge (four lines
        with room for a wrapped name), plus description, facts,
        and stats lines when it has them. The cap is still there
        (the grid's expanding spacer takes the leftover space,
        but without a cap the tiles grow past their content),
        only now it grows with the font instead of clipping a
        larger one. Compact cards are shorter: no description or
        facts, tighter margins."""
        line = self.fontMetrics().lineSpacing()
        if self._compact:
            self.setMinimumHeight(3 * line + 16)
            self.setMaximumHeight(4 * line + 16)
            return
        self.setMinimumHeight(4 * line + 32)
        if self._hero or self.description or self.facts_text:
            self.setMaximumHeight(8 * line + 32)
        else:
            self.setMaximumHeight(5 * line + 32)

    def _apply_compact(self):
        """Compact mode on this card: description and facts
        hidden (they stay in the tooltip), tighter margins and
        spacing. Full mode restores them."""
        if self._desc_label is not None:
            self._desc_label.setVisible(not self._compact)
        if self._facts_label is not None:
            self._facts_label.setVisible(not self._compact)
        if self._compact:
            self._layout.setContentsMargins(8, 5, 8, 5)
            self._layout.setSpacing(2)
        else:
            self._layout.setContentsMargins(12, 10, 12, 10)
            self._layout.setSpacing(4)

    def set_compact(self, compact):
        """Switch this card's density live (Settings)."""
        self._compact = bool(compact)
        self._apply_compact()
        self._apply_heights()

    def apply_theme(self, tokens):
        """Swap this card's theme live (Settings)."""
        self._tokens = tokens
        self._apply_label_styles()
        self._apply_frame()

    def set_stats(self, text):
        """Show live stats in the hero tile (no-op on a plain
        card)."""
        if self.stats_label is not None:
            self.stats_label.setText(text)

    def matches(self, model, loaded):
        """True when this card still shows this model: same size,
        description, and facts, and the same loaded state. A card
        that matches is reused across a refresh (so scroll and
        focus survive); anything else is rebuilt."""
        description = (model.get("description") or "").strip()
        if len(description) > self.DESCRIPTION_MAX:
            description = description[:137] + "..."
        return (
            model["size"] == self.model_size
            and description == self.description
            and format_facts(model.get("facts") or {})
            == self.facts_text
            and loaded == self._loaded
            and model.get("source", "folder") == self.source)

    def set_fit_status(self, status, model, free_bytes):
        """Update the fit badge (and the hover detail) in place:
        free RAM moves between refreshes, and the badge follows
        it without rebuilding the card."""
        self._status = status
        self._apply_badge_style()
        self.setToolTip(
            self._build_tooltip(model, status, free_bytes))

    @staticmethod
    def _build_tooltip(model, status, free_bytes):
        """Hover detail text: full path, size, RAM needed vs free,
        and the fit reason, in plain words."""
        lines = ["Full path: %s" % model["path"],
                 "Size: %s" % format_size(model["size"])]
        description = (model.get("description") or "").strip()
        if description:
            if len(description) > ModelCard.DESCRIPTION_MAX:
                description = description[:137] + "..."
            lines.append("About: %s" % description)
        architecture = (model.get("facts") or {}).get("architecture")
        if architecture:
            lines.append("Architecture: %s" % architecture)
        # The facts line (params, quant, context): compact mode
        # hides the facts label, so the tooltip is the only place
        # left to read it. Present in both modes, for one tooltip.
        facts_text = format_facts(model.get("facts") or {})
        if facts_text:
            lines.append(facts_text)
        headroom = model.get("headroom_bytes", HEADROOM_BYTES)
        needed = model["size"] + headroom
        if free_bytes is None:
            lines.append("Needs %s of RAM, free RAM unknown"
                         % format_size(needed))
        else:
            lines.append("Needs %s of RAM, %s free"
                         % (format_size(needed),
                            format_size(free_bytes)))
        reasons = {
            "fits": "Fits: needs less than 3/4 of free RAM.",
            "tight": "Tight: fits, but leaves little spare RAM.",
            "too big": "Too big: needs more RAM than is free.",
            "unknown": "RAM could not be read, so the fit is "
                       "unknown.",
        }
        lines.append(reasons[status])
        if model.get("source") == "ollama":
            lines.append("From Ollama: it stays where Ollama put "
                         "it. Fyrn can load it, but not remove it.")
        return "\n".join(lines)

    def set_selected(self, selected):
        self._selected = selected
        self._apply_frame()

    def _apply_frame(self):
        # Selected and keyboard-focused are two different states
        # and must never look the same: Load acts on the
        # selection, so a card that only has focus must not read
        # as selected. Selected is a solid amber border; focused
        # but not selected is a dashed blue ring; selected and
        # focused is solid amber (the selection wins, because it
        # is the one Load will act on). Labels inside the card
        # are transparent: the global widget background is darker
        # than the card, so an opaque label shows a darker band
        # behind its text. The badge keeps its own background
        # (its own stylesheet wins over this descendant rule).
        tokens = self._tokens
        if self._selected:
            border = "2px solid %s" % tokens["accent"]
        elif self.hasFocus():
            border = "2px dashed %s" % tokens["focus_ring"]
        else:
            border = "1px solid %s" % tokens["border_dim"]
        self.setStyleSheet(
            "ModelCard { border: %s; border-radius: 12px; "
            "background-color: %s; } "
            "ModelCard QLabel { background-color: transparent; }"
            % (border, tokens["bg_raised"]))

    def focusInEvent(self, event):  # noqa: N802 - Qt naming
        self._apply_frame()
        # Keyboard focus shows the same detail hover does, so a
        # keyboard user gets the preview without a mouse. The
        # tooltip is shown on a short delay, not in the focus
        # event itself: showText called during focusInEvent never
        # renders (Qt hides it again as the focus change
        # settles), while the same call 100 ms later shows.
        # The timer checks the card still has focus when it
        # fires, and never touches a deleted card.
        QTimer.singleShot(
            100,
            lambda: (self._show_focus_tooltip()
                     if isValid(self) else None))
        super().focusInEvent(event)

    def _show_focus_tooltip(self):
        """Show the hover detail for a focused card (task 7):
        path, size, RAM need, and fit reason, the same text the
        mouse hover shows."""
        if not self.hasFocus():
            return
        QToolTip.showText(
            self.mapToGlobal(QPoint(0, self.height())),
            self.toolTip(), self)

    def focusOutEvent(self, event):  # noqa: N802 - Qt naming
        QToolTip.hideText()
        self._apply_frame()
        super().focusOutEvent(event)

    def keyPressEvent(self, event):  # noqa: N802 - Qt naming
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.clicked.emit(self.path)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.double_clicked.emit(self.path)
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):  # noqa: N802 - Qt naming
        self.clicked.emit(self.path)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - Qt naming
        self.double_clicked.emit(self.path)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):  # noqa: N802 - Qt naming
        """Right-click menu: Remove. The confirm itself lives in
        the main window, so every remove path asks the same way."""
        menu = QMenu(self)
        action = menu.addAction("Remove")
        chosen = menu.exec(event.globalPos())
        if chosen is action:
            self.remove_requested.emit(self.path)

    def enterEvent(self, event):  # noqa: N802 - Qt naming
        if not reduced_motion():
            self._start_lift()
        self._hover_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 - Qt naming
        # If the cursor is still inside the card's resting place,
        # the lift itself caused this leave; the hover timer drops
        # the card once the cursor truly moves away.
        if self._cursor_outside_rest():
            self._end_lift()
            self._hover_timer.stop()
        super().leaveEvent(event)

    def _start_lift(self):
        if self._lift_rest is not None:
            return  # already lifted (or dropping back: let it land)
        rest = self.geometry()
        self._lift_rest = QRect(rest)
        lifted = QRect(rest)
        lifted.translate(0, -4)
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(200)
        anim.setStartValue(rest)
        anim.setEndValue(lifted)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        # The rest captured here can be stale: the layout
        # activates while the card is still settling (the grid
        # grows as the scroll area makes room), so the geometry
        # at lift time is not always the card's true cell. When
        # the lift finishes, the card is re-synced to its true
        # cell (see _on_lift_finished), instead of sitting at the
        # stale spot forever.
        anim.finished.connect(
            lambda a=anim, r=rest: self._on_lift_finished(a, r))
        self._lift_anim = anim
        anim.start()

    def _on_lift_finished(self, anim, rest):
        """The lift landed: put the card above its TRUE cell, not
        the rest captured when the lift started (task 3). The
        layout may have moved the card while it was lifting (a
        scrollbar appearing, a resize): the animation just
        parked the card at stale-rest-minus-4px, and nothing
        re-runs the layout afterwards, so without this the card
        sits in the wrong cell until the next refresh. Re-derive
        the cell from the layout and sit lifted above it."""
        if self._lift_anim is not anim:
            return  # a newer lift/drop owns the geometry now
        parent = self.parentWidget()
        layout = parent.layout() if parent is not None else None
        if layout is not None:
            layout.invalidate()
            layout.activate()
            cell = QRect(self.geometry())
        else:
            cell = QRect(rest)
        self._lift_rest = cell
        lifted = QRect(cell)
        lifted.translate(0, -4)
        self.setGeometry(lifted)

    def _end_lift(self):
        if self._lift_rest is None:
            return
        # The drop target is re-derived from the layout (task 3):
        # the stored rest can be stale (the layout moved the card
        # while it was lifted), and dropping to a stale rest
        # parks the card in the wrong cell. The layout is asked
        # for the true cell, then the card is put back at its
        # lifted spot so the animation still drops it visibly.
        start = QRect(self.geometry())
        parent = self.parentWidget()
        layout = parent.layout() if parent is not None else None
        if layout is not None:
            layout.invalidate()
            layout.activate()
            rest = QRect(self.geometry())
            self.setGeometry(start)
        else:
            rest = self._lift_rest
        self._lift_rest = rest
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(200)
        anim.setStartValue(start)
        anim.setEndValue(rest)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(
            lambda a=anim, r=rest: self._on_drop_finished(a, r))
        self._lift_anim = anim
        anim.start()

    def cancel_lift(self):
        """Stop any lift or drop in flight and forget the rest
        (task 3): a relayout owns the card's geometry, and an
        animation writing stale values during it would fight the
        layout and park the card in the wrong cell. The layout's
        own activation puts the card in its cell; if the cursor
        is still over it, the next mouse move lifts it again."""
        anim = self._lift_anim
        self._lift_anim = None
        self._lift_rest = None
        self._hover_timer.stop()
        if anim is not None:
            anim.stop()

    def _on_drop_finished(self, anim, rest):
        # Only the current animation may clear the rest: a stale
        # drop finishing after a new lift must not un-rest the card.
        if self._lift_anim is anim and self._lift_rest == rest:
            self._lift_rest = None

    def _cursor_outside_rest(self):
        if self._lift_rest is None or self.parentWidget() is None:
            return True
        point = self.parentWidget().mapFromGlobal(QCursor.pos())
        return not self._lift_rest.contains(point)

    def _check_hover(self):
        """Drop a lifted card once the cursor has truly left it:
        outside both the lifted card and its resting place."""
        if self._lift_rest is None or self.parentWidget() is None:
            self._hover_timer.stop()
            return
        point = self.parentWidget().mapFromGlobal(QCursor.pos())
        if (self._lift_rest.contains(point)
                or self.geometry().contains(point)):
            return
        self._end_lift()
        self._hover_timer.stop()


class SkeletonCard(QFrame):
    """Placeholder tile shown while the model list loads.

    Grey shape blocks where the content will land: a name bar, a
    size bar, a badge bar. No text: a skeleton is a shape, not a
    label, and three tiles reading "Loading models..." read as
    three labels (L1).
    """

    # (width, height) of each grey bar, in the shape of a card's
    # name, size, and fit badge.
    BAR_SIZES = ((160, 16), (70, 12), (60, 20))

    def __init__(self, parent=None, tokens=None):
        super().__init__(parent)
        self._tokens = tokens or current_tokens()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        # Same height as a regular card, so the list does not jump
        # when the skeletons are replaced by real cards. Font
        # derived, like the real card's, so a larger system text
        # size does not make the skeletons shorter than the cards
        # that replace them.
        line = self.fontMetrics().lineSpacing()
        self.setMinimumHeight(4 * line + 32)
        self.setMaximumHeight(5 * line + 32)
        tokens = self._tokens
        self.setStyleSheet(
            "SkeletonCard { border: 1px solid %s; "
            "border-radius: 12px; background-color: %s; } "
            "QFrame#skeleton_bar { background-color: %s; "
            "border: none; border-radius: 4px; }"
            % (tokens["skeleton_border"], tokens["skeleton_bg"],
               tokens["skeleton_bar"]))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        self.bars = []
        for width, height in self.BAR_SIZES:
            bar = QFrame()
            bar.setObjectName("skeleton_bar")
            bar.setFixedSize(width, height)
            layout.addWidget(bar)
            self.bars.append(bar)
        layout.addStretch(1)


class ChatInput(QTextEdit):
    """The chat prompt box: multi-line, Enter sends, Shift+Enter
    is a newline (L4). Grows from one line to four as the message
    grows, then scrolls. No formatting: plain text only."""

    send_requested = Signal()

    MIN_HEIGHT = 34
    MAX_HEIGHT = 110

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("Type a message")
        self.setAcceptRichText(False)
        self.setMaximumHeight(self.MAX_HEIGHT)
        self.setMinimumHeight(self.MIN_HEIGHT)
        self.textChanged.connect(self._fit_height)
        self._fit_height()

    def text(self):
        """The message, as plain text (QTextEdit has no text())."""
        return self.toPlainText()

    def _fit_height(self):
        doc_height = int(self.document().size().height())
        height = doc_height + 14  # frame and padding
        self.setFixedHeight(max(self.MIN_HEIGHT,
                                min(self.MAX_HEIGHT, height)))

    def keyPressEvent(self, event):  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                # Shift+Enter: a newline, the default behaviour.
                super().keyPressEvent(event)
            else:
                self.send_requested.emit()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------- main

# The line between chat turns: a plain row of hyphens (a hyphen
# line, never an em-dash — U33), so long replies do not run
# together into one wall of text.
TURN_DIVIDER = "-" * 24

# How long a transient status-bar message stays before it falls
# back to the neutral line (5 s: long enough to read, short
# enough that stale news never lingers on an unrelated screen).
STATUS_TIMEOUT_MS = 5000


class MainWindow(QMainWindow):
    """The Fyrn window: Models and Chat tabs."""

    def __init__(self, runner=None, log=None):
        super().__init__()
        self.setWindowTitle("Fyrn")
        self.setMinimumSize(900, 600)
        self.resize(DEFAULT_WIDTH, DEFAULT_HEIGHT)
        self._restore_geometry()
        if ICON_PATH.is_file():
            self.setWindowIcon(QIcon(str(ICON_PATH)))

        home = ensure_home()
        self._home = home
        # The debug logger is created lazily (see the log
        # property): importing its module costs ~0.5 s, and
        # nothing needs the logger before the window is shown.
        # main() warms it on the first event-loop turn after the
        # window appears, so it is ready before the user can do
        # anything; any earlier access creates it on the spot.
        # A logger passed in (tests) is used as-is.
        self._log = log
        self._log_ready = log is not None

        self.runner = runner if runner is not None else LlamaRunner()
        self.controller = ModelController(self.runner)
        self.models_folder = ensure_models_folder(get_models_folder())

        self.load_worker = None
        self.chat_worker = None
        self.import_worker = None
        self._update_check_worker = None
        self._update_check_manual = False
        self._update_download_worker = None
        self._update_progress = None
        self._update_release = None
        self.pull_window = None
        self.settings_window = None
        self.howto_window = None
        self.tray = None
        self.load_time_s = None
        self._reply_in_progress = False
        # True when the user asked to close while a worker was
        # still running: the window closes once every worker has
        # stopped, instead of destroying a running thread.
        self._close_pending = False
        # The conversation, as message dicts the model can read:
        # user and assistant turns, trimmed to the budget. The
        # transcript on screen is the same conversation in words.
        self._chat_history = []
        self._current_reply = ""

        # Saved chats (v0.8): the store, the open chat's id (None
        # until its first save), its display messages (user,
        # assistant and tool lines — the transcript kept as data),
        # and a chat switch waiting for a streaming reply to stop.
        self._chat_store = ChatStore()
        self._current_chat_id = None
        self._current_title = "New chat"
        self._current_title_auto = True
        self._current_created = None
        self._display_messages = []
        self._pending_chat_action = None
        self._executor = None
        self.browser_window = None
        self._reply_name = "Model"
        # Set when a model's tool path failed mid-turn and the
        # turn fell back to a plain reply: tools stay off for the
        # rest of this model's session.
        self._tools_broken = False
        # Whether the "Name: " prefix of the streaming reply has
        # been written yet (tool lines can come before it in v0.8).
        self._reply_prefix_done = False
        # Instructions save a moment after typing stops, not on
        # every keystroke.
        self._instructions_timer = QTimer(self)
        self._instructions_timer.setSingleShot(True)
        self._instructions_timer.setInterval(800)
        self._instructions_timer.timeout.connect(
            self._save_current_chat)

        # Model tiles (bento grid): the scan runs off the UI thread
        # and the tiles fill in when it finishes.
        self._scan_worker = None
        self._scan_dirty = False
        self._selected_path = None
        # A model to select once the next scan finishes (set by
        # import/pull, which select the new model).
        self._select_path = None
        self._hero_card = None  # the loaded model's tile, if any
        self.model_cards = {}  # path string -> ModelCard
        # Display order of the cards (path strings): the hero
        # first, then name order. Kept apart from the dict, which
        # holds no order, so a refresh can lay the same widgets
        # out again without deleting them.
        self._card_order = []
        # Cards the search and filter let through (path strings):
        # Load and Remove only work on a card that is visible.
        self._visible_paths = set()
        self._pulse_path = None
        self._pulse_anim = None
        # List-motion animations in flight: stagger fades and
        # FLIP ghosts. Kept alive here (an animation that is
        # garbage-collected stops dead) and dropped when each
        # one finishes.
        self._stagger_anims = []
        self._flip_anims = []
        self._overscroll_anims = {}
        # Files dropped on the window, waiting their turn to
        # import: one import runs at a time, the rest queue.
        self._import_pending = []
        # A picture attached to the next chat message (a vision
        # model only): None when nothing is attached.
        self._attached_image = None
        # A .gguf file dropped on the window imports it, the same
        # way Import does (dragEnterEvent / dropEvent below). A
        # QMainWindow accepts drops by default, so no setup is
        # needed here.
        # True while the status bar shows the empty-folder message
        # (so restoring the default message never clobbers a load
        # or unload message).
        self._empty_status = False
        # A status message to show once the next scan finishes
        # (remove/import confirmations): the scan's own status
        # update would clobber a message shown before the scan.
        self._status_after_scan = None

        self._build_menus()
        self._build_tabs()

        self.busy_dot = QLabel("\u25cf")
        self.busy_dot.setObjectName("busy_dot")
        self.busy_dot.setStyleSheet(
            "color: %s;" % current_tokens()["accent"])
        self.busy_dot.setVisible(False)
        self.statusBar().addPermanentWidget(self.busy_dot)
        # Transient status messages fall back to the neutral line
        # after a timeout (L2); the timer is single-shot and is
        # restarted by each transient message.
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._on_status_timeout)
        self.statusBar().showMessage(
            "Pick a model and press Load, or pull one from the "
            "Models menu.")

        # Live RAM numbers, refreshed every 2 seconds (U37).
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(2000)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start()

        # The first scan starts here, in the constructor, not
        # deferred: it runs on its own thread, so it costs the
        # first paint nothing but the skeleton layout (~10 ms),
        # and starting it here keeps the invariant the tests and
        # closeEvent rely on — _scan_worker is set from the
        # moment the window exists, so a close always waits for
        # it. (A singleShot deferral was tried and reverted: the
        # scan could then start after closeEvent had already run,
        # and the orphaned worker aborted the process.)
        self._refresh_model_list()
        self._refresh_stats()
        self._setup_tray()

    # ------------------------------------------------------------ menus

    @property
    def log(self):
        """The debug logger, created on first use (its module
        import costs ~0.5 s, which is why it is not paid during
        construction)."""
        if not self._log_ready:
            self._init_logger()
        return self._log

    def _init_logger(self):
        """Create the debug logger: dependencies, crash handler,
        and the models-folder setting. Called by main() once the
        window is visible, or by the log property on first use —
        whichever comes first."""
        if self._log_ready:
            return
        from .debug_log import DebugLogger
        self._log = DebugLogger(
            "fyrn", __version__, log_dir=str(self._home / "logs"))
        self._log.register_dependency("PySide6")
        self._log.register_dependency("llama-cpp-python")
        self._log.install_crash_handler()
        self._log.set_setting("models_folder",
                              str(self.models_folder))
        self._log_ready = True

    def _build_menus(self):
        # Every action that has a shortcut shows it in the menu,
        # and the same shortcuts are listed on the buttons and in
        # the command palette, so there is one set to learn.
        file_menu = self.menuBar().addMenu("File")
        new_chat_action = file_menu.addAction("New chat")
        new_chat_action.setShortcut(QKeySequence("Ctrl+N"))
        new_chat_action.triggered.connect(self.new_chat)
        settings_action = file_menu.addAction("Settings")
        settings_action.setShortcut(QKeySequence("Ctrl+,"))
        settings_action.triggered.connect(self.show_settings)
        save_chat_action = file_menu.addAction("Save chat")
        save_chat_action.setShortcut(QKeySequence("Ctrl+S"))
        save_chat_action.triggered.connect(self._save_chat)
        report_action = file_menu.addAction("Debug report")
        report_action.triggered.connect(self.make_debug_report)
        file_menu.addSeparator()
        quit_action = file_menu.addAction("Quit")
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.close)

        models_menu = self.menuBar().addMenu("Models")
        load_action = models_menu.addAction("Load selected model")
        load_action.setShortcut(QKeySequence("Ctrl+L"))
        load_action.triggered.connect(self.load_selected_model)
        pull_action = models_menu.addAction("Pull models")
        pull_action.setShortcut(QKeySequence("Ctrl+P"))
        pull_action.triggered.connect(self.show_pull_window)
        import_action = models_menu.addAction("Import a GGUF file")
        import_action.setShortcut(QKeySequence("Ctrl+I"))
        import_action.triggered.connect(self.import_model)
        remove_action = models_menu.addAction("Remove model")
        remove_action.setShortcut(QKeySequence("Del"))
        remove_action.triggered.connect(self.remove_selected_model)
        refresh_action = models_menu.addAction("Refresh list")
        refresh_action.setShortcut(QKeySequence("F5"))
        refresh_action.triggered.connect(self._refresh_model_list)
        unload_action = models_menu.addAction("Unload model")
        unload_action.setShortcut(QKeySequence("Ctrl+U"))
        unload_action.triggered.connect(self.unload_model)
        # The Models actions are stored (they used to be locals)
        # so _update_buttons can enable and disable them with
        # the state, the way the buttons already are (task 6).
        self._load_action = load_action
        self._pull_action = pull_action
        self._import_action = import_action
        self._remove_action = remove_action
        self._refresh_action = refresh_action
        self._unload_action = unload_action

        help_menu = self.menuBar().addMenu("Help")
        palette_action = help_menu.addAction("Command palette")
        palette_action.setShortcut(QKeySequence("Ctrl+K"))
        palette_action.triggered.connect(self.show_command_palette)
        howto_action = help_menu.addAction("How to Use")
        howto_action.setShortcut(QKeySequence("F1"))
        howto_action.triggered.connect(self.show_how_to_use)
        update_action = help_menu.addAction("Check for updates")
        update_action.triggered.connect(
            lambda: self.check_for_updates(manual=True))
        about_action = help_menu.addAction("About")
        about_action.triggered.connect(self.show_about)

        # Shortcuts with no menu entry: focusing the search box.
        focus_search = QShortcut(QKeySequence("Ctrl+F"), self)
        focus_search.activated.connect(self._focus_search)

    def _focus_search(self):
        """Jump to the model search box (Ctrl+F), on the Models
        tab, with the current search selected so typing replaces
        it."""
        self.tabs.setCurrentIndex(0)
        self.search_box.setFocus()
        self.search_box.selectAll()

    def _commands(self):
        """Every command the palette offers: (title, shortcut,
        callable). The titles are the menu and button words, so
        the palette reads the same as the rest of the app."""
        return [
            ("Load selected model", "Ctrl+L",
             self.load_selected_model),
            ("Unload model", "Ctrl+U", self.unload_model),
            ("Pull models", "Ctrl+P", self.show_pull_window),
            ("Import a GGUF file", "Ctrl+I", self.import_model),
            ("Remove selected model", "Delete",
             self.remove_selected_model),
            ("Refresh list", "F5", self._refresh_model_list),
            ("Focus search", "Ctrl+F", self._focus_search),
            ("Settings", "Ctrl+,", self.show_settings),
            ("How to Use", "F1", self.show_how_to_use),
            ("Debug report", "", self.make_debug_report),
            ("Clear chat", "", self._clear_chat),
            ("Save chat", "Ctrl+S", self._save_chat),
            ("New chat", "Ctrl+N", self.new_chat),
            ("Rename chat", "", self.rename_chat),
            ("Delete chat", "", self.delete_chat),
            ("Browser view", "", self.show_browser_window),
            ("Check for updates", "",
             lambda: self.check_for_updates(manual=True)),
            ("Go to Models tab", "",
             lambda: self.tabs.setCurrentIndex(0)),
            ("Go to Chat tab", "",
             lambda: self.tabs.setCurrentIndex(1)),
            ("Quit", "Ctrl+Q", self.close),
        ]

    def show_command_palette(self):
        """Open the command palette (Ctrl+K). Modal: it takes
        the keyboard until a command is picked or Esc is hit."""
        palette = CommandPalette(self._commands(), parent=self)
        palette.exec()

    # ------------------------------------------------------------ tabs

    def _build_tabs(self):
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self._build_models_tab()
        self._build_chat_tab()
        # Button state only once both tabs exist: _update_buttons
        # touches the chat tab's Send button too, and calling it
        # from _build_models_tab crashed the app on startup.
        self._update_buttons()

    def _build_models_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Search and filter: the list narrows as you type, and the
        # menu narrows it by size or where it came from. Both are
        # plain filters over what is already there.
        search_row = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search models")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._apply_model_filter)
        search_row.addWidget(self.search_box)
        self.filter_combo = QComboBox()
        self.filter_combo.addItems([
            "All models", "Small (under 1 GB)",
            "Medium (1-3 GB)", "Large (over 3 GB)",
            "From Ollama", "In my folder"])
        self.filter_combo.currentIndexChanged.connect(
            self._apply_model_filter)
        search_row.addWidget(self.filter_combo)
        layout.addLayout(search_row)

        # Bento grid: models as tiles, size as hierarchy, fit front
        # and center. The loaded model gets the hero tile. The
        # scroll area itself is out of the tab order (task 15):
        # Tab used to land on the bare container with no visible
        # focus between the filter and the buttons. Keyboard
        # scrolling still works: the grid scrolls to show the
        # focused card on its own.
        self._cards_scroll = QScrollArea()
        self._cards_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._cards_scroll.setWidgetResizable(True)
        self._cards_widget = QWidget()
        self._cards_layout = QGridLayout(self._cards_widget)
        self._cards_layout.setSpacing(GRID_SPACING)
        self._cards_scroll.setWidget(self._cards_widget)
        layout.addWidget(self._cards_scroll)

        # Overscroll glow (entry 031): a subtle edge glow when
        # the list is scrolled past its top or bottom. Two
        # overlays on the viewport, hidden until a wheel event
        # pushes past an edge. They never take mouse events, so
        # scrolling and clicking pass straight through them.
        viewport = self._cards_scroll.viewport()
        self._overscroll_top = QLabel(viewport)
        self._overscroll_bottom = QLabel(viewport)
        for glow in (self._overscroll_top, self._overscroll_bottom):
            glow.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            glow.setVisible(False)
        self._apply_overscroll_style()
        viewport.installEventFilter(self)

        # The filter found nothing, but models exist: say so in
        # plain words, in place of the empty grid, instead of
        # leaving a blank tab that looks broken.
        self._filter_empty_label = QLabel("")
        self._filter_empty_label.setWordWrap(True)
        self._filter_empty_label.setVisible(False)
        layout.addWidget(self._filter_empty_label)

        # The empty state takes the same space when the folder has
        # no models at all: a blank tab with nothing to pick is a
        # dead end, so it says what a model is, what to do first,
        # and offers the two ways to get one.
        self._empty_state = self._build_empty_state()
        self._empty_state.setVisible(False)
        layout.addWidget(self._empty_state)

        self.skipped_label = QLabel("")
        self.skipped_label.setWordWrap(True)
        self.skipped_label.setVisible(False)
        layout.addWidget(self.skipped_label)

        self.load_progress = QProgressBar()
        self.load_progress.setRange(0, 100)
        self.load_progress.setValue(0)
        # No text on the bar itself: the load stage is in the
        # status line, and bar text is unreadable where the fill
        # passes under it.
        self.load_progress.setTextVisible(False)
        self.load_progress.setVisible(False)
        layout.addWidget(self.load_progress)

        buttons = QHBoxLayout()
        # Every button carries a tooltip: what it does, and its
        # shortcut when it has one. No button is left bare.
        self.load_button = PressDipButton("Load")
        self.load_button.setToolTip(
            "Load the selected model (Ctrl+L)")
        self.load_button.clicked.connect(self.load_selected_model)
        self.unload_button = QPushButton("Unload")
        self.unload_button.setToolTip(
            "Unload the loaded model, freeing its RAM (Ctrl+U)")
        self.unload_button.clicked.connect(self.unload_model)
        self.import_button = QPushButton("Import")
        self.import_button.setToolTip(
            "Copy a .gguf file you already have into the models "
            "folder (Ctrl+I)")
        self.import_button.clicked.connect(self.import_model)
        self.pull_button = PressDipButton("Pull")
        self.pull_button.setToolTip(
            "Download a model from Hugging Face (Ctrl+P)")
        self.pull_button.clicked.connect(self.show_pull_window)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setToolTip(
            "Move the selected model to the trash (Delete)")
        self.remove_button.clicked.connect(self.remove_selected_model)
        self.load_cancel_button = QPushButton("Cancel load")
        self.load_cancel_button.setToolTip(
            "Stop loading this model")
        self.load_cancel_button.clicked.connect(self._cancel_load)
        self.load_cancel_button.setVisible(False)
        self.import_cancel_button = QPushButton("Cancel import")
        self.import_cancel_button.setToolTip(
            "Stop copying this file")
        self.import_cancel_button.clicked.connect(self._cancel_import)
        self.import_cancel_button.setVisible(False)
        buttons.addWidget(self.load_button)
        buttons.addWidget(self.unload_button)
        buttons.addWidget(self.import_button)
        buttons.addWidget(self.pull_button)
        buttons.addWidget(self.remove_button)
        buttons.addWidget(self.load_cancel_button)
        buttons.addWidget(self.import_cancel_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        folder_label = QLabel(
            "Models live in the folder set in the File menu, "
            "under Settings.")
        folder_label.setWordWrap(True)
        layout.addWidget(folder_label)

        # The double-click hint hides with the empty state (task
        # 16): with no models there is nothing to double-click.
        self._hint_label = QLabel("Double-click a model to load it.")
        self._hint_label.setWordWrap(True)
        layout.addWidget(self._hint_label)

        self.tabs.addTab(tab, "Models")

    def _apply_empty_state_style(self):
        """The empty-state panel's frame, from the active theme's
        tokens. Re-run when the theme changes live."""
        tokens = current_tokens()
        self._empty_state_frame.setStyleSheet(
            "QFrame#empty_state { border: 1px solid %s; "
            "border-radius: 12px; background-color: %s; } "
            "QFrame#empty_state QLabel { "
            "background-color: transparent; }"
            % (tokens["border_dim"], tokens["bg_raised"]))

    def _build_empty_state(self):
        """The no-models-yet panel: plain words, a short setup
        checklist, and working Pull and Import buttons wired to
        the same actions as the buttons below."""
        frame = QFrame()
        frame.setObjectName("empty_state")
        self._empty_state_frame = frame
        self._apply_empty_state_style()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(8)

        title = QLabel("No models yet")
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)

        what = QLabel(
            "A model is the brain Fyrn chats with. Get one to "
            "get started.")
        what.setWordWrap(True)
        layout.addWidget(what)

        steps = QLabel(
            "1. Pull a model, or Import one you already have\n"
            "2. Pick it and press Load\n"
            "3. Chat")
        steps.setWordWrap(True)
        layout.addWidget(steps)

        buttons = QHBoxLayout()
        pull_button = QPushButton("Pull")
        pull_button.setToolTip(
            "Download a model from Hugging Face (Ctrl+P)")
        pull_button.clicked.connect(self.show_pull_window)
        import_button = QPushButton("Import")
        import_button.setToolTip(
            "Copy a .gguf file you already have into the models "
            "folder (Ctrl+I)")
        import_button.clicked.connect(self.import_model)
        buttons.addWidget(pull_button)
        buttons.addWidget(import_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return frame

    def _set_empty_state(self, show):
        """Show the empty state (and hide the card grid) or the
        other way round. The status bar follows: it must never
        say "Pick a model" when there is nothing to pick. When
        the grid comes back, the filter owns whether it shows:
        a filter that matches nothing keeps the "no models match"
        line in place of the empty grid."""
        self._empty_state.setVisible(show)
        # The double-click hint goes with the grid (task 16):
        # with no models there is nothing to double-click, and
        # the hint sitting under the empty state reads as a
        # leftover.
        self._hint_label.setVisible(not show)
        if show:
            self._cards_scroll.setVisible(False)
            self._filter_empty_label.setVisible(False)
            if (not self.controller.is_loaded
                    and self.load_worker is None):
                self._show_status_persistent(
                    "No models yet. Pull one or import one to "
                    "get started.")
                self._empty_status = True
        else:
            if self._empty_status:
                self._show_status_persistent(
                    "Pick a model and press Load, or pull one from "
                    "the Models menu.")
                self._empty_status = False
            self._apply_model_filter()

    def _build_chat_tab(self):
        tab = QWidget()
        layout = QHBoxLayout(tab)

        # Saved chats (v0.8): the sidebar lists the chats saved
        # on disk; a chat saves itself as the user talks, and
        # clicking one reopens it.
        sidebar = QWidget()
        sidebar.setFixedWidth(216)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(0, 0, 8, 0)
        side_header = QHBoxLayout()
        side_header.addWidget(QLabel("Chats"))
        side_header.addStretch(1)
        self.new_chat_button = QPushButton("New chat")
        self.new_chat_button.setToolTip(
            "Start a new chat (Ctrl+N)")
        self.new_chat_button.clicked.connect(self.new_chat)
        side_header.addWidget(self.new_chat_button)
        side_layout.addLayout(side_header)
        self.chat_list = QListWidget()
        self.chat_list.setToolTip("Your saved chats")
        self.chat_list.currentRowChanged.connect(
            self._on_chat_list_row)
        side_layout.addWidget(self.chat_list, 1)
        chat_buttons = QHBoxLayout()
        self.rename_chat_button = QPushButton("Rename")
        self.rename_chat_button.setToolTip("Rename the open chat")
        self.rename_chat_button.clicked.connect(self.rename_chat)
        self.delete_chat_button = QPushButton("Delete")
        self.delete_chat_button.setToolTip("Delete the open chat")
        self.delete_chat_button.clicked.connect(self.delete_chat)
        chat_buttons.addWidget(self.rename_chat_button)
        chat_buttons.addWidget(self.delete_chat_button)
        side_layout.addLayout(chat_buttons)
        layout.addWidget(sidebar)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Instructions for this chat, and the tool controls: the
        # instructions are saved with the chat and sent as its
        # system prompt.
        tools_row = QHBoxLayout()
        tools_row.addWidget(QLabel("Instructions for this chat"))
        tools_row.addStretch(1)
        self.tools_folder_button = QPushButton("Tools folder...")
        self.tools_folder_button.setToolTip(
            "Pick the folder the model's file tools may use")
        self.tools_folder_button.clicked.connect(
            self._pick_tools_folder)
        tools_row.addWidget(self.tools_folder_button)
        self.browser_view_button = QPushButton("Browser view")
        self.browser_view_button.setToolTip(
            "See what the model's web tools are doing")
        self.browser_view_button.clicked.connect(
            self.show_browser_window)
        tools_row.addWidget(self.browser_view_button)
        right_layout.addLayout(tools_row)
        self.instructions_edit = QPlainTextEdit()
        self.instructions_edit.setPlaceholderText(
            "Tell the model how to act in this chat, e.g. 'You "
            "are a patient teacher. Keep answers short.' Saved "
            "with the chat; empty means no instructions.")
        self.instructions_edit.setMaximumHeight(58)
        self.instructions_edit.textChanged.connect(
            self._on_instructions_changed)
        right_layout.addWidget(self.instructions_edit)
        self.tools_status_label = QLabel("")
        self.tools_status_label.setWordWrap(True)
        self.tools_status_label.setStyleSheet(
            "color: %s;" % current_tokens()["text_muted"])
        right_layout.addWidget(self.tools_status_label)

        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText(
            "Load a model first, then chat here.")
        # Drops fall through to the window (a .gguf dropped on the
        # transcript imports it); the text box never swallows one.
        self.transcript.setAcceptDrops(False)
        right_layout.addWidget(self.transcript)

        # Calm technology: the live numbers sit quietly in muted
        # text. They inform at a glance, they never shout.
        self.stats_label = QLabel("No model loaded.")
        self.stats_label.setWordWrap(True)
        self.stats_label.setStyleSheet(
            "color: %s;" % current_tokens()["text_muted"])
        right_layout.addWidget(self.stats_label)

        # The attached picture, for the next message to a vision
        # model: shown only while one is attached.
        attach_row = QHBoxLayout()
        self.attachment_label = QLabel("")
        self.attachment_label.setWordWrap(True)
        attach_row.addWidget(self.attachment_label)
        self.attachment_remove_button = QPushButton("Remove")
        self.attachment_remove_button.setToolTip(
            "Take this picture off the next message")
        self.attachment_remove_button.clicked.connect(
            self._remove_attachment)
        attach_row.addWidget(self.attachment_remove_button)
        attach_row.addStretch(1)
        right_layout.addLayout(attach_row)
        self.attachment_label.setVisible(False)
        self.attachment_remove_button.setVisible(False)

        row = QHBoxLayout()
        self.chat_input = ChatInput()
        self.chat_input.send_requested.connect(self.send_message)
        self.chat_input.setAcceptDrops(False)
        self.attach_button = QPushButton("Attach image")
        self.attach_button.setToolTip(
            "Attach a picture to your next message (vision "
            "models only)")
        self.attach_button.clicked.connect(self._attach_image)
        self.send_button = PressDipButton("Send")
        self.send_button.setToolTip(
            "Send your message (Enter)")
        self.send_button.clicked.connect(self.send_message)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip(
            "Stop the reply in progress")
        self.stop_button.clicked.connect(self._stop_reply)
        self.stop_button.setEnabled(False)
        self.clear_button = QPushButton("Clear")
        self.clear_button.setToolTip(
            "Clear the chat and forget the conversation so far")
        self.clear_button.clicked.connect(self._clear_chat)
        self.save_chat_button = QPushButton("Save chat")
        self.save_chat_button.setToolTip(
            "Save this chat to a file (Ctrl+S)")
        self.save_chat_button.clicked.connect(self._save_chat)
        row.addWidget(self.chat_input)
        row.addWidget(self.attach_button)
        row.addWidget(self.send_button)
        row.addWidget(self.stop_button)
        row.addWidget(self.clear_button)
        row.addWidget(self.save_chat_button)
        right_layout.addLayout(row)

        layout.addWidget(right, 1)
        self.tabs.addTab(tab, "Chat")
        self._refresh_chat_list()
        self._update_tools_status()


    # ------------------------------------------------------------ status

    def _default_status_message(self):
        """The neutral status line for the current state: the
        empty-folder line when there is nothing to pick, the
        pick-a-model line otherwise."""
        if self._empty_state.isVisible():
            return ("No models yet. Pull one or import one to "
                    "get started.")
        return ("Pick a model and press Load, or pull one from the "
                "Models menu.")

    def _show_status(self, message, timeout_ms=STATUS_TIMEOUT_MS):
        """Show a transient status message (a reply done, a model
        loaded, a folder changed). After the timeout it falls back
        to the neutral line, so stale news never lingers on an
        unrelated screen (L2)."""
        self.statusBar().showMessage(message)
        self._status_timer.stop()
        self._status_timer.setInterval(timeout_ms)
        self._status_timer.start()

    def _show_status_persistent(self, message):
        """Show a status message that stays until something
        replaces it: a load in progress, a stop in progress, the
        empty state. Stops any pending transient timeout, so it
        cannot clobber a live state."""
        self._status_timer.stop()
        self.statusBar().showMessage(message)

    def _on_status_timeout(self):
        self.statusBar().showMessage(self._default_status_message())

    # ------------------------------------------------------------ models

    def _selected_model_path(self):
        if self._selected_path is None:
            return None
        return Path(self._selected_path)

    def _on_card_clicked(self, path):
        self._select_card(path)

    def _on_card_double_clicked(self, path):
        self._select_card(path)
        self.load_selected_model()

    def _select_card(self, path):
        self._selected_path = path
        for key, card in self.model_cards.items():
            card.set_selected(key == path)
        self._update_buttons()

    def _clear_cards(self):
        """Drop every tile in the grid. Cards are deleted, not
        reused, so no stale tile ever survives a refresh."""
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Hide before orphaning: a visible widget with no
                # parent becomes a top-level window until the
                # delete lands, and a new top-level window steals
                # this window's activation (and flashes on screen).
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self.model_cards = {}

    def _show_skeleton(self):
        """Skeleton tiles while the folder scan runs: the shape of
        the list appears first, the models fill it in."""
        self._set_empty_state(False)
        self._clear_cards()
        columns = grid_columns()
        for index in range(3):
            self._cards_layout.addWidget(
                SkeletonCard(), index // columns,
                index % columns, Qt.AlignmentFlag.AlignTop)
        rows = (3 + columns - 1) // columns
        self._cards_layout.addItem(
            QSpacerItem(0, 0, QSizePolicy.Policy.Minimum,
                        QSizePolicy.Policy.Expanding),
            rows, 0, 1, columns)

    def _refresh_model_list(self):
        """Scan the folder off the UI thread. A refresh asked for
        mid-scan runs right after the current scan finishes, never
        on top of it. Skeleton tiles only show when there are no
        cards yet (the first scan): on later refreshes the
        current cards stay visible until the new scan lands, so
        the list never blanks out under the user."""
        if self._scan_worker is not None:
            self._scan_dirty = True
            return
        if not self.model_cards:
            self._show_skeleton()
        self._scan_worker = ScanWorker(
            self.models_folder,
            include_ollama=get_show_ollama_models())
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.start()

    def _on_scan_finished(self, models, skipped, free_bytes):
        worker = self._scan_worker
        self._scan_worker = None
        if worker is not None:
            # The finished signal fires while the thread is still
            # exiting; wait for it, or destroying the QThread
            # aborts the process. See PullWindow._drop_worker.
            worker.wait()
        loaded_path = (str(self.controller.current_path)
                       if self.controller.is_loaded else None)
        # The loaded model goes first as the hero tile; the rest
        # follow in name order.
        ordered = sorted(
            models,
            key=lambda m: (str(m["path"]) != loaded_path,
                           m["name"].lower()))
        # Diff, not rebuild: cards that still show their model
        # stay exactly where they are (same widget), so scroll
        # position and keyboard focus survive every refresh.
        # Cards for models that are gone go away; cards whose
        # model changed (a re-pull, a load) are rebuilt.
        new_paths = {str(model["path"]) for model in ordered}
        for key in list(self.model_cards):
            if key not in new_paths:
                card = self.model_cards.pop(key)
                self._cards_layout.removeWidget(card)
                # Hide before orphaning (see _clear_cards).
                card.hide()
                card.setParent(None)
                card.deleteLater()
        self._card_order = []
        self._hero_card = None
        # Cards built fresh by this scan (not reused): they
        # stagger in below, instead of appearing all at once.
        new_cards = []
        for model in ordered:
            # The fit badge uses this model's real RAM need: the
            # KV cache for the context length the runner will use,
            # from the file's own header, not a fixed guess. A
            # vision model's projector loads with it, so its size
            # counts too.
            headroom = model_headroom_bytes(
                model["path"], auto_context_tokens(free_bytes))
            model["headroom_bytes"] = headroom
            load_size = (model["size"]
                         + projector_size_bytes(model["path"]))
            status = fit_status(load_size, free_bytes, headroom)
            is_loaded = str(model["path"]) == loaded_path
            key = str(model["path"])
            card = self.model_cards.get(key)
            if card is not None and card.matches(model, is_loaded):
                card.set_fit_status(status, model, free_bytes)
            else:
                if card is not None:
                    self.model_cards.pop(key)
                    self._cards_layout.removeWidget(card)
                    # Hide before orphaning (see _clear_cards).
                    card.hide()
                    card.setParent(None)
                    card.deleteLater()
                card = ModelCard(model, status, loaded=is_loaded,
                                 hero=is_loaded,
                                 free_bytes=free_bytes)
                card.clicked.connect(self._on_card_clicked)
                card.double_clicked.connect(
                    self._on_card_double_clicked)
                card.remove_requested.connect(
                    self._on_remove_requested)
                self.model_cards[key] = card
                new_cards.append(card)
            self._card_order.append(key)
            if is_loaded:
                self._hero_card = card
        self._apply_model_filter()
        # The hero tile gets its stats right away, instead of
        # waiting for the next 2-second refresh.
        self._refresh_stats()
        # Keep the selection when the selected model is still there
        # (U28): a refresh must not make the user find and re-click
        # their model. A refresh never creates a selection from
        # nothing (U6), except right after an import or pull, which
        # select the new model.
        wanted = self._select_path
        self._select_path = None
        if wanted is not None and wanted in self.model_cards:
            self._select_card(wanted)
        elif (self._selected_path is not None
                and self._selected_path in self.model_cards):
            self._select_card(self._selected_path)
        else:
            self._selected_path = None
        if skipped:
            lines = ["%s: %s" % (name, reason)
                     for name, reason in skipped]
            self.skipped_label.setText(
                "Could not read: " + "; ".join(lines))
            self.skipped_label.setVisible(True)
        else:
            self.skipped_label.setVisible(False)
        pulsed_path = self._pulse_path
        if pulsed_path is not None:
            card = self.model_cards.get(pulsed_path)
            self._pulse_path = None
            if card is not None:
                self._pulse_card(card)
        # New cards stagger in — except the pulsed one, which has
        # its own entrance already (two opacity effects on one
        # card would fight).
        pulsed = self.model_cards.get(pulsed_path or "")
        self._stagger_cards([card for card in new_cards
                             if card is not pulsed])
        # No models at all: the empty state replaces the grid. It
        # disappears the moment a model appears (the next scan).
        self._set_empty_state(not models)
        # A pending confirmation (remove/import) goes last, so the
        # scan's own status update never swallows it.
        if self._status_after_scan is not None:
            self._show_status(self._status_after_scan)
            self._status_after_scan = None
        self._update_buttons()
        if self._scan_dirty:
            self._scan_dirty = False
            self._refresh_model_list()
        else:
            self._maybe_close_pending()

    def _filter_matches(self, card):
        """True when a card passes the search text and the size or
        source menu: a name substring, case-insensitive, and one
        real bucket (size in 1024-based GB, the same units the
        sizes on the cards use)."""
        text = self.search_box.text().strip().lower()
        if text and text not in card.model_name.lower():
            return False
        choice = self.filter_combo.currentText()
        size = card.model_size
        if choice == "Small (under 1 GB)":
            return size < 1024 ** 3
        if choice == "Medium (1-3 GB)":
            return 1024 ** 3 <= size < 3 * 1024 ** 3
        if choice == "Large (over 3 GB)":
            return size >= 3 * 1024 ** 3
        if choice == "From Ollama":
            return card.source == "ollama"
        if choice == "In my folder":
            return card.source != "ollama"
        return True

    def _apply_model_filter(self):
        """Narrow the visible cards to the search text and the
        size or source menu, and lay the grid out again. The
        cards themselves are never deleted: hidden cards leave
        the grid and are hidden, and come back when the filter
        clears. If the focused card is filtered out, focus moves
        to the search box instead of landing nowhere."""
        if not self.model_cards:
            self._visible_paths = set()
            self._filter_empty_label.setVisible(False)
            return
        visible = {key for key, card in self.model_cards.items()
                   if self._filter_matches(card)}
        self._visible_paths = visible
        widget = QApplication.focusWidget()
        while widget is not None and not isinstance(
                widget, ModelCard):
            widget = widget.parent()
        focus_lost = (widget is not None
                      and widget.path not in visible)
        self._relayout_cards(visible)
        if focus_lost:
            self.search_box.setFocus()
        if visible:
            self._filter_empty_label.setVisible(False)
            self._cards_scroll.setVisible(True)
        else:
            text = self.search_box.text().strip()
            if text:
                self._filter_empty_label.setText(
                    "No models match \"%s\"." % text)
            else:
                self._filter_empty_label.setText(
                    "No models in this filter.")
            self._filter_empty_label.setVisible(True)
            self._cards_scroll.setVisible(False)
        self._update_buttons()

    def _relayout_cards(self, visible_paths):
        """Lay the cards out again in display order, without
        deleting any of them: every widget is taken out of the
        grid and the visible ones are put back, so scroll
        position and keyboard focus survive (the widgets
        themselves never go away). Cards hidden by the filter
        leave the grid and are hidden; skeleton tiles from the
        first scan are dropped for good. The scroll value is
        saved and restored, so a refresh never jumps the list
        back to the top."""
        scroll_bar = self._cards_scroll.verticalScrollBar()
        saved_scroll = scroll_bar.value()
        columns = grid_columns()
        # A lift or drop in flight writes the card's geometry
        # itself: stop every one before the layout moves the
        # cards, or the animation parks its card at a stale spot
        # and the layout never corrects it (task 3).
        for card in self.model_cards.values():
            card.cancel_lift()
        # Equal column stretches (task 17): one card used to span
        # the full row like the hero tile, and two cards took half
        # each. Every column now takes an equal share whether it
        # holds a card or not, so a short row keeps the one-third
        # card width and the hero tile's full width stays
        # meaningful. Columns past the current count (a density
        # change from four back to three) get no stretch, or the
        # empty column would claim a share of the row.
        for col_index in range(max(GRID_COLUMNS,
                                   GRID_COLUMNS_COMPACT)):
            self._cards_layout.setColumnStretch(
                col_index, 1 if col_index < columns else 0)
        # FLIP: remember where each visible card sits now, so the
        # ones that move can be flown to their new spot below.
        old_positions = {}
        if not reduced_motion():
            for key, card in self.model_cards.items():
                if card.isVisible() and key in visible_paths:
                    old_positions[key] = QRect(card.geometry())
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            widget = item.widget()
            if widget is None:
                continue  # the spacer; re-added below
            if widget not in self.model_cards.values():
                # Hide before orphaning (see _clear_cards).
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        row = 0
        col = 0
        for key in self._card_order:
            card = self.model_cards.get(key)
            if card is None:
                continue
            if key not in visible_paths:
                card.setVisible(False)
                continue
            # Add to the grid BEFORE showing it: a card fresh from
            # the scan has no parent yet, and showing a parentless
            # widget makes it a top-level window, which steals this
            # window's activation (and flashes on screen). Once it
            # is in the layout it is a child, and showing it is
            # safe.
            # Top-aligned: the grid centers tiles vertically by
            # default, so a taller card (one with a description)
            # sat above its neighbours and the row looked ragged.
            if card is self._hero_card:
                self._cards_layout.addWidget(
                    card, row, 0, 1, columns,
                    Qt.AlignmentFlag.AlignTop)
                row += 1
                col = 0
            else:
                self._cards_layout.addWidget(
                    card, row, col, Qt.AlignmentFlag.AlignTop)
                col += 1
                if col >= columns:
                    col = 0
                    row += 1
            card.setVisible(True)
        # An expanding spacer pins the tiles to the top: without it
        # the grid stretches a short list of tiles to fill the tab.
        spacer_row = row + 1 if col else row
        self._cards_layout.addItem(
            QSpacerItem(0, 0, QSizePolicy.Policy.Minimum,
                        QSizePolicy.Policy.Expanding),
            spacer_row, 0, 1, columns)
        scroll_bar.setValue(saved_scroll)
        # Settle the new geometry now, so FLIP can measure it:
        # without this the cards still report their old spots
        # until the next layout pass.
        self._cards_layout.activate()
        self._flip_cards(old_positions)

    def _pulse_card(self, card):
        """One quiet 200 ms fade-in on a newly pulled model, opacity
        only (the delight moment). Skipped under reduced motion."""
        if reduced_motion():
            return
        effect = QGraphicsOpacityEffect(card)
        card.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(200)
        anim.setStartValue(0.3)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: card.setGraphicsEffect(None))
        self._pulse_anim = anim
        anim.start()

    def _stagger_cards(self, cards):
        """New cards fade in one after another, 40 ms apart
        (entry 031): the list arrives instead of appearing. Only
        the first twelve stagger; the rest appear at once, so a
        big folder never takes seconds to settle. Skipped under
        reduced motion. One opacity fade per card, 200 ms: cheap
        enough for a 2-core CPU with no GPU."""
        if reduced_motion():
            return
        for index, card in enumerate(cards[:12]):
            effect = QGraphicsOpacityEffect(card)
            effect.setOpacity(0.0)
            card.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity", self)
            anim.setDuration(200)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.finished.connect(
                lambda c=card, a=anim: self._on_stagger_done(c, a))
            self._stagger_anims.append(anim)
            # The delayed start must not touch a dead animation:
            # the window can close before the timer fires, and
            # the animation (a child of the window) dies with it.
            # The lambda holds only the animation, never self, and
            # checks the C++ object is still alive before starting.
            QTimer.singleShot(
                index * 40,
                lambda a=anim: a.start() if isValid(a) else None)

    def _on_stagger_done(self, card, anim):
        if anim in self._stagger_anims:
            self._stagger_anims.remove(anim)
        # The card can be rebuilt (a refresh) while its fade is
        # still running; a deleted card takes no effect.
        if isValid(card):
            card.setGraphicsEffect(None)

    def _flip_cards(self, old_positions):
        """Animate cards that moved in a re-sort from where they
        were to where they are (entry 031, FLIP: First, Last,
        Invert, Play). Each moved card gets a snapshot ghost that
        slides 200 ms from its old spot to its new one; the real
        card is already at the new spot underneath, identical to
        the ghost, so the flight reads as the card moving. At most
        twelve ghosts, opacity and geometry only: cheap enough
        for a 2-core CPU with no GPU. Skipped under reduced
        motion."""
        if reduced_motion():
            return
        moved = 0
        for key, old in old_positions.items():
            card = self.model_cards.get(key)
            if card is None or not card.isVisible():
                continue
            new = card.geometry()
            if old == new:
                continue
            moved += 1
            if moved > 12:
                break
            ghost = QLabel(self._cards_widget)
            ghost.setPixmap(card.grab())
            ghost.setGeometry(old)
            ghost.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            ghost.show()
            ghost.raise_()
            anim = QPropertyAnimation(ghost, b"geometry", self)
            anim.setDuration(200)
            anim.setStartValue(QRect(old))
            anim.setEndValue(QRect(new))
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.finished.connect(
                lambda g=ghost, a=anim: self._on_flip_done(g, a))
            self._flip_anims.append(anim)
            anim.start()

    def _on_flip_done(self, ghost, anim):
        if anim in self._flip_anims:
            self._flip_anims.remove(anim)
        if isValid(ghost):
            ghost.deleteLater()

    def _apply_overscroll_style(self):
        """The overscroll glows' gradients, from the active
        theme's accent: strong at the edge, fading to nothing
        24 px in. Re-run when the theme changes live."""
        accent = current_tokens()["accent"]
        strong = rgba_string(accent, 140)
        clear = rgba_string(accent, 0)
        self._overscroll_top.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 %s, stop:1 %s);" % (strong, clear))
        self._overscroll_bottom.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 %s, stop:1 %s);" % (clear, strong))

    def _position_overscroll(self):
        """Pin the glows to the viewport's top and bottom edges."""
        viewport = self._cards_scroll.viewport()
        width = viewport.width()
        height = viewport.height()
        self._overscroll_top.setGeometry(0, 0, width, 24)
        self._overscroll_bottom.setGeometry(
            0, height - 24, width, 24)

    def _flash_overscroll(self, edge):
        """Show one edge glow, fading out over 300 ms. A second
        push while it is still showing restarts the fade instead
        of stacking a second animation on it."""
        if reduced_motion():
            return
        glow = (self._overscroll_top if edge == "top"
                else self._overscroll_bottom)
        old = self._overscroll_anims.get(edge)
        if old is not None:
            old.stop()
        self._position_overscroll()
        effect = QGraphicsOpacityEffect(glow)
        effect.setOpacity(0.9)
        glow.setGraphicsEffect(effect)
        glow.setVisible(True)
        glow.raise_()
        anim = QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(300)
        anim.setStartValue(0.9)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(
            lambda g=glow, a=anim, e=edge:
            self._on_overscroll_done(g, a, e))
        self._overscroll_anims[edge] = anim
        anim.start()

    def _on_overscroll_done(self, glow, anim, edge):
        if self._overscroll_anims.get(edge) is anim:
            del self._overscroll_anims[edge]
        if isValid(glow):
            glow.setVisible(False)
            glow.setGraphicsEffect(None)

    def eventFilter(self, watched, event):  # noqa: N802 - Qt naming
        # The model list's viewport: keep the overscroll glows
        # pinned to its edges on resize, and flash the edge the
        # user pushes past on a wheel event. Wheel events are
        # never consumed here: normal scrolling still works.
        if watched is self._cards_scroll.viewport():
            if event.type() == QEvent.Type.Resize:
                self._position_overscroll()
            elif event.type() == QEvent.Type.Wheel:
                bar = self._cards_scroll.verticalScrollBar()
                delta = event.angleDelta().y()
                if delta > 0 and bar.value() <= bar.minimum():
                    self._flash_overscroll("top")
                elif delta < 0 and bar.value() >= bar.maximum():
                    self._flash_overscroll("bottom")
        return super().eventFilter(watched, event)

    def _update_buttons(self):
        selected = self._selected_model_path()
        has_selection = selected is not None
        loading = self.load_worker is not None
        importing = self.import_worker is not None
        replying = self.chat_worker is not None
        loaded = self.controller.is_loaded
        # The loaded model is not loadable: clicking Load on it
        # would pay a full reload cycle for nothing.
        selected_is_loaded = (loaded and selected is not None
                              and self.controller.current_path
                              == selected)
        selected_card = (self.model_cards.get(str(selected))
                         if selected is not None else None)
        # An Ollama model belongs to Ollama: Fyrn lists it, but
        # it cannot remove it.
        selected_is_ollama = (selected_card is not None
                              and selected_card.source == "ollama")
        # A card the search or filter hides cannot be loaded or
        # removed: the user cannot see what they would be acting
        # on. The selection itself stays, so clearing the filter
        # brings the buttons back.
        selected_visible = (selected is None
                            or str(selected) in self._visible_paths)
        # One long operation at a time on the Models tab, and
        # nothing that touches the loaded model while a reply
        # streams from it: unloading or deleting a model mid-reply
        # leaves a reply coming from a model that is gone.
        self.load_button.setEnabled(
            has_selection and selected_visible and not loading
            and not importing and not replying
            and not selected_is_loaded)
        self.remove_button.setEnabled(
            has_selection and selected_visible and not loading
            and not importing and not replying
            and not selected_is_ollama)
        # The Models menu actions follow the same state (task 6),
        # with one deliberate difference from the buttons: Load
        # and Remove stay enabled for a hidden or Ollama selection.
        # A disabled menu action kills its shortcut, and the
        # shortcut is how the user is told why nothing happened
        # ("That model is hidden by the search", "belongs to
        # Ollama"). The guards in load_selected_model and
        # remove_selected_model say it in plain words instead.
        self._load_action.setEnabled(
            has_selection and not loading and not importing
            and not replying and not selected_is_loaded)
        self._remove_action.setEnabled(
            has_selection and not loading and not importing
            and not replying)
        self._unload_action.setEnabled(
            loaded and not loading and not replying)
        self._import_action.setEnabled(
            not loading and not importing)
        self._pull_action.setEnabled(True)
        self._refresh_action.setEnabled(True)
        self.unload_button.setEnabled(
            loaded and not loading and not replying)
        self.import_button.setEnabled(not loading and not importing)
        self.send_button.setEnabled(
            loaded and self.chat_worker is None)
        # Attach only works when the loaded model can see: a
        # text-only model has nothing to do with a picture.
        self.attach_button.setEnabled(
            loaded and self.runner.supports_vision)
        self.load_cancel_button.setVisible(loading)
        # A real load cannot be stopped once it starts (the
        # backend never checks for a cancel), so the button says
        # so by being greyed out instead of promising a cancel.
        self.load_cancel_button.setEnabled(
            self.runner.cancellable_load)
        self.import_cancel_button.setVisible(importing)
        self.load_progress.setVisible(loading or importing)

    def load_selected_model(self):
        if (self.load_worker is not None
                or self.chat_worker is not None
                or self.import_worker is not None):
            return
        path = self._selected_model_path()
        if path is None:
            self._show_status(
                "Pick a model from the list first.")
            return
        if str(path) not in self._visible_paths:
            # The card is hidden by the search or filter: the user
            # cannot see what they would be loading (task 5). The
            # button already greys out for this; the shortcut used
            # to bypass it.
            self._show_status(
                "That model is hidden by the search. Clear the "
                "search to load it.")
            return
        try:
            verdict, message = self.controller.check_before_load(path)
        except ModelNotFoundError as exc:
            QMessageBox.warning(self, "Load model", str(exc))
            self._refresh_model_list()
            return
        if verdict == "refuse":
            QMessageBox.warning(self, "Load model", message)
            return
        if verdict == "warn":
            answer = QMessageBox.question(
                self, "Load model",
                message + "\n\nLoad it anyway?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.log.action("load started: %s" % path.name)
        self.load_progress.setValue(0)
        self._set_busy(True)
        card = self.model_cards.get(str(path))
        display_name = card.model_name if card is not None else None
        # The status names the model, never the blob filename
        # (task 11): an Ollama model's file name is a hash.
        self._show_status_persistent(
            "Loading %s..." % (display_name or path.name))
        self.load_worker = LoadWorker(self.controller, path,
                                      display_name=display_name)
        self.load_worker.progress.connect(self._on_load_progress)
        self.load_worker.finished.connect(self._on_load_finished)
        self.load_worker.failed.connect(self._on_load_failed)
        self.load_worker.start()
        self._update_buttons()

    def _on_load_progress(self, pct, message):
        # pct is -1 when the backend has no percent to give: the
        # bar goes indeterminate instead of showing a fake number.
        if pct < 0:
            self.load_progress.setRange(0, 0)
        else:
            self.load_progress.setRange(0, 100)
            self.load_progress.setValue(pct)
        if not self.runner.cancellable_load:
            # Say it plainly while the load runs: the Cancel
            # button is greyed out, and this is why.
            message += " This load cannot be stopped once it starts."
        self._show_status_persistent(message)

    def _on_load_finished(self, result):
        self._drop_load_worker()
        self.load_time_s = result.get("load_time_s", 0.0)
        warning = result.get("warning", "")
        self._set_busy(False)
        self._refresh_stats()
        self._tools_broken = False
        self._update_tools_status()
        self._update_buttons()
        # Rebuild the cards so the loaded model becomes the hero
        # tile with its Loaded pill; otherwise the list still shows
        # the pre-load cards with nothing marked.
        self._refresh_model_list()
        name = self.controller.current_name or "model"
        if warning:
            message = "%s loaded, but RAM is tight." % name
        else:
            message = "%s loaded." % name
        # A vision model without its projector loads as text only:
        # say so in the same breath, so the user knows why the
        # Attach button stays greyed out.
        vision_warning = result.get("vision_warning", "")
        if vision_warning:
            message = "%s %s" % (message, vision_warning)
        self._show_status(message)
        self.log.action("load finished: %s" % name)
        # The empty transcript no longer says "Load a model first":
        # a model IS loaded now, so the hint switches to the true one.
        self.transcript.setPlaceholderText("Type a message below.")
        self.tabs.setCurrentIndex(1)  # Chat tab
        self._maybe_close_pending()

    def _on_load_failed(self, message):
        self._drop_load_worker()
        self._set_busy(False)
        self._update_buttons()
        self._show_status(message)
        self.log.error("load failed: %s", message)
        if not self.controller.is_loaded:
            # The failed load already unloaded the old model, so
            # nothing is loaded now: the hero tile, the Loaded
            # pill, and the stale stats must go, and the chat hint
            # goes back to the true one. (A refused load leaves
            # the current model loaded, so this only runs when
            # there really is nothing loaded.)
            self.load_time_s = None
            self._refresh_model_list()
            self.transcript.setPlaceholderText(
                "Load a model first, then chat here.")
        if message != "Load cancelled.":
            QMessageBox.warning(self, "Load model", message)
        self._maybe_close_pending()

    def _drop_load_worker(self):
        """Forget the load worker, after it has fully stopped
        (see PullWindow._drop_worker for why the wait matters)."""
        worker = self.load_worker
        self.load_worker = None
        if worker is not None:
            worker.wait()

    def _drop_chat_worker(self):
        """Forget the chat worker, after it has fully stopped
        (see PullWindow._drop_worker for why the wait matters)."""
        worker = self.chat_worker
        self.chat_worker = None
        if worker is not None:
            worker.wait()

    def _maybe_close_pending(self):
        """Close this window if a close was asked for while a
        worker was running and every worker has now stopped."""
        if not self._close_pending:
            return
        if (self.load_worker is not None
                or self.chat_worker is not None
                or self.import_worker is not None
                or self._scan_worker is not None
                or self._update_check_worker is not None
                or self._update_download_worker is not None):
            return
        self._close_pending = False
        self.close()

    def _cancel_load(self):
        if self.load_worker is not None:
            self.load_worker.cancel()
            self._show_status_persistent("Stopping the load...")

    def unload_model(self):
        if not self.controller.is_loaded:
            return
        if self.chat_worker is not None:
            # A reply is streaming: Unload waits for it, and says
            # so instead of silently doing nothing (v0.7: a greyed
            # Unload during a reply read as a broken button).
            self._show_status("Wait for the reply to finish first.")
            return
        name = self.controller.current_name or "model"
        self.controller.unload()
        self.load_time_s = None
        self._refresh_stats()
        self._update_tools_status()
        self._update_buttons()
        # Rebuild the cards so the hero tile and Loaded pill go away.
        self._refresh_model_list()
        self._show_status("%s unloaded." % name)
        self.log.action("unload: %s" % name)
        # Back to the true hint: nothing is loaded, so chat needs a
        # model first.
        self.transcript.setPlaceholderText(
            "Load a model first, then chat here.")

    def _on_remove_requested(self, path):
        self._select_card(path)
        self.remove_selected_model()

    def remove_selected_model(self):
        path = self._selected_model_path()
        if path is None:
            self._show_status(
                "Pick a model from the list first.")
            return
        if str(path) not in self._visible_paths:
            # The card is hidden by the search or filter: the user
            # cannot see what they would be removing.
            self._show_status(
                "That model is hidden by the search. Clear the "
                "search to remove it.")
            return
        self._remove_model(path)

    def _remove_model(self, path):
        """Move a model file to the desktop trash, after a confirm
        that defaults to No (U31). A loaded model is unloaded
        first, never deleted out from under the engine. The trash
        keeps the file, so a mistaken remove is undoable."""
        path = Path(path)
        if self.chat_worker is not None:
            self._show_status("Wait for the reply to finish first.")
            return
        card = self.model_cards.get(str(path))
        if card is not None and card.source == "ollama":
            QMessageBox.information(
                self, "Remove model",
                "%s belongs to Ollama. Fyrn lists it, but Ollama "
                "owns the file: remove it with Ollama, not here."
                % card.model_name)
            return
        if not path.is_file():
            self._show_status("That model file is gone.")
            self._refresh_model_list()
            return
        loaded = (self.controller.is_loaded
                  and self.controller.current_path == path)
        message = (
            "Remove %s (%s) from your models folder?\n\n"
            "This moves the file to the trash. You can get it "
            "back from the trash."
            % (path.name, format_size(path.stat().st_size)))
        if loaded:
            message += ("\n\nIt is loaded right now, so it will be "
                        "unloaded first.")
        answer = QMessageBox.question(
            self, "Remove model", message,
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        if loaded:
            self.unload_model()
        try:
            move_to_trash(path)
        except ValueError as exc:
            # The file is still there: a failed move never deletes.
            QMessageBox.warning(self, "Remove model", str(exc))
            self._refresh_model_list()
            return
        self.log.action("removed to trash: %s" % path.name)
        self._selected_path = None
        self._status_after_scan = (
            "%s moved to the trash." % path.name)
        self._refresh_model_list()

    def import_model(self):
        """Copy a .gguf file into the models folder, on a worker
        thread: a multi-GB copy runs for most of a minute, and
        the window must stay alive, with progress and a cancel,
        while it runs."""
        if (self.import_worker is not None
                or self.load_worker is not None):
            return
        path, _selected = QFileDialog.getOpenFileName(
            self, "Pick a GGUF model file", str(Path.home()),
            "GGUF models (*.gguf)")
        if not path:
            return
        self._start_import(path)

    def _start_import(self, path):
        """Start importing one .gguf file: the shared path for
        the Import dialog and for files dropped on the window."""
        self.log.action("import started: %s" % Path(path).name)
        self._set_busy(True)
        self._show_status_persistent(
            "Importing %s..." % Path(path).name)
        self.import_worker = ImportWorker(path, self.models_folder)
        self.import_worker.progress.connect(self._on_import_progress)
        self.import_worker.finished.connect(self._on_import_finished)
        self.import_worker.failed.connect(self._on_import_failed)
        self.import_worker.cancelled.connect(self._on_import_cancelled)
        self.import_worker.start()
        self._update_buttons()

    # ------------------------------------------------ drag and drop

    def dragEnterEvent(self, event):  # noqa: N802 - Qt naming
        """A .gguf file dragged over the window is an import
        waiting to happen; anything else is not ours."""
        if self._dropped_gguf_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):  # noqa: N802 - Qt naming
        """Drop .gguf files to import them, one after another,
        through the same worker as Import."""
        paths = self._dropped_gguf_paths(event.mimeData())
        if not paths:
            super().dropEvent(event)
            return
        event.acceptProposedAction()
        self._import_dropped(paths)

    def _dropped_gguf_paths(self, mime_data):
        """The .gguf files in a drag: local file URLs only, and
        only files that exist."""
        paths = []
        for url in mime_data.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.suffix.lower() == ".gguf" and path.is_file():
                paths.append(path)
        return paths

    def _import_dropped(self, paths):
        """Import dropped files in order: the first starts now
        (or queues behind a running import), the rest wait."""
        if self.load_worker is not None:
            self._show_status(
                "Wait for the load to finish, then drop the file "
                "again.")
            return
        if self.import_worker is not None:
            self._import_pending.extend(paths)
            return
        self._import_pending.extend(paths)
        self._start_import(self._import_pending.pop(0))

    def _drain_import_pending(self):
        """Start the next dropped import, if one is waiting."""
        if self._import_pending and self.import_worker is None:
            self._start_import(self._import_pending.pop(0))

    def _on_import_progress(self, copied, total):
        if total and total > 0:
            self.load_progress.setRange(0, 100)
            self.load_progress.setValue(int(copied * 100 / total))
            self._show_status_persistent(
                "Importing: %s of %s"
                % (format_size(copied), format_size(total)))
        else:
            self.load_progress.setRange(0, 0)

    def _on_import_finished(self, path):
        self._drop_import_worker()
        self._set_busy(False)
        self._update_buttons()
        dest = Path(path)
        self.log.action("imported: %s" % dest.name)
        self._status_after_scan = (
            "%s is now in your models folder." % dest.name)
        self._select_path = str(dest)
        self._refresh_model_list()
        self._drain_import_pending()
        self._maybe_close_pending()

    def _on_import_failed(self, message):
        self._drop_import_worker()
        self._set_busy(False)
        self._update_buttons()
        self._show_status(message)
        self.log.error("import failed: %s", message)
        # A failure stops the dropped queue too: the files still
        # waiting are left alone, so the user can see the error
        # and decide, instead of a modal appearing over the next
        # import already running.
        if self._import_pending:
            count = len(self._import_pending)
            self._import_pending = []
            message = ("%s\n\n%d more dropped file(s) were not "
                       "imported." % (message, count))
        QMessageBox.warning(self, "Import a GGUF file", message)
        self._maybe_close_pending()

    def _on_import_cancelled(self):
        self._drop_import_worker()
        self._set_busy(False)
        self._update_buttons()
        self._show_status("Import stopped. Nothing was copied.")
        # A cancel stops the whole drop: the files still waiting
        # are left alone.
        self._import_pending = []
        self._maybe_close_pending()

    def _drop_import_worker(self):
        """Forget the import worker, after it has fully stopped
        (see PullWindow._drop_worker for why the wait matters)."""
        worker = self.import_worker
        self.import_worker = None
        if worker is not None:
            worker.wait()

    def _cancel_import(self):
        if self.import_worker is not None:
            self.import_worker.cancel()
            self._show_status_persistent("Stopping the import...")

    # ------------------------------------------------------------ chat

    def _attach_image(self):
        """Pick a picture to send with the next message. Only a
        vision model can see it, so the button is greyed out
        otherwise. PNG and JPEG only: those are the formats the
        engine's image loader knows."""
        path, _selected = QFileDialog.getOpenFileName(
            self, "Pick a picture", str(Path.home()),
            "Pictures (*.png *.jpg *.jpeg)")
        if not path:
            return
        self._attached_image = Path(path)
        self.attachment_label.setText(
            "Attached: %s" % self._attached_image.name)
        self.attachment_label.setVisible(True)
        self.attachment_remove_button.setVisible(True)

    def _remove_attachment(self):
        """Drop the attached picture: the next message goes
        without it."""
        self._attached_image = None
        self.attachment_label.setText("")
        self.attachment_label.setVisible(False)
        self.attachment_remove_button.setVisible(False)

    # ------------------------------------------------------------ saved chats

    def _save_current_chat(self):
        """Save the open chat to its file (v0.8): the display
        messages, the instructions, the title. A chat with nothing
        in it and no instructions is not worth a file."""
        instructions = self.instructions_edit.toPlainText()
        if (not self._display_messages
                and not instructions.strip()
                and self._current_chat_id is None
                and self._current_title_auto):
            return
        title = self._current_title
        if self._current_title_auto:
            first_user = next(
                (m for m in self._display_messages
                 if m["role"] == "user"), None)
            if first_user is not None:
                title = auto_title(
                    first_user["content"].split("\n")[0])
        record = {
            "id": self._current_chat_id or "",
            "title": title,
            "title_auto": self._current_title_auto,
            "created": self._current_created or 0,
            "instructions": instructions,
            "model": self.controller.current_name or "",
            "messages": list(self._display_messages),
        }
        try:
            saved = self._chat_store.save(record)
        except (OSError, ValueError):
            return
        self._current_chat_id = saved["id"]
        self._current_title = saved["title"]
        self._current_created = saved["created"]
        self._refresh_chat_list()

    def _refresh_chat_list(self):
        """Rebuild the sidebar list from the chats on disk,
        newest first, with the open chat selected."""
        if not hasattr(self, "chat_list"):
            return
        self._refreshing_chats = True
        try:
            self.chat_list.clear()
            selected = -1
            for index, summary in enumerate(
                    self._chat_store.list_chats()):
                item = QListWidgetItem(summary["title"])
                item.setData(Qt.ItemDataRole.UserRole,
                             summary["id"])
                item.setToolTip(summary["title"])
                self.chat_list.addItem(item)
                if summary["id"] == self._current_chat_id:
                    selected = index
            self.chat_list.setCurrentRow(selected)
        finally:
            self._refreshing_chats = False
        has_chat = self._current_chat_id is not None
        self.rename_chat_button.setEnabled(has_chat)
        self.delete_chat_button.setEnabled(has_chat)

    def _on_chat_list_row(self, row):
        if getattr(self, "_refreshing_chats", False) or row < 0:
            return
        item = self.chat_list.item(row)
        if item is None:
            return
        chat_id = item.data(Qt.ItemDataRole.UserRole)
        if not chat_id or chat_id == self._current_chat_id:
            return
        self._request_chat_action(("open", chat_id))

    def _request_chat_action(self, action):
        """Switch chats, now or after the streaming reply stops:
        a switch never silently loses a reply in progress — it
        follows the same Stop path the Stop button uses."""
        if self.chat_worker is not None:
            self._pending_chat_action = action
            self._stop_reply()
            self._show_status(
                "Stopping the reply, then switching chats...")
            return
        self._run_chat_action(action)

    def _run_pending_chat_action(self):
        action = self._pending_chat_action
        self._pending_chat_action = None
        if action is not None:
            self._run_chat_action(action)

    def _run_chat_action(self, action):
        if action[0] == "open":
            self._open_chat(action[1])
        elif action[0] == "new":
            self._start_new_chat()

    def new_chat(self):
        """Start a fresh chat (the open one is already saved)."""
        if self._current_chat_id is None \
                and not self._display_messages \
                and not self.instructions_edit.toPlainText():
            self._show_status("This is already a new chat.")
            return
        self._request_chat_action(("new",))

    def _start_new_chat(self):
        self._save_current_chat()
        self._reset_chat_state()
        self._refresh_chat_list()
        self._show_status("New chat started.")

    def _reset_chat_state(self):
        """Blank chat state: no open chat, empty transcript,
        empty instructions, no history."""
        self._current_chat_id = None
        self._current_title = "New chat"
        self._current_title_auto = True
        self._current_created = None
        self._display_messages = []
        self._chat_history = []
        self._current_reply = ""
        self._last_stats = None
        self.transcript.clear()
        blocker = QSignalBlocker(self.instructions_edit)
        self.instructions_edit.clear()
        del blocker
        self._refresh_stats()

    def _open_chat(self, chat_id):
        self._save_current_chat()
        record = self._chat_store.load(chat_id)
        if record is None:
            self._show_status("That chat could not be opened.")
            self._refresh_chat_list()
            return
        self._reset_chat_state()
        self._current_chat_id = record["id"]
        self._current_title = record["title"]
        self._current_title_auto = record["title_auto"]
        self._current_created = record["created"] or None
        self._display_messages = list(record["messages"])
        self._chat_history = history_from_messages(
            record["messages"])
        blocker = QSignalBlocker(self.instructions_edit)
        self.instructions_edit.setPlainText(record["instructions"])
        del blocker
        for message in record["messages"]:
            if message["role"] == "user":
                self.transcript.append(
                    "You: %s" % message["content"])
            elif message["role"] == "assistant":
                name = message.get("name") or "Model"
                self.transcript.append(
                    "%s: %s" % (name, message["content"]))
                self.transcript.append(TURN_DIVIDER)
                self.transcript.append("")
            else:
                self.transcript.append(message["content"])
        self._refresh_chat_list()
        self._show_status("Chat opened: %s" % record["title"])

    def rename_chat(self):
        if self.chat_worker is not None:
            self._show_status(
                "Wait for the reply to finish first.")
            return
        if self._current_chat_id is None:
            self._show_status("There is no chat open to rename.")
            return
        title, ok = QInputDialog.getText(
            self, "Rename chat", "New name:",
            text=self._current_title)
        title = title.strip()
        if not ok or not title:
            return
        self._current_title = title
        self._current_title_auto = False
        self._save_current_chat()
        self._show_status("Chat renamed to %s." % title)

    def delete_chat(self):
        if self.chat_worker is not None:
            self._show_status(
                "Wait for the reply to finish first.")
            return
        if self._current_chat_id is None:
            self._show_status("There is no chat open to delete.")
            return
        box = QMessageBox(self)
        box.setWindowTitle("Delete chat")
        box.setText(
            "Delete the chat \"%s\"? This cannot be undone."
            % self._current_title)
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        self._chat_store.delete(self._current_chat_id)
        self._reset_chat_state()
        self._refresh_chat_list()
        self._show_status("Chat deleted.")

    def _on_instructions_changed(self):
        # Save a moment after typing stops; the text is read live
        # at send time either way, so a change mid-conversation
        # applies from the next message.
        self._instructions_timer.start()

    # ------------------------------------------------------------ tools

    def _update_tools_status(self):
        """The one plain line about tools for the loaded model:
        on, with what; or off, because this model cannot."""
        if not hasattr(self, "tools_status_label"):
            return
        if not self.controller.is_loaded:
            self.tools_status_label.setText(
                "Tools: load a model that can use tools to turn "
                "them on for a chat.")
        elif getattr(self.runner, "supports_tools", False) \
                and not self._tools_broken:
            self.tools_status_label.setText(
                "Tools are on for this chat: files in the tools "
                "folder, the web through Forge Browser, commands "
                "(each one asks you first), the time, and a "
                "calculator.")
        else:
            self.tools_status_label.setText(
                "This model cannot use tools, so this chat is "
                "plain chat: no tools are offered to it.")

    def _tool_executor(self):
        if self._executor is None:
            self._executor = ToolExecutor(
                get_tools_folder(),
                forge_override=get_forge_browser_path() or None,
                approve_command=None,
                shots_dir=self._home / "browser-shots")
        return self._executor

    def _pick_tools_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Pick the tools folder", get_tools_folder())
        if folder:
            set_tools_folder(folder)
            self._executor = None
            self._show_status("Tools folder is now %s." % folder)

    def show_browser_window(self):
        """Open the browser view (and return it): what the web
        tools are doing, live."""
        if self.browser_window is None:
            window = BrowserWindow(self)
            window.open_requested.connect(
                self._open_in_forge_browser)
            self.browser_window = window
        self.browser_window.show()
        self.browser_window.raise_()
        return self.browser_window

    def _open_in_forge_browser(self, url):
        if not url:
            return
        from .tools import ForgeBrowserClient, ToolFailure
        client = ForgeBrowserClient(
            override=get_forge_browser_path() or None)
        try:
            client.open_in_browser(url)
        except ToolFailure as exc:
            self._show_status(str(exc))

    def _on_browser_event(self, kind, data):
        """Web tool updates for the browser view (UI thread, via
        the worker's signal)."""
        window = self.browser_window
        if window is None:
            return
        payload = data or {}
        kind_of = payload.get("kind")
        if kind_of == "busy":
            window.show_busy(payload.get("text", "Working..."))
        elif kind_of == "search":
            window.show_search(payload.get("query", ""),
                               payload.get("text", ""))
        elif kind_of == "page":
            window.show_page(payload.get("url", ""),
                             payload.get("title", ""),
                             payload.get("markdown", ""),
                             payload.get("screenshot"))

    def _tool_arg_summary(self, name, arguments):
        """The short 'what it is doing' text for a tool call."""
        arguments = arguments or {}
        if name in ("read_file", "write_file"):
            return str(arguments.get("path", ""))
        if name == "list_folder":
            return str(arguments.get("path") or ".")
        if name == "calculate":
            return str(arguments.get("expression", ""))
        if name == "web_search":
            return str(arguments.get("query", ""))
        if name == "web_read":
            return str(arguments.get("url", ""))
        if name == "run_command":
            return str(arguments.get("command", ""))
        return ""

    def _on_tool_event(self, kind, data):
        """A tool call or its result, live in the transcript
        (v0.8): the user always sees what the model did."""
        data = data or {}
        name = data.get("name", "")
        if kind == "call":
            detail = self._tool_arg_summary(
                name, data.get("arguments"))
            line = "Tool: %s" % name
            if detail:
                line = "%s - %s" % (line, detail)
            self.transcript.append(line)
            self._display_messages.append(
                {"role": "tool", "name": name, "content": line})
            if name in ("web_search", "web_read"):
                window = self.show_browser_window()
                window.show_busy("Working on: %s" % detail)
        elif kind == "result":
            line = "Result: %s" % data.get("summary", "")
            self.transcript.append(line)
            self._display_messages.append(
                {"role": "tool", "name": name, "content": line})

    def _on_approval_requested(self, command, cwd):
        """The run_command gate, on the UI thread: the exact
        command in a modal; only Run runs it."""
        worker = self.chat_worker
        dialog = CommandApprovalDialog(command, cwd, self)
        approved = dialog.exec() == QDialog.DialogCode.Accepted
        if worker is not None:
            worker.resolve_approval(approved)

    def _on_tools_fallback(self):
        self._tools_broken = True
        self._update_tools_status()
        self._show_status(
            "This model cannot use tools after all, so that reply "
            "was written without them.")

    # ------------------------------------------------------------ chat flow

    def send_message(self):
        if self.chat_worker is not None:
            return
        if not self.controller.is_loaded:
            self._show_status(
                "Load a model from the Models tab first.")
            return
        text = self.chat_input.text().strip()
        if not text:
            return
        self.chat_input.clear()
        self.transcript.append("You: %s" % text)
        # A picture goes with the message (a vision model only):
        # the transcript notes it, so the turn reads true later.
        images = []
        user_content = text
        if self._attached_image is not None:
            images = [str(self._attached_image)]
            self.transcript.append(
                "Image: %s" % self._attached_image.name)
            user_content = "%s\nImage: %s" % (
                text, self._attached_image.name)
            self._remove_attachment()
        # The reply carries the model name that wrote it (captured
        # now, so a model switch mid-session never mislabels it).
        name = self.controller.current_name or "Model"
        self._reply_name = name
        # Tools are offered when the loaded model can use them —
        # and not on a picture turn: the vision path stays exactly
        # the plain one.
        use_tools = (
            bool(getattr(self.runner, "supports_tools", False))
            and not self._tools_broken
            and not images)
        if use_tools:
            # Tool lines may come first; the "Name: " prefix is
            # written when the first words of the answer arrive.
            self._reply_prefix_done = False
        else:
            self.transcript.append("%s: " % name)
            self._reply_prefix_done = True
        self._reply_in_progress = True
        self._current_reply = ""
        self._display_messages.append(
            {"role": "user", "content": user_content})
        # The chat saves itself as the user talks (v0.8).
        self._save_current_chat()
        self.log.action("chat sent (%d chars)" % len(text))
        self._set_busy(True)
        # The earlier turns go with the new message, trimmed to
        # the budget, so the model answers in context instead of
        # forgetting every turn.
        history = trim_history(self._chat_history)
        self._chat_history.append({"role": "user", "content": text})
        instructions = (
            self.instructions_edit.toPlainText().strip() or None)
        self.chat_worker = ChatWorker(
            self.runner, text,
            history=history,
            images=images,
            instructions=instructions,
            executor=self._tool_executor() if use_tools else None)
        self.chat_worker.token.connect(self._on_chat_token)
        self.chat_worker.finished.connect(self._on_chat_finished)
        self.chat_worker.failed.connect(self._on_chat_failed)
        self.chat_worker.tool_event.connect(self._on_tool_event)
        self.chat_worker.web_event.connect(self._on_browser_event)
        self.chat_worker.tools_fallback.connect(
            self._on_tools_fallback)
        self.chat_worker.approval_requested.connect(
            self._on_approval_requested)
        self.chat_worker.start()
        self._update_buttons()
        self.stop_button.setEnabled(True)
        # The stats carry the reply-in-progress line now (v0.7):
        # refresh at once instead of waiting for the 2 s timer.
        self._refresh_stats()

    def _on_chat_token(self, piece):
        if not self._reply_prefix_done:
            self._reply_prefix_done = True
            self.transcript.append("%s: " % self._reply_name)
        self._current_reply += piece
        cursor = self.transcript.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(piece)
        self.transcript.setTextCursor(cursor)
        self.transcript.ensureCursorVisible()

    def _end_turn(self, note=None):
        """Close a chat turn: an optional note, a divider line, and
        a blank line, so turns never run together (L11)."""
        if note:
            self.transcript.append("\n" + note)
        self.transcript.append(TURN_DIVIDER)
        self.transcript.append("")

    def _on_chat_finished(self, stats):
        self._drop_chat_worker()
        self._reply_in_progress = False
        self._set_busy(False)
        self.stop_button.setEnabled(False)
        self._update_buttons()
        if stats.get("stopped"):
            self._end_turn("(Reply stopped.)")
            self._show_status("Reply stopped.")
            self._finish_turn()
            self._run_pending_chat_action()
            return
        self._end_turn()
        self._last_stats = stats
        self._refresh_stats()
        self._show_status("Reply done.")
        self._finish_turn()
        self._run_pending_chat_action()

    def _finish_turn(self):
        """Close a chat turn in the history: the reply (whole or
        partial) joins the conversation, trimmed to the budget,
        so the next message carries it. The saved chat follows."""
        if self._current_reply:
            self._chat_history.append(
                {"role": "assistant",
                 "content": self._current_reply})
            self._chat_history = trim_history(self._chat_history)
            self._display_messages.append(
                {"role": "assistant", "name": self._reply_name,
                 "content": self._current_reply})
        self._current_reply = ""
        self._save_current_chat()
        self._maybe_close_pending()

    def _on_chat_failed(self, message):
        self._drop_chat_worker()
        self._reply_in_progress = False
        self._set_busy(False)
        self.stop_button.setEnabled(False)
        self._update_buttons()
        # Clear the reply-in-progress line at once (v0.7), the
        # way the finished path does.
        self._refresh_stats()
        self._end_turn("(%s)" % message)
        self._show_status(message)
        self.log.error("chat failed: %s", message)
        # A failed reply is not a turn: the model said nothing to
        # remember, so only the partial text is dropped. The user
        # message and any tool lines stay in the saved chat.
        self._current_reply = ""
        self._save_current_chat()
        self._run_pending_chat_action()
        self._maybe_close_pending()

    def _stop_reply(self):
        if self.chat_worker is not None:
            self.chat_worker.cancel()
            self._show_status_persistent("Stopping the reply...")

    def _clear_chat(self):
        """Clear the transcript. Nothing is saved, so nothing is
        lost: no confirm is needed (L5). The stats and the model's
        memory of the conversation go with it: numbers and history
        describing a conversation that no longer exists would lie.
        The saved chat is cleared with it (v0.8): Clear means the
        chat is emptied, on screen and on disk."""
        self.transcript.clear()
        self._last_stats = None
        self._chat_history = []
        self._display_messages = []
        if self._current_chat_id is not None:
            self._save_current_chat()
        self._refresh_stats()

    def _save_chat(self):
        """Save the chat to a file the user picks (task 13): the
        minimum scope for keeping a conversation. No history
        across sessions: the file is the record, and Clear still
        wipes the transcript with no confirm."""
        text = self.transcript.toPlainText().strip()
        if not text:
            self._show_status("There is no chat to save yet.")
            return
        default = str(Path.home() / "fyrn-chat.txt")
        path, _selected = QFileDialog.getSaveFileName(
            self, "Save chat", default, "Text files (*.txt)")
        if not path:
            return
        name = self.controller.current_name
        content = format_chat_export(
            self.transcript.toPlainText(), model_name=name)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            QMessageBox.warning(
                self, "Save chat",
                "The chat could not be saved (%s)." % exc)
            return
        self.log.action("chat saved: %s" % Path(path).name)
        self._show_status("Chat saved to %s." % path)

    def _refresh_stats(self):
        total, free = read_ram_bytes()
        parts = []
        if total is not None and free is not None:
            parts.append("RAM: %s used, %s free"
                         % (format_size(total - free),
                            format_size(free)))
        else:
            parts.append("RAM: unknown")
        if self.controller.is_loaded:
            parts.append("Loaded: %s" % self.controller.current_name)
            loaded_parts = []
            if self.load_time_s is not None:
                loaded_parts.append("Load time: %.1f s"
                                    % self.load_time_s)
            stats = getattr(self, "_last_stats", None)
            if stats:
                loaded_parts.append(
                    "Speed: %.1f tokens per second"
                    % stats.get("tokens_per_sec", 0.0))
                loaded_parts.append("First token: %.2f s"
                                    % stats.get("ttft_s", 0.0))
                loaded_parts.append("Reply: %d tokens"
                                    % stats.get("tokens", 0))
            if self.chat_worker is not None:
                # A reply in progress, visible on the Models tab
                # too (the hero tile carries these stats): a
                # greyed Unload during a reply reads as broken
                # unless something says a reply is running (v0.7).
                loaded_parts.append("Reply in progress…")
            parts.extend(loaded_parts)
            # The hero tile shows the same live stats as the Chat
            # tab, so the loaded model's numbers are visible
            # without switching tabs.
            if self._hero_card is not None:
                self._hero_card.set_stats("  |  ".join(loaded_parts))
        else:
            parts.append("No model loaded.")
        self.stats_label.setText("  |  ".join(parts))
        # The tray tooltip keeps the same facts current.
        if self.tray is not None:
            self.tray.setToolTip(self._tray_tooltip())

    # ------------------------------------------------------------ tray

    def _setup_tray(self):
        """The system tray icon: the loaded model and RAM at a
        glance, one click to raise the window (L6). One fact, one
        action, not a mini-app. When no tray is available (a
        desktop without one, or a headless run) there is simply
        no tray: no crash, no error."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(self)
        if ICON_PATH.is_file():
            tray.setIcon(QIcon(str(ICON_PATH)))
        tray.setToolTip(self._tray_tooltip())
        menu = QMenu(self)
        show_action = menu.addAction("Show Fyrn")
        show_action.triggered.connect(self.bring_to_front)
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self.close)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self.tray = tray

    def _tray_tooltip(self):
        """The tray tooltip: the loaded model and free RAM in one
        line."""
        _total, free = read_ram_bytes()
        if free is not None:
            ram = "RAM: %s free" % format_size(free)
        else:
            ram = "RAM: unknown"
        if self.controller.is_loaded:
            name = self.controller.current_name or "model"
            if len(name) > 40:
                name = name[:37] + "..."
            return "Fyrn: %s | %s" % (name, ram)
        return "Fyrn: no model loaded | %s" % ram

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.bring_to_front()

    # ------------------------------------------------------------ windows

    def _set_busy(self, busy):
        self.busy_dot.setVisible(busy)

    def _is_model_loaded(self, path):
        """True when this file is the model loaded right now:
        the Pull window asks before pulling over it."""
        return (self.controller.is_loaded
                and self.controller.current_path is not None
                and str(self.controller.current_path) == str(path))

    def show_pull_window(self):
        if self.pull_window is None:
            self.pull_window = PullWindow(
                self.models_folder, log=self.log,
                is_loaded=self._is_model_loaded)
            self.pull_window.setAttribute(
                Qt.WidgetAttribute.WA_DeleteOnClose)
            self.pull_window.destroyed.connect(self._pull_closed)
            self.pull_window.model_pulled.connect(
                self._on_model_pulled)
        self.pull_window.show()
        self.pull_window.raise_()
        self.pull_window.activateWindow()

    def _pull_closed(self, _obj=None):
        self.pull_window = None

    def _on_model_pulled(self, path):
        self._pulse_path = str(path)
        self._select_path = str(path)
        self._refresh_model_list()

    def show_settings(self):
        if self.settings_window is None:
            self.settings_window = SettingsWindow(
                self.models_folder, parent=self)
            self.settings_window.destroyed.connect(
                self._settings_closed)
            self.settings_window.folder_changed.connect(
                self._on_folder_changed)
            self.settings_window.settings_saved.connect(
                self._on_settings_saved)
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def _settings_closed(self, _obj=None):
        self.settings_window = None

    def _on_folder_changed(self, folder):
        self.models_folder = ensure_models_folder(Path(folder))
        self.log.set_setting("models_folder", str(self.models_folder))
        self.log.action("models folder changed: %s" % folder)
        self._refresh_model_list()
        # The folder message goes out now; the settings-saved
        # message that follows in the same save must not clobber
        # it (task 10).
        self._folder_message_shown = True
        self._show_status(
            "Models folder is now %s." % folder)

    def _on_settings_saved(self):
        """Settings were saved: apply the theme and the density
        live, so the window changes without a restart. A
        settings-only save says so plainly; when the folder
        changed in the same save, the folder message stands and
        this one stays quiet (task 10)."""
        self._apply_theme()
        self._apply_density()
        # The tool settings may have changed: the next tool turn
        # builds a fresh executor from the saved settings.
        self._executor = None
        if getattr(self, "_folder_message_shown", False):
            self._folder_message_shown = False
        else:
            self._show_status("Settings saved.")

    # ------------------------------------------------------------ updates

    def check_for_updates(self, manual=False):
        """Check GitHub for a newer Fyrn release (v0.6). The
        startup check (manual=False) is silent: up to date, or
        any failure, means nothing happens at all. A manual
        check (Help menu, command palette) always reports. A
        newer release pops up and asks either way; nothing
        downloads until the user taps to update."""
        if self._update_check_worker is not None:
            if manual:
                self._show_status("Already checking for updates.")
            return
        self._update_check_manual = manual
        self._update_check_worker = UpdateCheckWorker(__version__)
        self._update_check_worker.finished.connect(
            self._on_update_check_result)
        self._update_check_worker.start()

    def _on_update_check_result(self, result):
        worker = self._update_check_worker
        self._update_check_worker = None
        if worker is not None:
            worker.wait()
        manual = self._update_check_manual
        state = result.get("state")
        if state == "newer":
            answer = QMessageBox.question(
                self, "Update available",
                "Fyrn %s is out. You have %s.\n\n"
                "Download and install it now?"
                % (result["latest"], __version__),
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self._start_update_download(result)
            return
        if not manual:
            return  # silent when up to date or unknown
        if state == "current":
            QMessageBox.information(
                self, "Check for updates",
                "Fyrn is up to date (%s)." % __version__)
        else:
            QMessageBox.information(
                self, "Check for updates",
                "Could not check for updates: %s"
                % result.get("reason", "unknown reason"))

    def _start_update_download(self, release):
        if self._update_download_worker is not None:
            return
        # The check result names the release "latest"; the worker
        # stages it under "version". Normalize here, the one place
        # a check result becomes a download.
        release = dict(release)
        release.setdefault("version", release.get("latest"))
        self._update_release = release
        self._update_progress = QProgressDialog(
            "Downloading Fyrn %s..." % release["latest"],
            "Cancel", 0, 100, self)
        self._update_progress.setWindowTitle("Updating Fyrn")
        self._update_progress.setMinimumDuration(0)
        self._update_progress.canceled.connect(
            self._cancel_update_download)
        worker = UpdateDownloadWorker(release, self._home)
        self._update_download_worker = worker
        worker.progress.connect(self._on_update_download_progress)
        worker.finished.connect(self._on_update_downloaded)
        worker.failed.connect(self._on_update_download_failed)
        worker.cancelled.connect(self._on_update_download_cancelled)
        worker.start()
        self._update_progress.show()

    def _on_update_download_progress(self, downloaded, total):
        dialog = self._update_progress
        if dialog is None:
            return
        if total is None or total <= 0:
            dialog.setRange(0, 0)
            return
        dialog.setRange(0, 100)
        dialog.setValue(int(downloaded * 100 / total))

    def _cancel_update_download(self):
        if self._update_download_worker is not None:
            self._update_download_worker.cancel()

    def _drop_update_download_worker(self):
        """Forget the update download worker, after it has fully
        stopped (see PullWindow._drop_worker for why the wait
        matters)."""
        worker = self._update_download_worker
        self._update_download_worker = None
        if worker is not None:
            worker.wait()

    def _close_update_progress(self):
        if self._update_progress is not None:
            self._update_progress.close()
            self._update_progress = None

    def _on_update_downloaded(self, result):
        self._drop_update_download_worker()
        self._close_update_progress()
        version = result.get("version", "")
        answer = QMessageBox.question(
            self, "Update ready",
            "Fyrn %s is downloaded and checked. Restart now to "
            "complete the update?"
            % version,
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            self._show_status(
                "Fyrn %s will install when Fyrn restarts." % version)
            return
        self._apply_update_and_restart(version)

    def _on_update_download_failed(self, message):
        self._drop_update_download_worker()
        self._close_update_progress()
        self._show_status(message)
        QMessageBox.warning(self, "Update", message)

    def _on_update_download_cancelled(self):
        self._drop_update_download_worker()
        self._close_update_progress()
        self._show_status("Update download cancelled.")

    def _apply_update_and_restart(self, version):
        """Install the staged update and restart on it (v0.6).
        A model operation in flight means the update waits for
        the next restart instead: installing over the running
        program mid-load would pull the files out from under it.
        A failed install leaves the old version in place and
        says so; the restart only happens once the new files
        are in."""
        if (self.load_worker is not None
                or self.chat_worker is not None
                or self.import_worker is not None):
            self._show_status(
                "Fyrn %s is ready and will install when Fyrn "
                "restarts." % version)
            return
        from .updates import apply_pending_update, restart_after_exit
        ok, message = apply_pending_update(self._home)
        if not ok:
            QMessageBox.warning(self, "Update", message)
            return
        save_window_geometry(get_settings(), "window", self)
        restart_after_exit([sys.executable, "-m", "fyrn"])
        QApplication.instance().quit()

    def _apply_theme(self):
        """Re-skin the window from the active theme's tokens:
        the app stylesheet, every card, the busy dot, and the
        empty-state panel. The Pull window, when open, re-skins
        its own busy dot too."""
        tokens = current_tokens()
        QApplication.instance().setStyleSheet(
            build_stylesheet(tokens))
        for card in self.model_cards.values():
            card.apply_theme(tokens)
        self.busy_dot.setStyleSheet(
            "color: %s;" % tokens["accent"])
        if hasattr(self, "_empty_state_frame"):
            self._apply_empty_state_style()
        self._apply_overscroll_style()
        if self.pull_window is not None:
            self.pull_window.apply_theme(tokens)

    def _apply_density(self):
        """Apply the current list density: the grid's column
        count and gap, and every card's compact state. A relayout
        puts the cards back in the new shape (scroll and focus
        survive, as on any refresh)."""
        compact = get_compact_list()
        self._cards_layout.setSpacing(grid_spacing())
        for card in self.model_cards.values():
            card.set_compact(compact)
        self._relayout_cards(self._visible_paths)

    def show_how_to_use(self):
        if self.howto_window is None:
            self.howto_window = HowToUseWindow()
            self.howto_window.setAttribute(
                Qt.WidgetAttribute.WA_DeleteOnClose)
            self.howto_window.destroyed.connect(self._howto_closed)
        self.howto_window.show()
        self.howto_window.raise_()
        self.howto_window.activateWindow()

    def _howto_closed(self, _obj=None):
        self.howto_window = None

    def show_about(self):
        QMessageBox.information(
            self, "About Fyrn",
            "Fyrn %s\n\nA standalone model runner for Linux. "
            "It runs GGUF models on this PC, no other program "
            "needed." % __version__)

    def make_debug_report(self):
        try:
            path = self.log.generate_report()
        except OSError as exc:
            QMessageBox.warning(
                self, "Debug report",
                "The report could not be written (%s)." % exc)
            return
        QMessageBox.information(
            self, "Debug report",
            "Debug report written to:\n%s" % path)

    # ------------------------------------------------------------ geometry

    def _restore_geometry(self):
        restore_window_geometry(get_settings(), "window", self)

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        save_window_geometry(get_settings(), "window", self)
        if self.tray is not None:
            self.tray.hide()
        if self._scan_worker is not None:
            self._scan_worker.wait(5000)
        if self.load_worker is not None:
            self.load_worker.cancel()
            self.load_worker.wait(5000)
        if self.chat_worker is not None:
            self.chat_worker.cancel()
            self.chat_worker.wait(5000)
        if self.import_worker is not None:
            self.import_worker.cancel()
            self.import_worker.wait(5000)
        if self._update_check_worker is not None:
            # The check is a short network call with its own
            # timeout: wait it out rather than abandoning it.
            self._update_check_worker.wait(10000)
        if self._update_download_worker is not None:
            self._update_download_worker.cancel()
            self._update_download_worker.wait(5000)
        workers = (self._scan_worker, self.load_worker,
                   self.chat_worker, self.import_worker,
                   self._update_check_worker,
                   self._update_download_worker)
        if any(worker is not None and worker.isRunning()
               for worker in workers):
            # A worker is still running after its cancel and wait:
            # a real load never checks for a cancel, so it runs
            # until it finishes. Closing now would destroy a
            # running thread and abort the process ("QThread:
            # Destroyed while thread is still running"). Stay open
            # and close once the workers finish.
            self._close_pending = True
            if ((self._update_check_worker is not None
                 and self._update_check_worker.isRunning())
                    or (self._update_download_worker is not None
                        and self._update_download_worker.isRunning())):
                self._show_status_persistent(
                    "An update is still downloading. Fyrn will "
                    "close when it finishes.")
            else:
                self._show_status_persistent(
                    "A model is still loading. Fyrn will close when "
                    "it finishes.")
            event.ignore()
            return
        if self.controller.is_loaded:
            self.controller.unload()
        super().closeEvent(event)

    def bring_to_front(self):
        """Raise this window (used by the single-instance guard)."""
        self.show()
        self.raise_()
        self.activateWindow()


# ---------------------------------------------------------------- entry


def main(argv=None):
    """Open the Fyrn window. Second launches raise the first."""
    app = QApplication(list(argv) if argv is not None else [])
    # Lowercase, no spaces: this is the X11 WM_CLASS, and it must
    # match StartupWMClass in Fyrn.desktop exactly. The
    # window title stays "Fyrn" (set on MainWindow).
    app.setApplicationName("fyrn")
    app.setOrganizationName("BrokeNBored")
    app.setApplicationVersion(__version__)
    # One stylesheet across every window, from the active theme.
    app.setStyleSheet(current_stylesheet())
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))

    home = ensure_home()
    guard = SingleInstance(home)
    if not guard.try_take():
        guard.notify_running()
        return 0
    window = MainWindow()
    guard.start_server(window.bring_to_front)
    # The pre-Qt fast path's listener: a second launch sends
    # b"raise" to this socket without importing Qt. Started
    # after the window exists, so a raise always has a window
    # to raise.
    listener = RaiseListener(home / "raise.sock")
    listener.raise_requested.connect(window.bring_to_front)
    listener.start()
    window.show()
    # Warm the debug logger now that the window is visible: its
    # import costs ~0.5 s, which is why it is not paid during
    # construction. It is ready before the user can do anything.
    QTimer.singleShot(0, window._init_logger)
    # Warm the model engine in the background (task 2): the first
    # load of a session paid a 70-90 s engine import inside the
    # load worker. Warming it now, on a daemon thread, means the
    # first load starts loading at once. The thread never blocks
    # the window or the exit.
    threading.Thread(target=warm_engine, daemon=True).start()
    # The silent update check (v0.6): a background thread asks
    # GitHub for a newer release. Up to date, or unreachable,
    # means nothing happens at all; a newer release pops up
    # once. Off when the setting says so.
    if get_update_check():
        QTimer.singleShot(
            0, lambda: window.check_for_updates(manual=False))
    try:
        return app.exec()
    finally:
        listener.requestInterruption()
        listener.wait(2000)
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
