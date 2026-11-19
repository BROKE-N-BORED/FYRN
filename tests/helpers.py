"""Test helpers: a fake HTTP server with Range support, fake .gguf
files, and a modal-dialog watcher for GUI tests.

Nothing here touches the network or a real model.
"""

import os
import re
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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


def write_gguf_file(path, metadata, version=3, numbers=None,
                    tensors=None, file_type=0, arrays=None):
    """Write a minimal valid GGUF file with the given string
    metadata (dict key -> str) and numeric metadata (dict key ->
    int, written as UINT32), plus one uint32 entry
    (general.file_type, the file_type argument, default F32)
    and one string array, so parser tests have a real file to
    read. tensors is a list of (name, dims, type) tuples
    written as tensor infos after the KV section (v3 only); the
    default is no tensors. arrays is a dict key -> (elem_type,
    values): numeric arrays (elem_type 4 UINT32, 5 INT32, 7
    BOOL) written as ARRAY entries, for per-layer metadata like
    LFM3's head_count_kv."""
    if tensors and version != 3:
        raise ValueError("tensors are only supported for GGUF v3")
    tensors = list(tensors or [])
    array_entries = list((arrays or {}).items())
    out = bytearray()
    out += b"GGUF"
    out += struct.pack("<I", version)
    entries = list(metadata.items())
    number_entries = list((numbers or {}).items())
    kv_count = (len(entries) + len(number_entries)
                + len(array_entries) + 2)
    if version == 3:
        out += struct.pack("<Q", len(tensors))  # tensor count
        out += struct.pack("<Q", kv_count)
    else:
        out += struct.pack("<I", 0)
        out += struct.pack("<I", kv_count)

    def put_string(text):
        blob = text.encode("utf-8")
        out.extend(struct.pack("<Q", len(blob)))
        out.extend(blob)

    for key, value in entries:
        put_string(key)
        out.extend(struct.pack("<I", 8))  # STRING
        put_string(value)
    for key, value in number_entries:
        put_string(key)
        out.extend(struct.pack("<I", 4))  # UINT32
        out.extend(struct.pack("<I", value))
    _array_pack = {4: ("<I", 4), 5: ("<i", 4), 7: ("<?", 1)}
    for key, (elem_type, values) in array_entries:
        code, _size = _array_pack[elem_type]
        put_string(key)
        out.extend(struct.pack("<I", 9))  # ARRAY
        out.extend(struct.pack("<I", elem_type))
        out.extend(struct.pack("<Q", len(values)))
        for value in values:
            out.extend(struct.pack(code, value))
    # One non-string entry: general.file_type, uint32.
    put_string("general.file_type")
    out.extend(struct.pack("<I", 4))  # UINT32
    out.extend(struct.pack("<I", file_type))
    # One array entry: two strings, skipped by the parser.
    put_string("general.tags")
    out.extend(struct.pack("<I", 9))  # ARRAY
    out.extend(struct.pack("<I", 8))  # of STRING
    out.extend(struct.pack("<Q", 2))
    put_string("test")
    put_string("gguf")
    # Tensor infos: name, dimension count, dimensions, type,
    # offset (no tensor data follows; the parser only reads the
    # infos for the parameter count).
    for name, dims, tensor_type in tensors:
        put_string(name)
        out.extend(struct.pack("<I", len(dims)))
        for dim in dims:
            out.extend(struct.pack("<Q", dim))
        out.extend(struct.pack("<I", tensor_type))
        out.extend(struct.pack("<Q", 0))
    Path(path).write_bytes(bytes(out))
    return path


class RangeRequestHandler(BaseHTTPRequestHandler):
    """Serves one fixed payload, with HTTP Range support.

    Set SUPPORT_RANGE = False on a subclass to simulate a server
    that ignores Range (the download must restart from 0).
    Set FAIL_AFTER = N to drop the connection after N bytes (the
    download must fail, keeping the partial file for resume).
    Set ETAG to a string to send it as the ETag header.
    """

    PAYLOAD = b""
    SUPPORT_RANGE = True
    FAIL_AFTER = None
    ETAG = None

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
        if self.ETAG is not None:
            self.send_header("ETag", self.ETAG)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def start_range_server(payload, support_range=True, fail_after=None,
                       etag=None):
    """Start a local server serving payload. Returns (server, url).
    The bound handler class is on server.handler_class, so a test
    can change its ETAG between requests."""
    handler = type("BoundHandler", (RangeRequestHandler,), {
        "PAYLOAD": payload,
        "SUPPORT_RANGE": support_range,
        "FAIL_AFTER": fail_after,
        "ETAG": etag,
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.handler_class = handler
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
