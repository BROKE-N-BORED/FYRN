"""Tests for fyrn.pull: input parsing and resumable
downloads against a local fake HTTP server (no network)."""

import hashlib
import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from helpers import file_bytes, make_fake_gguf, start_range_server
from fyrn.pull import (DownloadError, PullCancelled, download_file,
                             fetch_hf_sha256, format_eta, parse_hf_url,
                             parse_model_input, read_part_url)

PAYLOAD = bytes(range(256)) * 256  # 64 KiB deterministic bytes


class ParseInputTests(unittest.TestCase):
    def test_direct_url(self):
        target = parse_model_input(
            "https://example.com/models/cool.gguf")
        self.assertEqual(target.url,
                         "https://example.com/models/cool.gguf")
        self.assertEqual(target.filename, "cool.gguf")

    def test_huggingface_blob_link_becomes_download_link(self):
        target = parse_model_input(
            "https://huggingface.co/user/model/blob/main/model.gguf")
        self.assertEqual(
            target.url,
            "https://huggingface.co/user/model/resolve/main/model.gguf")
        self.assertEqual(target.filename, "model.gguf")

    def test_huggingface_resolve_link(self):
        target = parse_model_input(
            "https://huggingface.co/user/model/resolve/main/m.gguf")
        self.assertEqual(target.filename, "m.gguf")

    def test_user_repo_plus_file_space(self):
        target = parse_model_input("myname/mymodel mymodel.gguf")
        self.assertEqual(
            target.url,
            "https://huggingface.co/myname/mymodel/resolve/main/"
            "mymodel.gguf")
        self.assertEqual(target.filename, "mymodel.gguf")

    def test_user_repo_plus_file_colon(self):
        target = parse_model_input("myname/mymodel:mymodel.gguf")
        self.assertEqual(target.filename, "mymodel.gguf")

    def test_empty_input(self):
        with self.assertRaises(ValueError):
            parse_model_input("   ")

    def test_repo_without_file_asks_for_it(self):
        with self.assertRaises(ValueError) as ctx:
            parse_model_input("myname/mymodel")
        self.assertIn("file name", str(ctx.exception))

    def test_non_gguf_link(self):
        with self.assertRaises(ValueError):
            parse_model_input("https://example.com/models/readme.md")

    def test_garbage(self):
        with self.assertRaises(ValueError):
            parse_model_input("just some words here")


class TruncatingHandler(BaseHTTPRequestHandler):
    """Sends a full Content-Length, then closes after half the body
    — a dropped connection mid-download."""

    PAYLOAD = b""

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = self.PAYLOAD[:len(self.PAYLOAD) // 2]
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.PAYLOAD)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class NotFoundHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()


def _start(handler_cls, payload=b""):
    handler = type("Bound", (handler_cls,), {"PAYLOAD": payload})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:%d/model.gguf" % server.server_address[1]
    return server, url


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "model.gguf"
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def _track(self, server):
        self.servers.append(server)
        return server

    def test_full_download_byte_identical(self):
        server, url = start_range_server(PAYLOAD)
        self._track(server)
        progress = []
        result = download_file(
            url, self.dest,
            progress_cb=lambda d, t, s: progress.append((d, t, s)))
        self.assertEqual(result, str(self.dest))
        self.assertEqual(file_bytes(self.dest), PAYLOAD)
        self.assertFalse(os.path.exists(str(self.dest) + ".part"))
        self.assertTrue(progress)
        downloaded, total, speed = progress[-1]
        self.assertEqual(downloaded, len(PAYLOAD))
        self.assertEqual(total, len(PAYLOAD))
        self.assertGreaterEqual(speed, 0)

    def test_resume_after_partial(self):
        server, url = start_range_server(PAYLOAD)
        self._track(server)
        part = str(self.dest) + ".part"
        with open(part, "wb") as fh:
            fh.write(PAYLOAD[:len(PAYLOAD) // 2])
        download_file(url, self.dest)
        self.assertEqual(file_bytes(self.dest), PAYLOAD)
        self.assertFalse(os.path.exists(part))

    def test_server_ignoring_range_restarts_cleanly(self):
        server, url = start_range_server(PAYLOAD, support_range=False)
        self._track(server)
        part = str(self.dest) + ".part"
        with open(part, "wb") as fh:
            fh.write(b"garbage-bytes")
        download_file(url, self.dest)
        self.assertEqual(file_bytes(self.dest), PAYLOAD)

    def test_dropped_connection_then_resume(self):
        bad, url = _start(TruncatingHandler, PAYLOAD)
        self._track(bad)
        with self.assertRaises(DownloadError):
            download_file(url, self.dest)
        part = str(self.dest) + ".part"
        self.assertTrue(os.path.exists(part))
        self.assertEqual(os.path.getsize(part), len(PAYLOAD) // 2)
        good, url2 = start_range_server(PAYLOAD)
        self._track(good)
        download_file(url2, self.dest)
        self.assertEqual(file_bytes(self.dest), PAYLOAD)

    def test_not_found_is_plain_words(self):
        server, url = _start(NotFoundHandler)
        self._track(server)
        with self.assertRaises(DownloadError) as ctx:
            download_file(url, self.dest)
        self.assertIn("not found", str(ctx.exception))

    def test_cancel_keeps_partial_for_resume(self):
        server, url = start_range_server(PAYLOAD)
        self._track(server)
        event = threading.Event()
        event.set()
        with self.assertRaises(PullCancelled):
            download_file(url, self.dest, cancel_event=event)
        part = str(self.dest) + ".part"
        self.assertTrue(os.path.exists(part))
        # And the resume after a cancel completes.
        download_file(url, self.dest)
        self.assertEqual(file_bytes(self.dest), PAYLOAD)

    def test_already_complete_partial_is_adopted(self):
        server, url = start_range_server(PAYLOAD)
        self._track(server)
        part = str(self.dest) + ".part"
        with open(part, "wb") as fh:
            fh.write(PAYLOAD)
        download_file(url, self.dest)
        self.assertEqual(file_bytes(self.dest), PAYLOAD)
        self.assertFalse(os.path.exists(part))

    def test_fake_gguf_helper(self):
        path = make_fake_gguf(Path(self.tmp.name) / "a.gguf", 2048)
        self.assertEqual(os.path.getsize(path), 2048)
        self.assertEqual(file_bytes(path)[:4], b"\x00\x01\x02\x03")


class HfUrlTests(unittest.TestCase):
    def test_resolve_link_parses(self):
        self.assertEqual(
            parse_hf_url("https://huggingface.co/user/model/"
                         "resolve/main/m.gguf"),
            ("user/model", "m.gguf"))

    def test_non_hf_link_is_none(self):
        self.assertIsNone(
            parse_hf_url("https://example.com/models/m.gguf"))

    def test_blob_link_is_none(self):
        # Blob links are converted to resolve links by
        # parse_model_input before a pull ever starts.
        self.assertIsNone(
            parse_hf_url("https://huggingface.co/user/model/"
                         "blob/main/m.gguf"))


class ApiHandler(BaseHTTPRequestHandler):
    """Serves one fixed Hub API model page."""

    BODY = b"{}"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.BODY)))
        self.end_headers()
        self.wfile.write(self.BODY)


class FetchHashTests(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def _api(self, body):
        handler = type("BoundApi", (ApiHandler,),
                       {"BODY": json.dumps(body).encode("utf-8")})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever,
                                  daemon=True)
        thread.start()
        self.servers.append(server)
        return "http://127.0.0.1:%d" % server.server_address[1]

    def test_hash_found_for_the_file(self):
        base = self._api({"siblings": [
            {"rfilename": "other.gguf",
             "lfs": {"oid": "deadbeef"}},
            {"rfilename": "m.gguf",
             "lfs": {"oid": "abc123"}}]})
        self.assertEqual(fetch_hf_sha256("user/model", "m.gguf",
                                         api_base=base),
                         "abc123")

    def test_missing_file_is_none(self):
        base = self._api({"siblings": [
            {"rfilename": "other.gguf",
             "lfs": {"oid": "deadbeef"}}]})
        self.assertIsNone(fetch_hf_sha256("user/model", "m.gguf",
                                          api_base=base))

    def test_dead_server_is_none_not_an_error(self):
        self.assertIsNone(fetch_hf_sha256(
            "user/model", "m.gguf",
            api_base="http://127.0.0.1:1"))


class HashVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "model.gguf"
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def test_matching_hash_downloads(self):
        server, url = start_range_server(PAYLOAD)
        self.servers.append(server)
        digest = hashlib.sha256(PAYLOAD).hexdigest()
        download_file(url, self.dest, expected_sha256=digest)
        self.assertEqual(file_bytes(self.dest), PAYLOAD)

    def test_mismatched_hash_deletes_and_fails(self):
        server, url = start_range_server(PAYLOAD)
        self.servers.append(server)
        with self.assertRaises(DownloadError) as ctx:
            download_file(url, self.dest,
                          expected_sha256="0" * 64)
        self.assertIn("does not match", str(ctx.exception))
        self.assertFalse(self.dest.exists())
        self.assertFalse(
            os.path.exists(str(self.dest) + ".part"))


class EtagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "model.gguf"
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def _stale_part(self, etag):
        """A partial file of garbage bytes with a stored ETag."""
        part = str(self.dest) + ".part"
        with open(part, "wb") as fh:
            fh.write(b"garbage-bytes" * 100)
        with open(part + ".etag", "w", encoding="utf-8") as fh:
            fh.write(etag)

    def test_changed_upstream_restarts_from_zero(self):
        server, url = start_range_server(PAYLOAD, etag="v2")
        self.servers.append(server)
        self._stale_part("v1")
        download_file(url, self.dest)
        # A resume would have kept the garbage prefix; a fresh
        # download is byte-identical to the payload.
        self.assertEqual(file_bytes(self.dest), PAYLOAD)

    def test_matching_etag_resumes(self):
        server, url = start_range_server(PAYLOAD, etag="v1")
        self.servers.append(server)
        part = str(self.dest) + ".part"
        with open(part, "wb") as fh:
            fh.write(PAYLOAD[:len(PAYLOAD) // 2])
        with open(part + ".etag", "w", encoding="utf-8") as fh:
            fh.write("v1")
        download_file(url, self.dest)
        self.assertEqual(file_bytes(self.dest), PAYLOAD)

    def test_etag_is_saved_for_the_next_resume(self):
        server, url = start_range_server(PAYLOAD, etag="v9")
        self.servers.append(server)
        event = threading.Event()
        event.set()
        with self.assertRaises(PullCancelled):
            download_file(url, self.dest, cancel_event=event)
        with open(str(self.dest) + ".part.etag",
                  encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "v9")


class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "model.gguf"
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def test_url_sidecar_survives_cancel_and_dies_on_success(self):
        server, url = start_range_server(PAYLOAD)
        self.servers.append(server)
        event = threading.Event()
        event.set()
        with self.assertRaises(PullCancelled):
            download_file(url, self.dest, cancel_event=event)
        part = str(self.dest) + ".part"
        self.assertEqual(read_part_url(part), url)
        download_file(url, self.dest)
        self.assertEqual(file_bytes(self.dest), PAYLOAD)
        self.assertFalse(os.path.exists(part + ".url"))
        self.assertIsNone(read_part_url(part))


class FormatEtaTests(unittest.TestCase):
    def test_none_and_non_positive_are_none(self):
        self.assertIsNone(format_eta(None))
        self.assertIsNone(format_eta(0))
        self.assertIsNone(format_eta(-5))

    def test_under_ten_seconds(self):
        self.assertEqual(format_eta(3),
                         "less than 10 seconds left")
        self.assertEqual(format_eta(9.9),
                         "less than 10 seconds left")

    def test_seconds(self):
        self.assertEqual(format_eta(10), "about 10 s left")
        self.assertEqual(format_eta(45), "about 45 s left")

    def test_minutes(self):
        self.assertEqual(format_eta(60), "about 1 min left")
        self.assertEqual(format_eta(150), "about 2 min left")
        self.assertEqual(format_eta(3540), "about 59 min left")

    def test_hours(self):
        self.assertEqual(format_eta(3600), "about 1 h left")
        self.assertEqual(format_eta(5400), "about 1 h 30 min left")
        self.assertEqual(format_eta(7200), "about 2 h left")


if __name__ == "__main__":
    unittest.main()
