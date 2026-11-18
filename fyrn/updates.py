"""Self-update: check GitHub for a newer Fyrn release, download
it, verify it, and install it (v0.6).

The flow, in the order the user sees it:

1. On launch (and from Help, Check for updates), check_for_update
   asks GitHub's public releases endpoint for the latest Fyrn
   release. The request is a plain GET to a public endpoint: it
   sends no identifying information and needs no token. Up to
   date, or any failure, means nothing happens at all (the
   startup check is silent; a manual check says so).
2. A newer release pops up and asks. Nothing downloads until the
   user taps to update.
3. The download fetches the release zip AND its checksum sidecar
   (fyrn-vX.Y.zip.sha256, shipped from v0.6 on). The zip's
   SHA-256 is verified against the sidecar BEFORE anything is
   touched: a mismatch means nothing is installed and the old
   version keeps running.
4. Once verified and pulled, the app asks "restart now to
   complete the update, yes or no". Yes installs the verified
   zip over the install folder and restarts on its own. No stages
   the update; it applies at the next launch, re-verified first.
5. The update replaces PROGRAM files only: the zip carries no
   models, settings, or venv, so none of those are ever touched.
   Files the update replaces are backed up first, and a failed
   install restores them, so a failed or interrupted install
   leaves the old version runnable.

Two things the update does NOT do: it does not touch the
launcher in ~/.local/bin/fyrn or the desktop files (they point
at the install folder, which does not move, so they keep
working), and it never removes files the new release dropped
(it replaces and adds only; a file a release deletes stays
behind, harmlessly).

This module is stdlib-only and Qt-free: __main__ applies a
staged update before Qt is imported, so the new version is what
actually launches, and the second-launch fast path never pays
for this import.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = "BROKE-N-BORED/FYRN"
LATEST_RELEASE_URL = ("https://api.github.com/repos/%s/releases/latest"
                      % REPO)
RELEASES_PAGE_URL = "https://github.com/%s/releases" % REPO
CHECK_TIMEOUT_S = 8
SIDECAR_TIMEOUT_S = 30

# The zip's entries all start with this prefix (build_zip.py);
# it is stripped when the update is applied.
ZIP_PREFIX = "fyrn/"
STAGED_ZIP_NAME = "update.zip"
STAGED_SIDECAR_NAME = "update.zip.sha256"
PENDING_FILE_NAME = "pending.json"

_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)$")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


class UpdateError(Exception):
    """The update could not be checked, downloaded, or applied,
    in plain words."""


def parse_version(text):
    """A version string as a tuple of ints: "0.6" becomes
    (0, 6), "v0.6" the same. None when it is not a version at
    all. Anything but an optional "v" followed by numbers and
    dots is refused, so a tag like "v0.6-beta" reads as no usable
    version rather than a wrong one."""
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.match(text.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(latest, current):
    """True when the latest version is newer than the running
    one. Either side unparsable means no: an unknown version is
    never reported as an update. Shorter tuples are padded with
    zeros, so 0.6 and 0.6.0 compare equal."""
    latest_parsed = parse_version(latest)
    current_parsed = parse_version(current)
    if latest_parsed is None or current_parsed is None:
        return False
    width = max(len(latest_parsed), len(current_parsed))
    latest_padded = latest_parsed + (0,) * (width - len(latest_parsed))
    current_padded = current_parsed + (0,) * (width - len(current_parsed))
    return latest_padded > current_padded


def _asset_url(assets, suffix):
    """(name, download URL) of the first release asset whose name
    ends with suffix, or (None, None)."""
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name") or ""
        if name.endswith(suffix):
            return name, asset.get("browser_download_url")
    return None, None


def fetch_latest_release(urlopen=None):
    """The latest release from GitHub's public endpoint, as
    {"version", "url", "zip_url", "zip_name", "sha256_url"}.
    sha256_url is None when the release carries no checksum
    sidecar (releases before v0.6); the download step refuses
    those, since an update without a checksum cannot be
    verified. Raises UpdateError with a plain reason when the
    network fails, the answer is not JSON, it carries no tag,
    or it has no zip to download. urlopen is a test seam: pass
    a stub and no network is touched."""
    opener = urlopen or urllib.request.urlopen
    try:
        with opener(LATEST_RELEASE_URL,
                    timeout=CHECK_TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise UpdateError("the update check timed out")
        raise UpdateError("could not reach GitHub: %s" % reason)
    except TimeoutError:
        raise UpdateError("the update check timed out")
    except (ValueError, UnicodeDecodeError):
        raise UpdateError(
            "the releases page did not answer in a form "
            "Fyrn understands")
    if not isinstance(payload, dict):
        raise UpdateError(
            "the releases page did not answer in a form "
            "Fyrn understands")
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise UpdateError(
            "the releases page did not name a version")
    version = parse_version(tag)
    if version is None:
        raise UpdateError(
            "the latest release tag did not look like a version: %s"
            % tag)
    url = payload.get("html_url") or RELEASES_PAGE_URL
    assets = payload.get("assets")
    zip_name, zip_url = _asset_url(assets, ".zip")
    if not zip_url:
        raise UpdateError("the latest release has no download")
    _sidecar_name, sha256_url = _asset_url(assets, ".zip.sha256")
    return {"version": ".".join(str(part) for part in version),
            "url": url,
            "zip_url": zip_url,
            "zip_name": zip_name,
            "sha256_url": sha256_url}


def check_for_update(current_version, urlopen=None):
    """One call, three outcomes, never an exception for a
    network or parsing problem:

    {"state": "newer", "latest": "0.7", "url": ...,
     "zip_url": ..., "zip_name": ..., "sha256_url": ...} — a
     newer release is out.
    {"state": "current", ...} — the running version is the
     newest (or newer than the latest release, a dev tree).
    {"state": "unknown", "latest": None, ...,
     "reason": "<plain words>"} — the check could not run.
    """
    try:
        release = fetch_latest_release(urlopen=urlopen)
    except UpdateError as exc:
        return {"state": "unknown", "latest": None, "url": None,
                "zip_url": None, "zip_name": None,
                "sha256_url": None, "reason": str(exc)}
    result = {"latest": release["version"], "url": release["url"],
              "zip_url": release["zip_url"],
              "zip_name": release["zip_name"],
              "sha256_url": release["sha256_url"]}
    if is_newer(release["version"], current_version):
        result["state"] = "newer"
    else:
        result["state"] = "current"
    return result


def fetch_sidecar(url, urlopen=None):
    """The checksum sidecar's text. Raises UpdateError with a
    plain reason when it cannot be fetched."""
    opener = urlopen or urllib.request.urlopen
    try:
        with opener(url, timeout=SIDECAR_TIMEOUT_S) as response:
            return response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise UpdateError(
            "the update's checksum could not be fetched: %s"
            % exc.reason)
    except TimeoutError:
        raise UpdateError(
            "the update's checksum fetch timed out")
    except UnicodeDecodeError:
        raise UpdateError(
            "the update's checksum could not be read")


def parse_sidecar(text, zip_name=None):
    """The SHA-256 from a sha256sum-format sidecar
    ("<hash>  <file>"), or None when it cannot be read. When
    zip_name is given, only the line naming that file counts:
    a sidecar for another file is no verification at all."""
    if not isinstance(text, str):
        return None
    first = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        if not _SHA256_RE.fullmatch(parts[0]):
            continue
        if first is None:
            first = parts[0].lower()
        if zip_name is not None:
            name = parts[1].lstrip("*")
            if name == zip_name or name.endswith("/" + zip_name):
                return parts[0].lower()
    if zip_name is None:
        return first
    return None


def sha256_file(path):
    """The SHA-256 of a file, read in 1 MB chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path, expected_sha256):
    """True when the file's SHA-256 matches the expected one.
    A missing or unreadable file never verifies."""
    if not isinstance(expected_sha256, str):
        return False
    try:
        actual = sha256_file(path)
    except OSError:
        return False
    return actual.lower() == expected_sha256.strip().lower()


def staging_dir(home):
    """Where a verified update waits for its restart."""
    return Path(home) / "updates" / "pending"


def pending_update(home):
    """The staged update's record ({"version", "zip_sha256",
    "zip_name"}), or None when nothing is staged. A damaged
    record reads as nothing staged: a half-written staging
    never blocks a launch."""
    record_path = staging_dir(home) / PENDING_FILE_NAME
    try:
        with open(record_path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    if "version" not in record or "zip_sha256" not in record:
        return None
    return record


def stage_update(home, zip_path, sidecar_text, version, zip_sha256):
    """Record a verified download as the staged update: the zip
    moves into the staging dir with its sidecar and a record of
    the version and checksum. Any previous staging is replaced.
    The zip must live OUTSIDE the staging dir (the download
    worker puts it next to it), since staging clears the dir
    first."""
    staged = staging_dir(home)
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True)
    dest = staged / STAGED_ZIP_NAME
    shutil.move(str(zip_path), str(dest))
    with open(staged / STAGED_SIDECAR_NAME, "w",
              encoding="utf-8") as fh:
        fh.write(sidecar_text)
    record = {"version": version, "zip_sha256": zip_sha256,
              "zip_name": STAGED_ZIP_NAME}
    with open(staged / PENDING_FILE_NAME, "w",
              encoding="utf-8") as fh:
        json.dump(record, fh)
    return staged


def clear_staged_update(home):
    """Drop a staged update (after it applied, or because it
    failed verification and must never be retried)."""
    staged = staging_dir(home)
    if staged.exists():
        shutil.rmtree(staged)


def install_dir_from_here():
    """The folder Fyrn is running from: the parent of the fyrn
    package."""
    return Path(__file__).resolve().parent.parent


def _safe_zip_members(zf):
    """[(ZipInfo, target-relative-path)] for every file in the
    zip, with the fyrn/ prefix stripped. Refuses absolute paths
    and ".." escapes (zip slip) and entries outside the prefix:
    an update package that is not shaped like Fyrn is not
    installed."""
    members = []
    for info in zf.infolist():
        name = info.filename
        if name.endswith("/"):
            continue
        if not name.startswith(ZIP_PREFIX):
            raise UpdateError(
                "the update package is not shaped like Fyrn "
                "(bad entry: %s)" % name)
        rel = name[len(ZIP_PREFIX):]
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise UpdateError(
                "the update package has an unsafe entry: %s" % name)
        members.append((info, rel))
    if not members:
        raise UpdateError("the update package is empty")
    return members


def apply_pending_update(home, install_dir=None):
    """Apply the staged update over the install folder.

    The staged zip's checksum is verified again first: a
    mismatch means nothing is installed and the staging is
    dropped, so a corrupt download never retries forever. Files
    the update replaces are backed up first (program files only:
    the zip carries no models, settings, or venv, so none of
    those are ever touched); a failed install restores them and
    removes the files the update added, so the old version stays
    runnable.

    Returns (True, version) on success (the staging is consumed)
    or (False, plain reason) on any failure.
    """
    home = Path(home)
    if install_dir is None:
        install_dir = install_dir_from_here()
    else:
        install_dir = Path(install_dir)
    record = pending_update(home)
    if record is None:
        return False, "No update is staged."
    staged = staging_dir(home)
    zip_path = staged / STAGED_ZIP_NAME
    if not zip_path.is_file():
        clear_staged_update(home)
        return False, "The staged update is missing its download."
    if not verify_file(zip_path, record["zip_sha256"]):
        clear_staged_update(home)
        return False, ("The staged update failed its checksum, so "
                       "it was not installed.")
    if (not (install_dir / "fyrn" / "__init__.py").is_file()
            or not (install_dir / "install.sh").is_file()):
        return False, ("Fyrn is not running from an installed "
                       "copy, so the update was not installed.")
    backup_dir = Path(home) / "updates" / "backup"
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = _safe_zip_members(zf)
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            backup_dir.mkdir(parents=True)
            replaced = []
            added = []
            for _info, rel in members:
                target = install_dir / rel
                if target.is_file():
                    dest = backup_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, dest)
                    replaced.append(rel)
                else:
                    added.append(rel)
            try:
                for info, rel in members:
                    target = install_dir / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    data = zf.read(info.filename)
                    with open(target, "wb") as fh:
                        fh.write(data)
                    # zipfile does not restore Unix modes: the
                    # stored mode is the exec bit (install.sh and
                    # the launchers need it).
                    mode = (info.external_attr >> 16) & 0o7777
                    if mode:
                        os.chmod(target, mode)
            except Exception:
                # Roll back: restore the backed-up files and
                # remove the newly added ones, so the old version
                # is still in place.
                for rel in added:
                    target = install_dir / rel
                    try:
                        if target.is_file():
                            target.unlink()
                    except OSError:
                        pass
                for rel in replaced:
                    src = backup_dir / rel
                    target = install_dir / rel
                    try:
                        target.parent.mkdir(parents=True,
                                            exist_ok=True)
                        shutil.copy2(src, target)
                    except OSError:
                        pass
                raise
    except UpdateError as exc:
        return False, str(exc)
    except (OSError, zipfile.BadZipFile) as exc:
        return False, ("The update could not be installed (%s). "
                       "The old version is still in place." % exc)
    clear_staged_update(home)
    return True, record["version"]


# The restarter: waits for the old process to die (so its
# single-instance lock is released), then starts the new
# version. It runs as its own session, detached from the GUI,
# so it survives the GUI quitting.
_RESTARTER = (
    "import os, sys, time\n"
    "pid = int(sys.argv[1])\n"
    "while True:\n"
    "    try:\n"
    "        os.kill(pid, 0)\n"
    "    except OSError:\n"
    "        break\n"
    "    time.sleep(0.2)\n"
    "os.execv(sys.argv[2], sys.argv[2:])\n"
)


def restart_after_exit(argv, pid=None):
    """Restart Fyrn after this process exits: a detached helper
    waits for this process to die, then starts argv. The new
    version must start only after the old process is gone, or
    it would see the single-instance lock and exit as a second
    instance. The helper inherits this process's environment and
    working directory, so PYTHONPATH and the launch folder
    carry over."""
    if pid is None:
        pid = os.getpid()
    subprocess.Popen(
        [sys.executable, "-c", _RESTARTER, str(pid)] + list(argv),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
        close_fds=True)
