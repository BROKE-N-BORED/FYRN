"""Fyrn window: models, pull, settings, chat.

Native Qt desktop window. One function, one window (U1): the main
window holds the Models and Chat tabs, while Pull, Settings, and How
to Use each get their own closable window.

Visible text rules: no em-dashes (U33) and no bare "&" (U11) in any
label, button, or menu text. The suite sweeps for both.
"""

import hashlib
import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import (QEvent, QEasingCurve, QLockFile, QPoint,
                            QPropertyAnimation, QRect, QSettings, Qt,
                            QThread, QTimer, Signal)
from PySide6.QtGui import QCursor, QGuiApplication, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog,
                               QFileDialog, QFrame,
                               QGraphicsOpacityEffect, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow,
                               QMenu, QMessageBox, QProgressBar,
                               QPushButton, QScrollArea, QSizePolicy,
                               QSpacerItem, QSystemTrayIcon, QTabWidget,
                               QTextEdit, QToolTip, QVBoxLayout, QWidget)

from . import __version__
from . import core
from .core import (HEADROOM_BYTES, ModelController, ModelNotFoundError,
                   ModelTooBigError, ensure_home, ensure_models_folder,
                   fit_status, format_size, read_ram_bytes, remove_gguf,
                   scan_models)
from .debug_log import DebugLogger
from .pull import (DownloadError, PullCancelled, download_file,
                   parse_model_input)
from .runner import (GenerationCancelled, LoadCancelled, LlamaRunner,
                     RunnerError)

DEFAULT_WIDTH = 1100
DEFAULT_HEIGHT = 700
SETTINGS_ORG = "BrokeNBored"
SETTINGS_APP = "fyrn"

_HERE = Path(__file__).resolve().parent
ICON_PATH = _HERE.parent / "assets" / "fyrn.png"

# The look: one dark stylesheet, drawn from the icon, applied by
# main() across every window. Deep blue-black surfaces (#14202e, the
# icon tile), ember amber (#f2c14e) for selection and highlights,
# ember orange (#e8890c) for progress. The motion in the app is
# small and cheap: two one-shot 200 ms opacity fades (pull finish,
# newly pulled card), a 150 ms press dip on Send/Load/Pull, and a
# 200 ms 4 px hover lift on cards. The reduced-motion fallback
# skips all of it.
DARK_STYLESHEET = """
QWidget {
    background-color: #14202e;
    color: #e8e8e8;
    font-size: 11pt;
}
QMainWindow, QDialog {
    background-color: #14202e;
}
QLabel {
    color: #e8e8e8;
}
QPushButton {
    background-color: #243447;
    border: 1px solid #3a4d63;
    border-radius: 6px;
    padding: 6px 16px;
    color: #e8e8e8;
}
QPushButton:hover {
    border-color: #f2c14e;
}
QPushButton:pressed {
    background-color: #1a2836;
}
QPushButton:focus {
    border-color: #f2c14e;
}
QPushButton:disabled {
    color: #666666;
    border-color: #2a3644;
}
QLineEdit, QTextEdit {
    background-color: #0f1822;
    border: 1px solid #3a4d63;
    border-radius: 6px;
    padding: 5px 8px;
    color: #e8e8e8;
    selection-background-color: #e8890c;
}
QLineEdit:focus, QTextEdit:focus {
    border-color: #f2c14e;
}
QTabWidget::pane {
    border: 1px solid #3a4d63;
    background-color: #14202e;
}
QTabBar::tab {
    background-color: #1a2836;
    border: 1px solid #3a4d63;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 8px 20px;
    color: #bbbbbb;
}
QTabBar::tab:selected {
    background-color: #243447;
    color: #f2c14e;
    border-bottom: 2px solid #f2c14e;
}
QMenuBar {
    background-color: #14202e;
    color: #e8e8e8;
}
QMenuBar::item {
    padding: 4px 10px;
}
QMenuBar::item:selected {
    background-color: #243447;
}
QMenu {
    background-color: #1c2836;
    border: 1px solid #3a4d63;
    color: #e8e8e8;
}
QMenu::item:selected {
    background-color: #243447;
    color: #f2c14e;
}
QProgressBar {
    background-color: #0f1822;
    border: 1px solid #3a4d63;
    border-radius: 6px;
    text-align: center;
    color: #e8e8e8;
}
QProgressBar::chunk {
    background-color: #e8890c;
    border-radius: 6px;
}
QScrollBar:vertical {
    background-color: #14202e;
    width: 12px;
}
QScrollBar::handle:vertical {
    background-color: #3a4d63;
    border-radius: 6px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background-color: #f2c14e;
}
QScrollBar:horizontal {
    background-color: #14202e;
    height: 12px;
}
QScrollBar::handle:horizontal {
    background-color: #3a4d63;
    border-radius: 6px;
    min-width: 30px;
}
QScrollBar::handle:horizontal:hover {
    background-color: #f2c14e;
}
QListWidget {
    background-color: #0f1822;
    border: 1px solid #3a4d63;
    border-radius: 6px;
}
QListWidget::item:selected {
    background-color: #243447;
    color: #f2c14e;
}
QScrollArea {
    background-color: #14202e;
    border: none;
}
QStatusBar {
    background-color: #0f1822;
    color: #bbbbbb;
}
"""


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
        "big means it will not fit in free RAM.\n\n"
        "Pick a model and press Load. Double-click a model to load "
        "it straight away. Only one model runs at a time: loading "
        "a second one unloads the first. Press Unload when you are "
        "done to give the RAM back.\n\n"
        "Keyboard works too: Tab moves between models, Space picks "
        "the one in focus, Enter loads it.\n\n"
        "Import brings in a .gguf file from anywhere on the disk, "
        "such as a fine-tuned model you made elsewhere. It copies "
        "the file into your models folder.\n\n"
        "Remove deletes a model from the folder. It asks first, "
        "names the file and its size, and a loaded model is "
        "unloaded before it is removed."
    ),
    "Pulling a model": (
        "Pull downloads a model from the internet into your models "
        "folder. Open it from the Models menu.\n\n"
        "Type a Hugging Face model and its file name, like "
        "myname/mymodel mymodel.gguf, or paste a direct link to a "
        ".gguf file. Then press Pull.\n\n"
        "Not sure what to get first? The Pull window suggests a "
        "small first model that fits this PC.\n\n"
        "The window shows progress, how much has downloaded, and the "
        "speed. Press Cancel to stop; the part already downloaded "
        "stays, and pulling again picks up where it stopped instead "
        "of starting over.\n\n"
        "When the pull finishes, the model appears in the Models tab."
    ),
    "Settings": (
        "Settings lives in the File menu. It has two settings: the "
        "models folder, where pulled and imported models are kept, "
        "and Reduce motion.\n\n"
        "Press Browse to pick a different models folder, then Save. "
        "The Models tab switches to the new folder right away.\n\n"
        "Check Reduce motion if movement on screen bothers you: "
        "the short fades and lifts become instant instead.\n\n"
        "Everything else about how models run (threads, context "
        "size) is set by the program itself to fit this PC. There "
        "is nothing else to set."
    ),
    "Chatting": (
        "Load a model first, then open the Chat tab.\n\n"
        "Type a message and press Send (or Enter). The reply streams "
        "in as the model writes it. Press Stop to cut a reply short. "
        "Shift+Enter starts a new line without sending. Clear "
        "empties the transcript.\n\n"
        "If no model is loaded, Send stays greyed out. Load a model "
        "from the Models tab first."
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
    off every screen (a monitor setup that changed since)."""
    values = [settings.value("%s/%s" % (prefix, key), type=int)
              for key in ("x", "y", "width", "height")]
    if any(value is None for value in values):
        return
    rect = QRect(*values)
    screens = QGuiApplication.screens()
    if screens and not any(
            screen.availableGeometry().intersects(rect)
            for screen in screens):
        return
    widget.setGeometry(rect)


# ---------------------------------------------------------------- workers


class LoadWorker(QThread):
    """Load a model off the UI thread (U30)."""

    # percent, message; percent is -1 when the backend has no
    # percent to give (indeterminate bar, honest stage text).
    progress = Signal(int, str)
    finished = Signal(dict)      # {"load_time_s": float, "warning": str}
    failed = Signal(str)         # plain-words message

    def __init__(self, controller, path):
        super().__init__()
        self.controller = controller
        self.path = path
        self.cancel_event = threading.Event()

    def run(self):
        try:
            result = self.controller.load(
                self.path,
                progress_cb=lambda pct, msg: self.progress.emit(pct, msg),
                cancel_event=self.cancel_event)
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
    """Stream one reply off the UI thread (U30)."""

    token = Signal(str)
    finished = Signal(dict)  # generation stats
    failed = Signal(str)

    def __init__(self, runner, prompt):
        super().__init__()
        self.runner = runner
        self.prompt = prompt
        self.cancel_event = threading.Event()

    def run(self):
        try:
            stats = self.runner.generate(
                self.prompt,
                on_token=lambda piece: self.token.emit(piece),
                cancel_event=self.cancel_event)
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

    def cancel(self):
        self.cancel_event.set()


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
        try:
            download_file(
                self.url, self.dest,
                progress_cb=lambda d, t, s: self.progress.emit(
                    d, -1 if t is None else t, s),
                cancel_event=self.cancel_event)
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


class ScanWorker(QThread):
    """Scan the models folder off the UI thread (U30).

    The disk scan is quick, but it runs on a spinning hard drive,
    so it never blocks the window. While it runs, the Models tab
    shows skeleton tiles (honest loading).
    """

    finished = Signal(list, list, object)  # models, skipped, free_bytes

    def __init__(self, folder):
        super().__init__()
        self.folder = Path(folder)

    def run(self):
        models, skipped = scan_models(self.folder)
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
    Progress, MB downloaded, and speed while it runs (U26), and the
    pull resumes after a failure or a cancel (U24).
    """

    model_pulled = Signal(str)  # saved path

    def __init__(self, models_folder, log=None, parent=None):
        super().__init__(parent)
        self.models_folder = Path(models_folder)
        self.log = log
        self.worker = None
        self._celebrate_anim = None
        self.setWindowTitle("Pull a model")
        self.setMinimumSize(560, 300)
        self.resize(640, 340)
        restore_window_geometry(get_settings(), "pull_window", self)

        layout = QVBoxLayout(self)

        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "myname/mymodel mymodel.gguf  or  https://.../model.gguf")
        self.input.returnPressed.connect(self._start_pull)
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
        self.suggest_button.clicked.connect(self._fill_suggestion)
        suggest_row.addWidget(self.suggest_button)
        suggest_row.addStretch(1)
        layout.addLayout(suggest_row)

        buttons = QHBoxLayout()
        self.pull_button = PressDipButton("Pull")
        self.pull_button.clicked.connect(self._start_pull)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel_pull)
        self.cancel_button.setEnabled(False)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.pull_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("Nothing pulling yet.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.busy_dot = QLabel("\u25cf")
        self.busy_dot.setObjectName("busy_dot")
        self.busy_dot.setStyleSheet("color: #f2c14e;")
        self.busy_dot.setVisible(False)
        layout.addWidget(self.busy_dot)

    def _fill_suggestion(self):
        """Fill the field with the suggested first model."""
        self.input.setText(FIRST_PULL_SUGGESTION)
        self.input.setFocus()

    def _set_busy(self, busy):
        self.busy_dot.setVisible(busy)
        self.pull_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.input.setEnabled(not busy)

    def _start_pull(self):
        if self.worker is not None:
            return
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
        if dest.exists():
            # Same pattern: an outcome, not an interruption.
            self.status_label.setText(
                "%s is already in your models folder."
                % target.filename)
            return
        if self.log is not None:
            self.log.action("pull started: %s" % target.filename)
        self._set_busy(True)
        self.progress_bar.setValue(0)
        self.status_label.setText("Starting download...")
        self.worker = PullWorker(target.url, dest)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(self._on_cancelled)
        self.worker.start()

    def _on_progress(self, downloaded, total, speed):
        if total and total > 0:
            self.progress_bar.setRange(0, 100)
            pct = int(downloaded * 100 / total)
            self.progress_bar.setValue(pct)
            self.status_label.setText(
                "%s of %s downloaded, %s per second"
                % (format_size(downloaded), format_size(total),
                   format_size(int(speed))))
        else:
            self.progress_bar.setRange(0, 0)  # unknown total: busy bar
            self.status_label.setText(
                "%s downloaded, %s per second"
                % (format_size(downloaded), format_size(int(speed))))

    def _on_finished(self, path):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.status_label.setText("Done. The model is in your models "
                                  "folder.")
        self._celebrate()
        self._set_busy(False)
        self._drop_worker()
        if self.log is not None:
            self.log.action("pull finished: %s" % path)
        self.model_pulled.emit(path)

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
        self.progress_bar.setRange(0, 100)
        self.status_label.setText(message)
        self._set_busy(False)
        self._drop_worker()
        if self.log is not None:
            self.log.error("pull failed: %s", message)
        QMessageBox.warning(self, "Pull a model", message)

    def _on_cancelled(self):
        self.progress_bar.setRange(0, 100)
        self.status_label.setText(
            "Pull stopped. The part already downloaded stays, and "
            "pulling again picks up where it stopped.")
        self._set_busy(False)
        self._drop_worker()

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
            answer = QMessageBox.question(
                self, "Pull a model",
                "Stop this pull? The part already downloaded stays, "
                "and pulling again picks up where it stopped.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(5000)
        super().closeEvent(event)


class SettingsWindow(QDialog):
    """Settings: its own resizable pop-up window (U1).

    Two settings, both ones the user would change for a real
    reason (U36): the models folder, and Reduce motion. The
    window is resizable and maximizable; nothing pins its size.
    """

    folder_changed = Signal(str)

    def __init__(self, current_folder, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(480, 180)
        self.resize(560, 220)
        restore_window_geometry(
            get_settings(), "settings_window", self)

        layout = QVBoxLayout(self)

        label = QLabel("Models folder")
        layout.addWidget(label)

        row = QHBoxLayout()
        self.folder_edit = QLineEdit(str(current_folder))
        self.folder_edit.setReadOnly(True)
        browse_button = QPushButton("Browse")
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

        layout.addStretch(1)

        buttons = QHBoxLayout()
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self._save)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

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
        self.folder_changed.emit(str(folder))
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


# ---------------------------------------------------------------- tiles

# Bento grid numbers (entry 005): 3 columns at this window width,
# 16 px gaps, 12 px corners. Cards size to their content; the hero
# tile spans the full width and earns it with live stats.
GRID_COLUMNS = 3
GRID_SPACING = 16


FIT_BADGES = {
    "fits": ("Fits", "#2e7d32"),
    "tight": ("Tight", "#b7791f"),
    "too big": ("Too big", "#b3261e"),
    "unknown": ("Unknown", "#666666"),
}


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
                 free_bytes=None, parent=None):
        super().__init__(parent)
        self.path = str(model["path"])
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
        # Content height, not a fixed box: a card holds three lines
        # (name, size, badge), so ~100 px is enough; the old
        # 110-150 px box left each card ~60% empty inside. A card
        # with a description gets room for it.
        self.setMinimumHeight(120 if hero else 96)
        # Cap the height: the grid's expanding spacer takes the
        # leftover space, but without a cap the tiles still grow
        # past their content (250 px tall with 2 models).
        if hero:
            self.setMaximumHeight(170)
        elif self.description:
            self.setMaximumHeight(150)
        else:
            self.setMaximumHeight(120)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        name = QLabel(model["name"])
        name.setWordWrap(True)
        name.setStyleSheet("font-size: %dpt; font-weight: bold;"
                           % (16 if hero else 13))
        layout.addWidget(name)

        size = QLabel(format_size(model["size"]))
        size.setStyleSheet("font-size: 11pt; color: #999999;")
        layout.addWidget(size)

        if self.description:
            desc_label = QLabel(self.description)
            desc_label.setObjectName("model_description")
            desc_label.setWordWrap(True)
            desc_label.setStyleSheet(
                "font-size: 11pt; color: #999999;")
            layout.addWidget(desc_label)

        badge_text, badge_color = FIT_BADGES[status]
        badge_row = QHBoxLayout()
        badge = QLabel(badge_text)
        badge.setObjectName("fit_badge")
        badge.setStyleSheet(
            "background-color: %s; color: white; font-size: 11pt; "
            "font-weight: bold; padding: 3px 10px; border-radius: 8px;"
            % badge_color)
        badge_row.addWidget(badge)
        if loaded:
            loaded_label = QLabel("Loaded")
            loaded_label.setStyleSheet(
                "color: #f2c14e; font-size: 11pt; font-weight: bold; "
                "border: 1px solid #f2c14e; padding: 2px 9px; "
                "border-radius: 8px;")
            badge_row.addWidget(loaded_label)
        badge_row.addStretch(1)
        layout.addLayout(badge_row)
        # The hero tile earns its size with live stats (load time,
        # then speed once a reply exists), updated by the main
        # window's stats refresh. Plain cards have no stats line.
        self.stats_label = None
        if hero:
            self.stats_label = QLabel("")
            self.stats_label.setObjectName("hero_stats")
            self.stats_label.setStyleSheet(
                "font-size: 11pt; color: #999999;")
            self.stats_label.setWordWrap(True)
            layout.addWidget(self.stats_label)
        layout.addStretch(1)

        # Clicks on the labels must reach the card, not stop at the
        # label: the whole tile is the thing you tap.
        for child in self.findChildren(QLabel):
            child.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._apply_frame()

    def set_stats(self, text):
        """Show live stats in the hero tile (no-op on a plain
        card)."""
        if self.stats_label is not None:
            self.stats_label.setText(text)

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
        needed = model["size"] + HEADROOM_BYTES
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
        return "\n".join(lines)

    def set_selected(self, selected):
        self._selected = selected
        self._apply_frame()

    def _apply_frame(self):
        # Selected and keyboard-focused cards both get the amber
        # border, so focus is visible at a glance. Labels inside
        # the card are transparent: the global widget background
        # (#14202e) is darker than the card (#1c2836), so an
        # opaque label shows a darker band behind its text. The
        # badge keeps its own background (its own stylesheet wins
        # over this descendant rule).
        if self._selected or self.hasFocus():
            border = "2px solid #f2c14e"
        else:
            border = "1px solid #555555"
        self.setStyleSheet(
            "ModelCard { border: %s; border-radius: 12px; "
            "background-color: #1c2836; } "
            "ModelCard QLabel { background-color: transparent; }"
            % border)

    def focusInEvent(self, event):  # noqa: N802 - Qt naming
        self._apply_frame()
        # Keyboard focus shows the same detail hover does, so a
        # keyboard user gets the preview without a mouse.
        QToolTip.showText(
            self.mapToGlobal(QPoint(0, self.height())),
            self.toolTip(), self)
        super().focusInEvent(event)

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
        self._lift_anim = anim
        anim.start()

    def _end_lift(self):
        if self._lift_rest is None:
            return
        rest = self._lift_rest
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(200)
        anim.setStartValue(self.geometry())
        anim.setEndValue(rest)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(
            lambda a=anim, r=rest: self._on_drop_finished(a, r))
        self._lift_anim = anim
        anim.start()

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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        # Same height as a regular card, so the list does not jump
        # when the skeletons are replaced by real cards.
        self.setMinimumHeight(96)
        self.setMaximumHeight(120)
        self.setStyleSheet(
            "SkeletonCard { border: 1px solid #444444; "
            "border-radius: 12px; background-color: #1a2531; } "
            "QFrame#skeleton_bar { background-color: #2a3644; "
            "border: none; border-radius: 4px; }")
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
        self.log = log or DebugLogger(
            "fyrn", __version__, log_dir=str(home / "logs"))
        self.log.register_dependency("PySide6")
        self.log.register_dependency("llama-cpp-python")
        self.log.install_crash_handler()

        self.runner = runner if runner is not None else LlamaRunner()
        self.controller = ModelController(self.runner)
        self.models_folder = ensure_models_folder(get_models_folder())
        self.log.set_setting("models_folder", str(self.models_folder))

        self.load_worker = None
        self.chat_worker = None
        self.pull_window = None
        self.settings_window = None
        self.howto_window = None
        self.tray = None
        self.load_time_s = None
        self._reply_in_progress = False

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
        self._pulse_path = None
        self._pulse_anim = None
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
        self.busy_dot.setStyleSheet("color: #f2c14e;")
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

        self._refresh_model_list()
        self._refresh_stats()
        self._setup_tray()

    # ------------------------------------------------------------ menus

    def _build_menus(self):
        file_menu = self.menuBar().addMenu("File")
        settings_action = file_menu.addAction("Settings")
        settings_action.triggered.connect(self.show_settings)
        report_action = file_menu.addAction("Debug report")
        report_action.triggered.connect(self.make_debug_report)
        file_menu.addSeparator()
        quit_action = file_menu.addAction("Quit")
        quit_action.triggered.connect(self.close)

        models_menu = self.menuBar().addMenu("Models")
        pull_action = models_menu.addAction("Pull models")
        pull_action.triggered.connect(self.show_pull_window)
        import_action = models_menu.addAction("Import a GGUF file")
        import_action.triggered.connect(self.import_model)
        remove_action = models_menu.addAction("Remove model")
        remove_action.triggered.connect(self.remove_selected_model)
        refresh_action = models_menu.addAction("Refresh list")
        refresh_action.triggered.connect(self._refresh_model_list)
        unload_action = models_menu.addAction("Unload model")
        unload_action.triggered.connect(self.unload_model)

        help_menu = self.menuBar().addMenu("Help")
        howto_action = help_menu.addAction("How to Use")
        howto_action.triggered.connect(self.show_how_to_use)
        about_action = help_menu.addAction("About")
        about_action.triggered.connect(self.show_about)

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

        # Bento grid: models as tiles, size as hierarchy, fit front
        # and center. The loaded model gets the hero tile.
        self._cards_scroll = QScrollArea()
        self._cards_scroll.setWidgetResizable(True)
        self._cards_widget = QWidget()
        self._cards_layout = QGridLayout(self._cards_widget)
        self._cards_layout.setSpacing(GRID_SPACING)
        self._cards_scroll.setWidget(self._cards_widget)
        layout.addWidget(self._cards_scroll)

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
        self.load_progress.setVisible(False)
        layout.addWidget(self.load_progress)

        buttons = QHBoxLayout()
        self.load_button = PressDipButton("Load")
        self.load_button.clicked.connect(self.load_selected_model)
        self.unload_button = QPushButton("Unload")
        self.unload_button.clicked.connect(self.unload_model)
        self.import_button = QPushButton("Import")
        self.import_button.clicked.connect(self.import_model)
        self.pull_button = PressDipButton("Pull")
        self.pull_button.clicked.connect(self.show_pull_window)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self.remove_selected_model)
        self.load_cancel_button = QPushButton("Cancel load")
        self.load_cancel_button.clicked.connect(self._cancel_load)
        self.load_cancel_button.setVisible(False)
        buttons.addWidget(self.load_button)
        buttons.addWidget(self.unload_button)
        buttons.addWidget(self.import_button)
        buttons.addWidget(self.pull_button)
        buttons.addWidget(self.remove_button)
        buttons.addWidget(self.load_cancel_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        folder_label = QLabel(
            "Models live in the folder set in File menu, Settings.")
        folder_label.setWordWrap(True)
        layout.addWidget(folder_label)

        hint_label = QLabel("Double-click a model to load it.")
        hint_label.setWordWrap(True)
        layout.addWidget(hint_label)

        self.tabs.addTab(tab, "Models")

    def _build_empty_state(self):
        """The no-models-yet panel: plain words, a short setup
        checklist, and working Pull and Import buttons wired to
        the same actions as the buttons below."""
        frame = QFrame()
        frame.setObjectName("empty_state")
        frame.setStyleSheet(
            "QFrame#empty_state { border: 1px solid #555555; "
            "border-radius: 12px; background-color: #1c2836; } "
            "QFrame#empty_state QLabel { "
            "background-color: transparent; }")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(8)

        title = QLabel("No models yet")
        title.setStyleSheet("font-size: 16pt; font-weight: bold;")
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
        pull_button.clicked.connect(self.show_pull_window)
        import_button = QPushButton("Import")
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
        say "Pick a model" when there is nothing to pick."""
        self._empty_state.setVisible(show)
        self._cards_scroll.setVisible(not show)
        if show:
            if (not self.controller.is_loaded
                    and self.load_worker is None):
                self._show_status_persistent(
                    "No models yet. Pull one or import one to "
                    "get started.")
                self._empty_status = True
        elif self._empty_status:
            self._show_status_persistent(
                "Pick a model and press Load, or pull one from the "
                "Models menu.")
            self._empty_status = False

    def _build_chat_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText(
            "Load a model first, then chat here.")
        layout.addWidget(self.transcript)

        # Calm technology: the live numbers sit quietly in muted
        # text. They inform at a glance, they never shout.
        self.stats_label = QLabel("No model loaded.")
        self.stats_label.setWordWrap(True)
        self.stats_label.setStyleSheet("color: #999999;")
        layout.addWidget(self.stats_label)

        row = QHBoxLayout()
        self.chat_input = ChatInput()
        self.chat_input.send_requested.connect(self.send_message)
        self.send_button = PressDipButton("Send")
        self.send_button.clicked.connect(self.send_message)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self._stop_reply)
        self.stop_button.setEnabled(False)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self._clear_chat)
        row.addWidget(self.chat_input)
        row.addWidget(self.send_button)
        row.addWidget(self.stop_button)
        row.addWidget(self.clear_button)
        layout.addLayout(row)

        self.tabs.addTab(tab, "Chat")

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
                widget.setParent(None)
                widget.deleteLater()
        self.model_cards = {}

    def _show_skeleton(self):
        """Skeleton tiles while the folder scan runs: the shape of
        the list appears first, the models fill it in."""
        self._set_empty_state(False)
        self._clear_cards()
        for index in range(3):
            self._cards_layout.addWidget(
                SkeletonCard(), index // GRID_COLUMNS,
                index % GRID_COLUMNS)
        rows = (3 + GRID_COLUMNS - 1) // GRID_COLUMNS
        self._cards_layout.addItem(
            QSpacerItem(0, 0, QSizePolicy.Policy.Minimum,
                        QSizePolicy.Policy.Expanding),
            rows, 0, 1, GRID_COLUMNS)

    def _refresh_model_list(self):
        """Show skeleton tiles, then scan the folder off the UI
        thread. A refresh asked for mid-scan runs right after the
        current scan finishes, never on top of it."""
        if self._scan_worker is not None:
            self._scan_dirty = True
            return
        self._show_skeleton()
        self._scan_worker = ScanWorker(self.models_folder)
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
        self._clear_cards()
        loaded_path = (str(self.controller.current_path)
                       if self.controller.is_loaded else None)
        # The loaded model goes first as the hero tile; the rest
        # follow in name order.
        ordered = sorted(
            models,
            key=lambda m: (str(m["path"]) != loaded_path,
                           m["name"].lower()))
        row = 0
        col = 0
        self._hero_card = None
        for model in ordered:
            status = fit_status(model["size"], free_bytes)
            is_loaded = str(model["path"]) == loaded_path
            card = ModelCard(model, status, loaded=is_loaded,
                             hero=is_loaded, free_bytes=free_bytes)
            card.clicked.connect(self._on_card_clicked)
            card.double_clicked.connect(self._on_card_double_clicked)
            card.remove_requested.connect(self._on_remove_requested)
            if is_loaded:
                self._hero_card = card
                self._cards_layout.addWidget(
                    card, row, 0, 1, GRID_COLUMNS)
                row += 1
                col = 0
            else:
                self._cards_layout.addWidget(card, row, col)
                col += 1
                if col >= GRID_COLUMNS:
                    col = 0
                    row += 1
            self.model_cards[str(model["path"])] = card
        # An expanding spacer pins the tiles to the top: without it
        # the grid stretches a short list of tiles to fill the tab.
        spacer_row = row + 1 if col else row
        self._cards_layout.addItem(
            QSpacerItem(0, 0, QSizePolicy.Policy.Minimum,
                        QSizePolicy.Policy.Expanding),
            spacer_row, 0, 1, GRID_COLUMNS)
        # The rebuilt hero tile gets its stats right away, instead
        # of waiting for the next 2-second refresh.
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
        if self._pulse_path is not None:
            card = self.model_cards.get(self._pulse_path)
            self._pulse_path = None
            if card is not None:
                self._pulse_card(card)
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

    def _update_buttons(self):
        has_selection = self._selected_model_path() is not None
        loading = self.load_worker is not None
        loaded = self.controller.is_loaded
        self.load_button.setEnabled(has_selection and not loading)
        self.remove_button.setEnabled(has_selection and not loading)
        self.unload_button.setEnabled(loaded and not loading)
        self.send_button.setEnabled(
            loaded and self.chat_worker is None)
        self.load_cancel_button.setVisible(loading)
        self.load_progress.setVisible(loading)

    def load_selected_model(self):
        if self.load_worker is not None:
            return
        path = self._selected_model_path()
        if path is None:
            self._show_status(
                "Pick a model from the list first.")
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
        self._show_status_persistent("Loading %s..." % path.name)
        self.load_worker = LoadWorker(self.controller, path)
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
        self._show_status_persistent(message)

    def _on_load_finished(self, result):
        self._drop_load_worker()
        self.load_time_s = result.get("load_time_s", 0.0)
        warning = result.get("warning", "")
        self._set_busy(False)
        self._refresh_stats()
        self._update_buttons()
        # Rebuild the cards so the loaded model becomes the hero
        # tile with its Loaded pill; otherwise the list still shows
        # the pre-load cards with nothing marked.
        self._refresh_model_list()
        name = self.controller.current_name or "model"
        if warning:
            self._show_status(
                "%s loaded, but RAM is tight." % name)
        else:
            self._show_status("%s loaded." % name)
        self.log.action("load finished: %s" % name)
        # The empty transcript no longer says "Load a model first":
        # a model IS loaded now, so the hint switches to the true one.
        self.transcript.setPlaceholderText("Type a message below.")
        self.tabs.setCurrentIndex(1)  # Chat tab

    def _on_load_failed(self, message):
        self._drop_load_worker()
        self._set_busy(False)
        self._update_buttons()
        self._show_status(message)
        self.log.error("load failed: %s", message)
        if message != "Load cancelled.":
            QMessageBox.warning(self, "Load model", message)

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

    def _cancel_load(self):
        if self.load_worker is not None:
            self.load_worker.cancel()
            self._show_status_persistent("Stopping the load...")

    def unload_model(self):
        if not self.controller.is_loaded:
            return
        name = self.controller.current_name or "model"
        self.controller.unload()
        self.load_time_s = None
        self._refresh_stats()
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
        self._remove_model(path)

    def _remove_model(self, path):
        """Delete a model file, after a confirm that defaults to
        No (U31). A loaded model is unloaded first, never deleted
        out from under the engine."""
        path = Path(path)
        if not path.is_file():
            self._show_status("That model file is gone.")
            self._refresh_model_list()
            return
        loaded = (self.controller.is_loaded
                  and self.controller.current_path == path)
        message = (
            "Remove %s (%s) from your models folder?\n\n"
            "This deletes the file. You can pull it again later."
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
            remove_gguf(path)
        except ValueError as exc:
            QMessageBox.warning(self, "Remove model", str(exc))
            self._refresh_model_list()
            return
        self.log.action("removed: %s" % path.name)
        self._selected_path = None
        self._status_after_scan = "%s removed." % path.name
        self._refresh_model_list()

    def import_model(self):
        path, _selected = QFileDialog.getOpenFileName(
            self, "Pick a GGUF model file", str(Path.home()),
            "GGUF models (*.gguf)")
        if not path:
            return
        try:
            dest = core.import_gguf(path, self.models_folder)
        except ValueError as exc:
            QMessageBox.warning(self, "Import a GGUF file", str(exc))
            return
        self.log.action("imported: %s" % dest.name)
        self._status_after_scan = (
            "%s is now in your models folder." % dest.name)
        self._select_path = str(dest)
        self._refresh_model_list()

    # ------------------------------------------------------------ chat

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
        # The reply carries the model name that wrote it (captured
        # now, so a model switch mid-session never mislabels it).
        name = self.controller.current_name or "Model"
        self.transcript.append("%s: " % name)
        self._reply_in_progress = True
        self.log.action("chat sent (%d chars)" % len(text))
        self._set_busy(True)
        self.chat_worker = ChatWorker(self.runner, text)
        self.chat_worker.token.connect(self._on_chat_token)
        self.chat_worker.finished.connect(self._on_chat_finished)
        self.chat_worker.failed.connect(self._on_chat_failed)
        self.chat_worker.start()
        self._update_buttons()
        self.stop_button.setEnabled(True)

    def _on_chat_token(self, piece):
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
            return
        self._end_turn()
        self._last_stats = stats
        self._refresh_stats()
        self._show_status("Reply done.")

    def _on_chat_failed(self, message):
        self._drop_chat_worker()
        self._reply_in_progress = False
        self._set_busy(False)
        self.stop_button.setEnabled(False)
        self._update_buttons()
        self._end_turn("(%s)" % message)
        self._show_status(message)
        self.log.error("chat failed: %s", message)

    def _stop_reply(self):
        if self.chat_worker is not None:
            self.chat_worker.cancel()
            self._show_status_persistent("Stopping the reply...")

    def _clear_chat(self):
        """Clear the transcript. Nothing is saved, so nothing is
        lost: no confirm is needed (L5)."""
        self.transcript.clear()

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

    def show_pull_window(self):
        if self.pull_window is None:
            self.pull_window = PullWindow(
                self.models_folder, log=self.log)
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
        self._show_status(
            "Models folder is now %s." % folder)

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
    # One dark stylesheet across every window, drawn from the icon.
    app.setStyleSheet(DARK_STYLESHEET)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))

    home = ensure_home()
    guard = SingleInstance(home)
    if not guard.try_take():
        guard.notify_running()
        return 0
    window = MainWindow()
    guard.start_server(window.bring_to_front)
    window.show()
    try:
        return app.exec()
    finally:
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
