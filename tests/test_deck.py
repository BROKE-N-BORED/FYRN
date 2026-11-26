"""Tests for the phone deck (v0.11): the served page, the
home-network address helpers, and the vendored QR generator.

Qt-free: the page is data, the helpers are stdlib, and the QR
library is pure Python. The served script is extracted and
syntax-checked with node when node is on the box, because a
Python string's escapes have corrupted served JavaScript
before while every Python-side test stayed green.
"""

import re
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fyrn import qrcodegen
from fyrn.bridge import deck_url, detect_lan_ip
from fyrn.deck import DECK_HTML


def _script_of(html):
    match = re.search(r"<script>(.*?)</script>", html, re.S)
    assert match, "the deck page carries no script"
    return match.group(1)


class DeckPageTests(unittest.TestCase):
    def test_key_markers_present(self):
        for marker in ('id="model"', 'id="chat"', 'id="input"',
                       'id="send"', 'id="stop"', 'id="newchat"',
                       'id="photofile"', 'accept="image/*"',
                       "/api/tags", "/api/ps", "/api/chat",
                       "stream: true"):
            self.assertIn(marker, DECK_HTML)

    def test_plain_words_states_present(self):
        self.assertIn("Loading the model", DECK_HTML)
        self.assertIn(
            "Fyrn is busy at the PC right now", DECK_HTML)
        self.assertIn("Can't reach Fyrn", DECK_HTML)
        self.assertIn("same WiFi", DECK_HTML)

    def test_photo_downscale_present(self):
        # The photo is shrunk on a canvas to at most 1024 px
        # and JPEG-encoded before it is sent.
        self.assertIn("1024", DECK_HTML)
        self.assertIn("image/jpeg", DECK_HTML)
        self.assertIn("toDataURL", DECK_HTML)

    def test_no_external_assets(self):
        # The page must work on a LAN with no internet: no
        # external script, style, font, or image references.
        self.assertNotIn("<script src", DECK_HTML)
        self.assertNotIn("<link", DECK_HTML)
        self.assertNotIn("http://", DECK_HTML)
        self.assertNotIn("https://", DECK_HTML)

    @unittest.skipUnless(shutil.which("node"),
                         "node is not on this box")
    def test_served_script_parses(self):
        script = _script_of(DECK_HTML)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deck-script.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run(
                ["node", "--check", str(path)],
                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)


class _FakeSocket:
    """A datagram socket stand-in for detect_lan_ip: records
    the connect, answers getsockname with a fixed address (or
    fails the connect, when address is None)."""

    def __init__(self, address):
        self._address = address
        self.connected_to = None

    def connect(self, target):
        if self._address is None:
            raise OSError("no route")
        self.connected_to = target

    def getsockname(self):
        return (self._address, 54321)

    def close(self):
        pass


class LanAddressTests(unittest.TestCase):
    def test_udp_route_address_is_used(self):
        fake = _FakeSocket("192.168.1.42")
        with mock.patch.object(socket, "socket",
                               lambda *a, **k: fake):
            self.assertEqual(detect_lan_ip(), "192.168.1.42")
        self.assertEqual(fake.connected_to[1], 80)

    def test_loopback_answers_are_rejected(self):
        fake = _FakeSocket("127.0.0.1")
        with mock.patch.object(socket, "socket",
                               lambda *a, **k: fake), \
                mock.patch.object(
                    socket, "gethostbyname",
                    lambda name: "127.0.0.1"), \
                mock.patch.object(
                    socket, "gethostname", lambda: "pc"):
            self.assertIsNone(detect_lan_ip())

    def test_no_route_and_no_name_is_none(self):
        fake = _FakeSocket(None)
        with mock.patch.object(socket, "socket",
                               lambda *a, **k: fake), \
                mock.patch.object(
                    socket, "gethostbyname",
                    side_effect=OSError("no name")):
            self.assertIsNone(detect_lan_ip())

    def test_deck_url_shape(self):
        self.assertEqual(
            deck_url(11434, ip="192.168.1.42"),
            "http://192.168.1.42:11434/deck")
        self.assertEqual(
            deck_url(9999, ip="10.0.0.7"),
            "http://10.0.0.7:9999/deck")

    def test_deck_url_none_without_an_address(self):
        with mock.patch("fyrn.bridge.detect_lan_ip",
                        lambda: None):
            self.assertIsNone(deck_url(11434))


class QrCodegenTests(unittest.TestCase):
    def test_vendored_library_header(self):
        source = Path(qrcodegen.__file__).read_text(
            encoding="utf-8")
        self.assertIn("Project Nayuki", source)
        self.assertIn("MIT License", source)

    def test_encodes_a_deck_url(self):
        url = "http://192.168.1.42:11434/deck"
        qr = qrcodegen.QrCode.encode_text(
            url, qrcodegen.QrCode.Ecc.MEDIUM)
        size = qr.get_size()
        self.assertGreaterEqual(size, 21)
        # The three finder patterns' corners are dark.
        self.assertTrue(qr.get_module(0, 0))
        self.assertTrue(qr.get_module(size - 1, 0))
        self.assertTrue(qr.get_module(0, size - 1))
        # And the encoding is deterministic.
        again = qrcodegen.QrCode.encode_text(
            url, qrcodegen.QrCode.Ecc.MEDIUM)
        self.assertEqual(again.get_size(), size)
        for y in range(size):
            for x in range(size):
                self.assertEqual(qr.get_module(x, y),
                                 again.get_module(x, y))


if __name__ == "__main__":
    unittest.main()
