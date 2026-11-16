"""Pulling models: download a GGUF file into the models folder.

One input, two shapes: a Hugging Face model (user/repo plus the
.gguf file name) or a direct .gguf link. Downloads are resumable
(U24): a failed or frozen pull picks up where it stopped instead of
starting over. Standard library only.
"""

import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

CHUNK_SIZE = 1024 * 1024  # 1 MB reads

_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")


class DownloadError(Exception):
    """A pull failed, in plain words."""


class PullCancelled(Exception):
    """The user cancelled a pull. The partial file stays for resume."""


def format_eta(seconds):
    """Time remaining in plain words: "about 40 s left",
    "about 3 min left", "about 1 h 20 min left". None when there
    is no time to estimate (no speed yet, or nothing left)."""
    if seconds is None:
        return None
    seconds = int(seconds)
    if seconds <= 0:
        return None
    if seconds < 10:
        return "less than 10 seconds left"
    if seconds < 60:
        return "about %d s left" % seconds
    minutes = seconds // 60
    if minutes < 60:
        return "about %d min left" % minutes
    hours = minutes // 60
    rest = minutes % 60
    if rest:
        return "about %d h %d min left" % (hours, rest)
    return "about %d h left" % hours


class DownloadTarget:
    """A parsed pull request: a URL and the file name to save."""

    def __init__(self, url, filename):
        self.url = url
        self.filename = filename

    def __repr__(self):
        return "DownloadTarget(%r, %r)" % (self.url, self.filename)


def _filename_from_url(url):
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1]
    return name


def parse_model_input(text):
    """Turn what he typed into a download target.

    Accepted shapes:
    - a direct link ending in .gguf
    - a Hugging Face link to a .gguf file (blob links are converted
      to download links)
    - user/repo plus a file name, separated by a space or a colon:
      myname/mymodel mymodel.gguf
    Raises ValueError with a plain-words message otherwise.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Type a model first.")
    if text.startswith("http://") or text.startswith("https://"):
        url = text
        if "huggingface.co" in url and "/blob/" in url:
            url = url.replace("/blob/", "/resolve/", 1)
        name = _filename_from_url(url)
        if not name.lower().endswith(".gguf"):
            raise ValueError(
                "That link does not point to a .gguf model file.")
        return DownloadTarget(url, name)
    # user/repo plus file name, split on a space or a colon.
    if " " in text:
        repo, filename = text.rsplit(None, 1)
    elif ":" in text:
        repo, filename = text.rsplit(":", 1)
    else:
        repo, filename = text, ""
    repo = repo.strip()
    filename = filename.strip()
    if _REPO_RE.match(repo) and not filename:
        raise ValueError(
            "I need the file name too. Type it like: %s file-name.gguf"
            % repo)
    if _REPO_RE.match(repo) and filename.lower().endswith(".gguf"):
        url = ("https://huggingface.co/%s/resolve/main/%s"
               % (repo, filename))
        return DownloadTarget(url, filename)
    raise ValueError(
        "I could not understand that. Type a Hugging Face model like "
        "user/model-name file.gguf, or paste a direct .gguf link.")


class _EtagChanged(Exception):
    """The file upstream changed since the partial download: the
    partial bytes belong to the old file, so the download must
    start over instead of resuming."""


def _drop_quietly(path):
    """Delete a file if it is there; a missing file is fine."""
    try:
        os.remove(path)
    except OSError:
        pass


def _read_sidecar(path):
    """A sidecar file's text, stripped, or None when it is missing
    or unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _write_sidecar(path, text):
    """Write a sidecar file. A failure is ignored: the sidecar is
    a convenience for resume, never a reason to fail a download."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


def read_part_url(part_path):
    """The address a partial download came from, saved alongside
    it, or None when it was not saved (a partial file from before
    this version, or one placed by hand)."""
    return _read_sidecar(str(part_path) + ".url")


def parse_hf_url(url):
    """The Hugging Face repo and file for a download URL, or None
    when the URL is not a Hugging Face file link."""
    parsed = urlparse(url)
    if parsed.netloc.lower() != "huggingface.co":
        return None
    parts = [piece for piece in parsed.path.split("/") if piece]
    # huggingface.co/user/repo/resolve/rev/file.gguf
    if len(parts) < 5 or parts[2] != "resolve":
        return None
    return ("%s/%s" % (parts[0], parts[1]), "/".join(parts[4:]))


def fetch_hf_sha256(repo, filename,
                    api_base="https://huggingface.co", opener=None):
    """The SHA-256 Hugging Face publishes for a file, from the Hub
    API's model page (the LFS oid for the file). Returns None when
    the hash cannot be found: no hash means no verification, never
    a failed pull."""
    url = "%s/api/models/%s" % (api_base.rstrip("/"), repo)
    open_url = opener or urllib.request.urlopen
    try:
        response = open_url(url, timeout=15)
        try:
            data = json.loads(response.read().decode("utf-8"))
        finally:
            try:
                response.close()
            except Exception:
                pass
    except Exception:
        return None
    for sibling in data.get("siblings") or []:
        if sibling.get("rfilename") == filename:
            lfs = sibling.get("lfs") or {}
            oid = lfs.get("oid")
            if oid:
                return oid
    return None


def _sha256_file(path):
    """The SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _verify_hash(part_path, url_sidecar, etag_sidecar,
                 expected_sha256):
    """Check a completed download against its published hash.
    Nothing to check when no hash was given. A mismatch deletes
    the download and its sidecars and raises DownloadError, so a
    corrupt file never becomes a model."""
    if expected_sha256 is None:
        return
    try:
        actual = _sha256_file(part_path)
    except OSError as exc:
        raise DownloadError(
            "The download could not be checked (%s)." % exc)
    if actual.lower() != expected_sha256.strip().lower():
        _drop_quietly(part_path)
        _drop_quietly(url_sidecar)
        _drop_quietly(etag_sidecar)
        raise DownloadError(
            "The downloaded file does not match the one "
            "published (its checksum differs). It was deleted, "
            "not saved as a model. Try pulling it again.")


def download_file(url, dest, progress_cb=None, cancel_event=None,
                  opener=None, expected_sha256=None):
    """Download a URL to dest, resuming a partial file if there is one.

    The download lands in dest + ".part" and is moved into place only
    when complete, so a half-downloaded file never looks like a
    model. progress_cb(downloaded, total_or_None, speed_bytes_per_s)
    is called after every chunk. cancel_event stops the download and
    keeps the partial file for the next try.

    Two sidecars travel with the partial file: dest + ".part.url"
    holds the address it came from (so it can be resumed later
    without retyping it), and dest + ".part.etag" holds the
    server's ETag for the file (so a resume against a changed
    upstream file starts over instead of producing a corrupt
    model). Both are removed when the download completes.

    expected_sha256, when given, is checked against the completed
    file before it is moved into place: a mismatch deletes the
    download and raises DownloadError, so a corrupt file never
    becomes a "model" that fails confusingly at load.

    Returns dest. Raises DownloadError or PullCancelled.
    """
    dest = str(dest)
    part_path = dest + ".part"
    url_sidecar = part_path + ".url"
    etag_sidecar = part_path + ".etag"
    _write_sidecar(url_sidecar, url)
    restarted = False
    while True:
        try:
            return _download_once(
                url, dest, part_path, url_sidecar, etag_sidecar,
                progress_cb, cancel_event, opener, expected_sha256)
        except _EtagChanged:
            if restarted:
                # The fresh attempt cannot resume, so it cannot
                # raise this; if it ever did, refuse rather than
                # loop forever.
                raise DownloadError(
                    "The download could not start.")
            restarted = True
            _drop_quietly(part_path)
            _drop_quietly(url_sidecar)
            _drop_quietly(etag_sidecar)
            _write_sidecar(url_sidecar, url)


def _download_once(url, dest, part_path, url_sidecar, etag_sidecar,
                   progress_cb, cancel_event, opener,
                   expected_sha256):
    """One download attempt. Raises _EtagChanged when a resume
    meets an upstream file that changed since the partial file."""
    existing = 0
    if os.path.exists(part_path):
        existing = os.path.getsize(part_path)
    stored_etag = (_read_sidecar(etag_sidecar)
                   if existing > 0 else None)
    request = urllib.request.Request(url)
    if existing > 0:
        request.add_header("Range", "bytes=%d-" % existing)
    open_url = opener or urllib.request.urlopen
    try:
        response = open_url(request, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code == 416:
            # The partial file already covers the whole file.
            _verify_hash(part_path, url_sidecar, etag_sidecar,
                         expected_sha256)
            os.replace(part_path, dest)
            _drop_quietly(url_sidecar)
            _drop_quietly(etag_sidecar)
            return dest
        if exc.code == 404:
            raise DownloadError(
                "That model was not found. Check the name and try "
                "again.")
        raise DownloadError(
            "The download failed (server said HTTP %s)." % exc.code)
    except urllib.error.URLError as exc:
        raise DownloadError(
            "The download could not start (%s). Check the network "
            "connection and try again." % exc.reason)
    except http.client.HTTPException as exc:
        raise DownloadError(
            "The download could not start (%s). Check the network "
            "connection and try again." % exc)
    except OSError as exc:
        raise DownloadError(
            "The download could not start (%s)." % exc)

    status = getattr(response, "status", 200)
    resumed = (status == 206)
    # The upstream file changed since the partial download: the
    # partial bytes belong to the old file. Start over, don't mix.
    etag = response.headers.get("ETag")
    if resumed and stored_etag and etag and stored_etag != etag:
        try:
            response.close()
        except Exception:
            pass
        raise _EtagChanged()
    if etag:
        _write_sidecar(etag_sidecar, etag)
    total = None
    content_range = response.headers.get("Content-Range")
    if content_range and "/" in content_range:
        try:
            total = int(content_range.rsplit("/", 1)[1])
        except ValueError:
            total = None
    if total is None:
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                total = int(length) + (existing if resumed else 0)
            except ValueError:
                total = None
    mode = "ab" if resumed else "wb"
    downloaded = existing if resumed else 0
    started = time.monotonic()
    try:
        with open(part_path, mode) as fh:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise PullCancelled("Pull cancelled.")
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)
                if progress_cb is not None:
                    elapsed = time.monotonic() - started
                    speed = downloaded / elapsed if elapsed > 0 else 0
                    progress_cb(downloaded, total, speed)
    except PullCancelled:
        raise
    except http.client.HTTPException as exc:
        # A stream that ends early raises IncompleteRead, which is
        # an HTTPException, not an OSError. It is a dropped
        # connection, and the partial file stays for the next try.
        raise DownloadError(
            "The connection dropped during the download (%s). Try "
            "again and it will pick up where it stopped." % exc)
    except OSError as exc:
        raise DownloadError(
            "The download failed while saving (%s). Check disk "
            "space and try again." % exc)
    finally:
        try:
            response.close()
        except Exception:
            pass
    if total is not None and downloaded != total:
        raise DownloadError(
            "The download stopped early (%d of %d bytes). Try again "
            "and it will pick up where it stopped."
            % (downloaded, total))
    _verify_hash(part_path, url_sidecar, etag_sidecar,
                 expected_sha256)
    os.replace(part_path, dest)
    _drop_quietly(url_sidecar)
    _drop_quietly(etag_sidecar)
    return dest
