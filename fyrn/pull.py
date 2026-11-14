"""Pulling models: download a GGUF file into the models folder.

One input, two shapes: a Hugging Face model (user/repo plus the
.gguf file name) or a direct .gguf link. Downloads are resumable
(U24): a failed or frozen pull picks up where it stopped instead of
starting over. Standard library only.
"""

import http.client
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


def download_file(url, dest, progress_cb=None, cancel_event=None,
                  opener=None):
    """Download a URL to dest, resuming a partial file if there is one.

    The download lands in dest + ".part" and is moved into place only
    when complete, so a half-downloaded file never looks like a
    model. progress_cb(downloaded, total_or_None, speed_bytes_per_s)
    is called after every chunk. cancel_event stops the download and
    keeps the partial file for the next try.

    Returns dest. Raises DownloadError or PullCancelled.
    """
    dest = str(dest)
    part_path = dest + ".part"
    existing = 0
    if os.path.exists(part_path):
        existing = os.path.getsize(part_path)
    request = urllib.request.Request(url)
    if existing > 0:
        request.add_header("Range", "bytes=%d-" % existing)
    open_url = opener or urllib.request.urlopen
    try:
        response = open_url(request, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code == 416:
            # The partial file already covers the whole file.
            os.replace(part_path, dest)
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
    os.replace(part_path, dest)
    return dest
