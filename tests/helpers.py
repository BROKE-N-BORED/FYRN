"""Test helpers: a fake HTTP server with Range support, fake .gguf
files, and a modal-dialog watcher for GUI tests.

Nothing here touches the network or a real model.
"""

import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox


def make_fake_gguf(path, size):
    """Write a deterministic fake .gguf file of size bytes."""
    path = str(path)
    chunk = bytes(range(256)) * 4  # 1024 deterministic bytes
    with open(path, "wb") as fh:
        remaining = size
        while remaining > 0:
            piece = chunk[:min(len(chunk), remaining)]
            fh.write(piece)
            remaining -= len(piece)
    return path


def file_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


class RangeRequestHandler(BaseHTTPRequestHandler):
    """Serves one fixed payload, with HTTP Range support.

    Set SUPPORT_RANGE = False on a subclass to simulate a server
    that ignores Range (the download must restart from 0).
    Set FAIL_AFTER = N to drop the connection after N bytes (the
    download must fail, keeping the partial file for resume).
    """

    PAYLOAD = b""
    SUPPORT_RANGE = True
    FAIL_AFTER = None

    def log_message(self, *args):  # keep test output clean
        pass

    def do_GET(self):
        payload = self.PAYLOAD
        start = 0
        range_header = self.headers.get("Range")
        if range_header and self.SUPPORT_RANGE:
            match = re.match(r"bytes=(\d+)-", range_header)
            if match:
                start = int(match.group(1))
        if start >= len(payload):
            self.send_response(416)
            self.send_header("Content-Range",
                             "bytes */%d" % len(payload))
            self.end_headers()
            return
        body = payload[start:]
        if self.FAIL_AFTER is not None:
            body = body[:self.FAIL_AFTER]
        if start > 0 and self.SUPPORT_RANGE:
            self.send_response(206)
            self.send_header(
                "Content-Range",
                "bytes %d-%d/%d" % (start, len(payload) - 1,
                                   len(payload)))
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def start_range_server(payload, support_range=True, fail_after=None):
    """Start a local server serving payload. Returns (server, url)."""
    handler = type("BoundHandler", (RangeRequestHandler,), {
        "PAYLOAD": payload,
        "SUPPORT_RANGE": support_range,
        "FAIL_AFTER": fail_after,
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:%d/model.gguf" % server.server_address[1]
    return server, url


class ModalWatcher:
    """Records and dismisses unexpected modal dialogs in GUI tests.

    A settle pump can hang forever inside a dialog's nested event
    loop, so the watcher runs on a persistent QTimer and clicks the
    dialog's default button (by role, never by text: QMessageBox
    buttons carry a mnemonic, "&Yes" not "Yes").
    """

    def __init__(self):
        self.seen = []  # (title, text) of every dialog dismissed
        self.enabled = True
        self._timer = QTimer()
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._sweep)
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _sweep(self):
        if not self.enabled:
            return
        app = QApplication.instance()
        if app is None:
            return
        for widget in app.topLevelWidgets():
            if not isinstance(widget, QDialog):
                continue
            if not widget.isModal() or not widget.isVisible():
                continue
            # Only auto-dismiss plain message boxes; the app's own
            # windows (Pull, Settings, How to Use) are not modal.
            if isinstance(widget, QMessageBox):
                self.seen.append((widget.windowTitle(), widget.text()))
                button = widget.defaultButton()
                if button is not None:
                    button.click()
                else:
                    widget.accept()
