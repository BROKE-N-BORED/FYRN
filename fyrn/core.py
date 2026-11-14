"""Core logic for Fyrn: models folder, RAM checks, fit badges.

Standard library only. No Qt here, so the rules are testable without
a window. All user-facing messages are plain words, no em-dashes.
"""

import os
import shutil
from pathlib import Path

APP_NAME = "Fyrn"
VERSION = "0.1.0"

# Working headroom a loaded model needs on top of its file size:
# the prompt context and working memory. 512 MB covers a 4096-token
# context on a 2B model with room to spare.
HEADROOM_BYTES = 512 * 1024 * 1024

# A model "fits" when what it needs stays under this share of free
# RAM, leaving the desktop usable. Above that it is "tight" until it
# needs more than free RAM outright, which is "too big".
FITS_SHARE_NUM = 3
FITS_SHARE_DEN = 4


class ModelTooBigError(Exception):
    """The model clearly will not fit in free RAM."""


class ModelNotFoundError(Exception):
    """The model file is gone."""


def get_home() -> Path:
    """Fyrn's home folder (logs, lock file).

    FYRN_HOME overrides it; tests point it at a temp folder so
    they never touch the real home.
    """
    override = os.environ.get("FYRN_HOME")
    if override:
        return Path(override)
    return Path.home() / ".fyrn"


def ensure_home() -> Path:
    """Create the home folder (and logs folder) if needed."""
    home = get_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    return home


def default_models_folder() -> Path:
    """Where pulled and imported models live unless changed."""
    return Path.home() / "Models"


def ensure_models_folder(folder: Path) -> Path:
    """Create the models folder if needed; return it."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ---------------------------------------------------------------- RAM


def read_ram_bytes():
    """Return (total_bytes, available_bytes) from /proc/meminfo.

    (None, None) when the numbers cannot be read (not Linux, or the
    file is missing). Callers treat that as "unknown", never as 0.
    """
    try:
        total = available = None
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024
                if total is not None and available is not None:
                    break
        if total is None:
            return None, None
        return total, available
    except (OSError, ValueError, IndexError):
        return None, None


def format_size(num_bytes) -> str:
    """Plain-words size: 850 MB, 2.1 GB, 12 KB."""
    if num_bytes is None:
        return "unknown size"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return "%d B" % int(size)
            if size >= 100 or unit == "KB":
                return "%d %s" % (round(size), unit)
            return "%.1f %s" % (size, unit)
        size /= 1024
    return "%.1f GB" % size


# ---------------------------------------------------------------- fit


def fit_status(file_size_bytes, free_bytes):
    """How a model file fits in free RAM.

    "fits" - needs less than 3/4 of free RAM, desktop stays usable.
    "tight" - fits, but leaves little spare RAM.
    "too big" - needs more than free RAM outright.
    "unknown" - free RAM could not be read, so no call is made.
    """
    if free_bytes is None:
        return "unknown"
    needed = file_size_bytes + HEADROOM_BYTES
    if needed <= free_bytes * FITS_SHARE_NUM // FITS_SHARE_DEN:
        return "fits"
    if needed <= free_bytes:
        return "tight"
    return "too big"


def ram_check(file_size_bytes, free_bytes):
    """Verdict before loading a model.

    Returns (verdict, message). Verdict is "ok", "warn", or "refuse".
    "unknown" RAM is "ok" with a note: with no numbers, refusing
    would block every load on a machine we cannot measure.
    """
    status = fit_status(file_size_bytes, free_bytes)
    if status == "fits":
        return ("ok", "This model fits your PC.")
    if status == "unknown":
        return ("ok", "RAM could not be read, so the fit check was "
                      "skipped. Loading anyway.")
    if status == "tight":
        return ("warn",
                "This model is tight on RAM (%s needed, %s free). "
                "Close other programs first, or it may be slow."
                % (format_size(file_size_bytes + HEADROOM_BYTES),
                   format_size(free_bytes)))
    return ("refuse",
            "This model will not fit in free RAM (%s needed, %s "
            "free). It would run out of memory, so it was not "
            "loaded. Close other programs and try again, or pick a "
            "smaller model."
            % (format_size(file_size_bytes + HEADROOM_BYTES),
               format_size(free_bytes)))


# ---------------------------------------------------------------- models


def scan_models(folder):
    """List the .gguf models in a folder.

    Returns (models, skipped). models is a list of dicts with name,
    path, and size, sorted by name. skipped is a list of
    (name, reason) for .gguf entries that could not be read, so
    nothing is silently dropped (U32). In-progress pulls (*.part)
    are not models yet and are not listed.
    """
    folder = Path(folder)
    models = []
    skipped = []
    if not folder.is_dir():
        return models, skipped
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not entry.name.lower().endswith(".gguf"):
            continue
        if entry.is_dir():
            skipped.append((entry.name, "is a folder, not a file"))
            continue
        try:
            size = entry.stat().st_size
        except OSError as exc:
            skipped.append((entry.name, "could not be read (%s)" % exc))
            continue
        models.append({"name": entry.name, "path": entry,
                       "size": size})
    return models, skipped


def import_gguf(source, folder):
    """Copy a loose .gguf file into the models folder.

    Returns the destination path. If the file is already in the
    folder, it is left alone and its path is returned. Raises
    ValueError with a plain-words message for anything that is not
    a readable .gguf file.
    """
    src = Path(source)
    folder = ensure_models_folder(folder)
    if not src.is_file():
        raise ValueError("That file could not be found.")
    if src.suffix.lower() != ".gguf":
        raise ValueError("That is not a .gguf model file.")
    dest = folder / src.name
    try:
        if src.resolve() == dest.resolve():
            return dest
    except OSError:
        pass
    if dest.exists():
        raise ValueError(
            "A model called %s is already in your models folder."
            % src.name)
    try:
        shutil.copy2(src, dest)
    except OSError as exc:
        raise ValueError("The file could not be copied (%s)." % exc)
    return dest


# ---------------------------------------------------------------- loading


class ModelController:
    """One model loaded at a time, with a RAM check first.

    Wraps a Runner (real or fake). load() auto-unloads whatever is
    loaded first. The RAM check runs before any load: a model that
    clearly will not fit is refused, never attempted.
    """

    def __init__(self, runner, free_ram_fn=None):
        self.runner = runner
        self._free_ram_fn = free_ram_fn or (
            lambda: read_ram_bytes()[1])
        self.current_path = None
        self.current_name = None
        self.load_warning = ""

    @property
    def is_loaded(self):
        return self.current_path is not None

    def check_before_load(self, path):
        """RAM verdict for a model file, before loading it."""
        path = Path(path)
        if not path.is_file():
            raise ModelNotFoundError(
                "That model file is gone. It may have been moved or "
                "deleted.")
        size = path.stat().st_size
        return ram_check(size, self._free_ram_fn())

    def load(self, path, progress_cb=None, cancel_event=None):
        """Load a model, auto-unloading the previous one.

        Raises ModelTooBigError when the RAM check refuses, without
        touching the currently loaded model. Returns a dict with
        load_time_s and warning ("" when the fit is clean).
        """
        path = Path(path)
        verdict, message = self.check_before_load(path)
        if verdict == "refuse":
            raise ModelTooBigError(message)
        if self.is_loaded:
            self.unload()
        result = self.runner.load(
            path, progress_cb=progress_cb, cancel_event=cancel_event)
        self.current_path = path
        self.current_name = path.name
        self.load_warning = message if verdict == "warn" else ""
        return {"load_time_s": result.get("load_time_s", 0.0),
                "warning": self.load_warning}

    def unload(self):
        """Unload the current model and free its RAM."""
        self.runner.unload()
        self.current_path = None
        self.current_name = None
        self.load_warning = ""
