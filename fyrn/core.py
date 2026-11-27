"""Core logic for Fyrn: models folder, RAM checks, fit badges.

Standard library only. No Qt here, so the rules are testable without
a window. All user-facing messages are plain words, no em-dashes.
"""

import os
import shutil
import struct
from pathlib import Path

APP_NAME = "Fyrn"

# Fallback headroom for a model whose header carries no KV
# numbers (an unreadable or older file): the measured working
# memory (WORKING_BYTES) plus a typical 2B model's KV cache at
# the app's context sizes. A model whose header CAN be read is
# charged its real numbers instead (model_headroom_bytes).
HEADROOM_BYTES = 1024 * 1024 * 1024

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

# Working memory a loaded model needs beyond its KV cache:
# the backend's compute buffer, its output buffer, and the
# context's own bookkeeping. Fixed; the KV cache is the part
# that varies with the model and the context length.
#
# Measured, not guessed (v0.9): the backend prints its own
# buffer sizes at load, and the load's RSS delta was measured
# in a fresh process per model. At n_ctx 2048 (the app uses
# 2048/4096; the compute buffer grows only ~4 KiB per context
# token, so 4096 adds ~8 MiB):
#   Qwen3 0.6B   compute 316.77 MiB, output 0.58 MiB
#   LFM3 1.7B    compute 282.02 MiB (v0.7's F3 measurement)
#   Qwen3.5 2B   compute 515.02 MiB, recurrent state 19.27 MiB,
#                output 0.95 MiB (allocations alone: 535 MiB)
# Qwen3.5 2B's whole footprint beyond its file size and KV
# cache measured ~713 MB by RSS delta: the allocations above
# plus the graph reserve and context bookkeeping around them.
# 768 MiB covers that worst measured case with slack for the
# roster's bigger models. The old 64 MiB figure understated
# every model by hundreds of MB, which made the fit badges
# optimistic (v0.7's F3 follow-up). A cautious badge costs a
# "tight" warning the user can load past; an optimistic one
# costs an out-of-memory kill mid-load.
WORKING_BYTES = 768 * 1024 * 1024

# KV cache element size: the backend stores keys and values as
# 16-bit floats (2 bytes each).
_KV_BYTES_PER_VALUE = 2


def _is_count(value):
    """True for a usable GGUF count (an int, not a bool)."""
    return isinstance(value, int) and not isinstance(value, bool)


def kv_cache_bytes_per_token(header):
    """KV cache bytes per context token, from a GGUF header.

    header is the dict from _read_gguf_kv (or read_gguf_metadata
    plus read_gguf_numbers), with a per-layer head_count_kv array
    merged in when the file carries one (see
    model_headroom_bytes). Each layer stores one key vector and
    one value vector per token, so the size is layers times (key
    size plus value size) times 2 bytes. Returns None when the
    header lacks what the calculation needs, so the caller falls
    back to the fixed headroom instead of guessing.
    """
    arch = header.get("general.architecture")
    if not isinstance(arch, str) or not arch:
        return None
    prefix = arch + "."
    layers = header.get(prefix + "block_count")
    if not _is_count(layers) or layers <= 0:
        return None
    heads = header.get(prefix + "attention.head_count")
    heads_kv = header.get(prefix + "attention.head_count_kv", heads)
    key_len = header.get(prefix + "attention.key_length")
    if key_len is None:
        # Head dimension from the embedding size and head count.
        embd = header.get(prefix + "embedding_length")
        if not _is_count(embd) or not _is_count(heads) or heads <= 0:
            return None
        key_len = embd // heads
    value_len = header.get(prefix + "attention.value_length", key_len)
    if not _is_count(key_len) or key_len <= 0:
        return None
    if not _is_count(value_len) or value_len <= 0:
        return None
    if isinstance(heads_kv, list):
        # Per-layer KV head counts (hybrid designs like LFM3,
        # whose attention.head_count_kv is an array, not a
        # scalar): a layer with 0 KV heads is a convolution layer
        # that caches no KV at all, and must not be charged for
        # any. Keys are charged at each layer's own size. Values
        # are padded: the backend stores every KV layer's values
        # at the largest value size across layers ("padding V
        # cache to 512" in the load log), so each KV layer pays
        # the maximum. Sliding-window layers count in full: this
        # backend uses a full-size cache for them ("using
        # full-size SWA cache"), not a window-sized one. The
        # numbers were checked against a real LFM3 load (see the
        # v0.7 changelog): 11 KV layers of 24, 22,528 bytes per
        # token, against the old 196,608.
        if (len(heads_kv) != layers
                or any(not _is_count(count) or count < 0
                       for count in heads_kv)):
            return None
        key_total = sum(count * key_len for count in heads_kv)
        value_sizes = [count * value_len for count in heads_kv
                       if count > 0]
        if not value_sizes:
            return 0
        per_token = (key_total
                     + len(value_sizes) * max(value_sizes))
        return per_token * _KV_BYTES_PER_VALUE
    if not _is_count(heads_kv) or heads_kv <= 0:
        return None
    per_token = layers * (heads_kv * key_len + heads_kv * value_len)
    return per_token * _KV_BYTES_PER_VALUE


def model_headroom_bytes(path, context_tokens):
    """RAM a model needs on top of its file size, from its own
    GGUF header: the KV cache for the context length plus fixed
    working memory. Falls back to the fixed headroom when the
    header does not carry the numbers (not a GGUF file, or an
    older format without them).
    """
    header = _read_gguf_kv(path)
    arch = header.get("general.architecture")
    if isinstance(arch, str) and arch:
        # A per-layer head_count_kv array (LFM3) is invisible to
        # the scalar reader, which skips arrays: read it directly
        # when the scalar is missing, so a hybrid model is charged
        # for its KV layers only (v0.7, F3).
        key = arch + ".attention.head_count_kv"
        if key not in header:
            array = read_gguf_number_array(path, key)
            if array is not None:
                header = dict(header)
                header[key] = array
    per_token = kv_cache_bytes_per_token(header)
    if per_token is None:
        return HEADROOM_BYTES
    return per_token * max(0, int(context_tokens)) + WORKING_BYTES


def fit_status(file_size_bytes, free_bytes, headroom_bytes=None):
    """How a model file fits in free RAM.

    "fits" - needs less than 3/4 of free RAM, desktop stays usable.
    "tight" - fits, but leaves little spare RAM.
    "too big" - needs more than free RAM outright.
    "unknown" - free RAM could not be read, so no call is made.
    headroom_bytes is the RAM needed on top of the file size;
    None means the fixed headroom (a model whose header was not
    read, or has no KV numbers).
    """
    if free_bytes is None:
        return "unknown"
    if headroom_bytes is None:
        headroom_bytes = HEADROOM_BYTES
    needed = file_size_bytes + headroom_bytes
    if needed <= free_bytes * FITS_SHARE_NUM // FITS_SHARE_DEN:
        return "fits"
    if needed <= free_bytes:
        return "tight"
    return "too big"


def ram_check(file_size_bytes, free_bytes, headroom_bytes=None):
    """Verdict before loading a model.

    Returns (verdict, message). Verdict is "ok", "warn", or "refuse".
    "unknown" RAM is "ok" with a note: with no numbers, refusing
    would block every load on a machine we cannot measure.
    """
    if headroom_bytes is None:
        headroom_bytes = HEADROOM_BYTES
    status = fit_status(file_size_bytes, free_bytes, headroom_bytes)
    if status == "fits":
        return ("ok", "This model fits your PC.")
    if status == "unknown":
        return ("ok", "RAM could not be read, so the fit check was "
                      "skipped. Loading anyway.")
    if status == "tight":
        return ("warn",
                "This model is tight on RAM (%s needed, %s free). "
                "Close other programs first, or it may be slow."
                % (format_size(file_size_bytes + headroom_bytes),
                   format_size(free_bytes)))
    return ("refuse",
            "This model will not fit in free RAM (%s needed, %s "
            "free). It would run out of memory, so it was not "
            "loaded. Close other programs and try again, or pick a "
            "smaller model."
            % (format_size(file_size_bytes + headroom_bytes),
               format_size(free_bytes)))


# ---------------------------------------------------------------- models

# GGUF header reading: the tensor-info reader below works on
# the first 256 KB only (the facts it needs sit at the very
# start of the file). The KV reader (_read_gguf_kv) instead
# streams the whole KV section from the file, skipping array
# payloads by seeking: a model's tokenizer tables are megabytes
# of array data sitting BEFORE later keys (the chat template,
# the sampling defaults), and a fixed head window silently
# lost every key past them (v0.13).
_GGUF_MAGIC = b"GGUF"
_GGUF_MAX_HEADER_BYTES = 256 * 1024
_GGUF_MAX_KV = 10000
_GGUF_MAX_STRING = 1024 * 1024
_GGUF_MAX_ARRAY = 100000

# Fixed-size GGUF value types: type id -> (struct code, size).
# 8 is STRING, 9 is ARRAY; both are handled separately.
_GGUF_FIXED = {
    0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2),
    4: ("<I", 4), 5: ("<i", 4), 6: ("<f", 4), 7: ("<?", 1),
    10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8),
}


class _GgufReader:
    """A cursor over GGUF header bytes. Any short read raises
    ValueError, so a truncated or corrupt file stops the parse
    instead of crashing it."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def read(self, size):
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError("short read")
        chunk = self.data[self.pos:self.pos + size]
        self.pos += size
        return chunk

    def u32(self):
        return struct.unpack("<I", self.read(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.read(8))[0]

    def string(self):
        length = self.u64()
        if length > _GGUF_MAX_STRING:
            raise ValueError("string too long")
        return self.read(length).decode("utf-8")

    def value(self, value_type):
        if value_type == 8:  # STRING
            return self.string()
        if value_type == 9:  # ARRAY: skip the elements
            elem_type = self.u32()
            count = self.u64()
            if count > _GGUF_MAX_ARRAY:
                raise ValueError("array too long")
            for _ in range(count):
                self.value(elem_type)
            return None
        fixed = _GGUF_FIXED.get(value_type)
        if fixed is None:
            raise ValueError("unknown value type %d" % value_type)
        code, size = fixed
        return struct.unpack(code, self.read(size))[0]


def _parse_gguf_header(data):
    """Parse a GGUF header's bytes.

    Returns (metadata, reader, tensor_count): metadata is the dict
    of scalar key -> value entries, reader sits just past the KV
    section (at the tensor infos), and tensor_count is how many
    tensors follow. Returns (None, None, None) when the bytes are
    not a readable GGUF header. A KV section that stops early
    yields partial metadata with the reader where it stopped, so
    a bad file still gives what it can.
    """
    if len(data) < 8 or data[:4] != _GGUF_MAGIC:
        return None, None, None
    reader = _GgufReader(data)
    reader.pos = 4
    try:
        version = reader.u32()
        if version == 3:
            tensor_count = reader.u64()
            kv_count = reader.u64()
        elif version == 2:
            tensor_count = reader.read(4)
            tensor_count = struct.unpack("<I", tensor_count)[0]
            kv_count = reader.u32()
        else:
            return None, None, None
    except ValueError:
        return None, None, None
    if kv_count > _GGUF_MAX_KV:
        return None, None, None
    metadata = {}
    try:
        for _ in range(kv_count):
            key = reader.string()
            value_type = reader.u32()
            value = reader.value(value_type)
            if value is not None:
                metadata[key] = value
    except (ValueError, struct.error, UnicodeDecodeError):
        pass  # partial metadata is still real metadata
    return metadata, reader, tensor_count


class _GgufStreamReader:
    """A cursor over a GGUF file object, for the KV section.

    Values are read; array payloads are SKIPPED by seeking, so
    walking the whole section costs a few small reads per entry
    however large the arrays are. Any short read raises
    ValueError, so a truncated or corrupt file stops the parse
    instead of crashing it.
    """

    def __init__(self, fh):
        self.fh = fh

    def read(self, size):
        chunk = self.fh.read(size)
        if len(chunk) != size:
            raise ValueError("short read")
        return chunk

    def skip(self, size):
        if size <= 0:
            return
        try:
            self.fh.seek(size, os.SEEK_CUR)
        except OSError:
            # Not seekable: read and discard in chunks.
            remaining = size
            while remaining > 0:
                chunk = self.fh.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError("short read")
                remaining -= len(chunk)

    def u32(self):
        return struct.unpack("<I", self.read(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.read(8))[0]

    def string(self):
        length = self.u64()
        if length > _GGUF_MAX_STRING:
            raise ValueError("string too long")
        return self.read(length).decode("utf-8")

    def skip_value(self, value_type):
        """Skip one value of the given type without reading its
        payload into memory."""
        if value_type == 8:  # STRING
            self.skip(self.u64())
            return
        if value_type == 9:  # ARRAY
            elem_type = self.u32()
            count = self.u64()
            if elem_type == 8:
                # An array of strings: walk the length prefixes,
                # seek past each payload.
                for _ in range(count):
                    self.skip(self.u64())
                return
            fixed = _GGUF_FIXED.get(elem_type)
            if fixed is None:
                raise ValueError(
                    "unknown value type %d" % elem_type)
            self.skip(fixed[1] * count)
            return
        fixed = _GGUF_FIXED.get(value_type)
        if fixed is None:
            raise ValueError("unknown value type %d" % value_type)
        self.skip(fixed[1])

    def value(self, value_type):
        """Read one scalar value; arrays are skipped and return
        None, the same contract as the bytes reader."""
        if value_type == 9:
            self.skip_value(value_type)
            return None
        if value_type == 8:  # STRING
            return self.string()
        fixed = _GGUF_FIXED.get(value_type)
        if fixed is None:
            raise ValueError("unknown value type %d" % value_type)
        code, size = fixed
        return struct.unpack(code, self.read(size))[0]


def _read_gguf_kv(path):
    """Read every scalar entry from a GGUF file's header.

    Returns a dict of key -> value for string and numeric entries
    (arrays are skipped, not returned). The whole KV section is
    walked, however far into the file it runs: an earlier reader
    stopped at a fixed 256 KB window and an array-size cap, and
    silently lost every key stored after the tokenizer tables
    (the chat template among them) on models with large
    vocabularies (v0.13). Anything that is not a readable GGUF
    file, or a header that cannot be parsed, returns {} or a
    partial dict: a bad file never raises.
    """
    try:
        fh = open(path, "rb")
    except OSError:
        return {}
    metadata = {}
    with fh:
        try:
            if fh.read(4) != _GGUF_MAGIC:
                return {}
            reader = _GgufStreamReader(fh)
            version = reader.u32()
            if version == 3:
                reader.u64()  # tensor count
                kv_count = reader.u64()
            elif version == 2:
                reader.u32()  # tensor count
                kv_count = reader.u32()
            else:
                return {}
            if kv_count > _GGUF_MAX_KV:
                return {}
            for _ in range(kv_count):
                key = reader.string()
                value = reader.value(reader.u32())
                if value is not None:
                    metadata[key] = value
        except (ValueError, struct.error, UnicodeDecodeError,
                OSError):
            pass  # partial metadata is still real metadata
    return metadata


# How much of a file read_gguf_number_array scans: the
# architecture keys it looks for sit at the very start of the
# header (before the tokenizer section, which is megabytes of
# token strings), so 1 MB is generous.
_GGUF_ARRAY_SCAN_BYTES = 1024 * 1024

# A per-layer array is at most one entry per layer (a few
# hundred); anything bigger is not a per-layer array.
_GGUF_MAX_LAYER_ARRAY = 4096


def read_gguf_number_array(path, key):
    """Read one numeric ARRAY entry from a GGUF file's header.

    Returns the list of values (ints, floats, or bools) when the
    key is there as an array of numbers with at most
    _GGUF_MAX_LAYER_ARRAY entries, else None. The scan stops as
    soon as the key is found, so the tokenizer section (megabytes
    of strings) is never walked for a key that sits before it.
    A bad file, a missing key, a non-numeric array, or a key past
    the scan window all return None: the caller falls back to the
    scalar numbers or the fixed headroom, never raises.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(_GGUF_ARRAY_SCAN_BYTES)
    except OSError:
        return None
    if len(head) < 8 or head[:4] != _GGUF_MAGIC:
        return None
    reader = _GgufReader(head)
    reader.pos = 4
    try:
        version = reader.u32()
        if version == 3:
            reader.u64()  # tensor count
            kv_count = reader.u64()
        elif version == 2:
            reader.u32()  # tensor count
            kv_count = reader.u32()
        else:
            return None
        if kv_count > _GGUF_MAX_KV:
            return None
        for _ in range(kv_count):
            entry_key = reader.string()
            value_type = reader.u32()
            if entry_key == key:
                if value_type != 9:  # not an array
                    return None
                elem_type = reader.u32()
                count = reader.u64()
                if (count > _GGUF_MAX_LAYER_ARRAY
                        or elem_type not in _GGUF_FIXED):
                    return None
                return [reader.value(elem_type)
                        for _ in range(count)]
            # Not the key: skip the value. A huge array (the
            # tokenizer's, over the reader's cap) raises here,
            # which ends the scan: a key past one is not found,
            # and None is the honest answer for it.
            reader.value(value_type)
    except (ValueError, struct.error, UnicodeDecodeError):
        return None
    return None


# GGUF general.file_type values, from the llama.cpp enum
# (verified against llama-cpp-python 0.3.36). Only values here are
# ever named: an unknown file type shows nothing, never a guess.
GGUF_FILE_TYPES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0",
    8: "Q5_0", 9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M",
    13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S",
    17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS",
    21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S",
    25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S",
    29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16",
    36: "TQ1_0", 37: "TQ2_0", 38: "MXFP4_MOE", 39: "NVFP4",
    40: "Q1_0", 41: "Q2_0",
}

# GGUF architectures that are vision models in their own right
# (their language model expects image input). Used only for the
# "vision model, projector missing" warning: the projector file
# itself is the signal that decides whether a model can see, so a
# fine-tune with a projector (Bonsai-2) works without being in
# this list.
VISION_ARCHITECTURES = frozenset(
    ("qwen3vl", "qwen2vl", "llava", "minicpmv"))


def read_gguf_tensor_params(path):
    """The exact parameter count of a GGUF model, from its tensor
    infos: the sum of every tensor's element count. Returns None
    when the file is not a GGUF file or the tensor section cannot
    be read (a bad file never raises, and a count that cannot be
    trusted is not shown).
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(_GGUF_MAX_HEADER_BYTES)
    except OSError:
        return None
    _metadata, reader, tensor_count = _parse_gguf_header(head)
    if reader is None or tensor_count is None:
        return None
    if tensor_count > 100000:
        return None
    total = 0
    try:
        for _ in range(tensor_count):
            reader.string()  # tensor name
            n_dims = reader.u32()
            if n_dims > 8:
                return None
            elements = 1
            for _ in range(n_dims):
                dim = reader.u64()
                elements *= dim
            reader.u32()  # tensor type
            reader.u64()  # offset
            total += elements
    except (ValueError, struct.error, UnicodeDecodeError):
        return None
    return total


def _format_params(count):
    """A parameter count in words: 2B, 1.7B, 500M."""
    if count >= 1_000_000_000:
        text = "%.1f" % (count / 1_000_000_000)
        return text[:-2] + "B" if text.endswith(".0") else text + "B"
    if count >= 1_000_000:
        return "%dM" % round(count / 1_000_000)
    return "%d" % count


def _format_context(tokens):
    """A context length in words: 32K context, 4K context."""
    if tokens >= 1024 and tokens % 1024 == 0:
        return "%dK context" % (tokens // 1024)
    return "%d context" % tokens


def read_gguf_facts(path):
    """The real facts about a GGUF model, from its own header.

    Returns a dict with only what the header carries: "params"
    (exact parameter count, from the tensor infos), "params_text"
    ("2B"), "architecture" ("qwen3"), "context_length" (tokens),
    "context_text" ("32K context"), "quantization" ("Q4_K_M").
    A file without a fact simply lacks that key: nothing is ever
    invented. A bad file returns {}.
    """
    facts = {}
    try:
        with open(path, "rb") as fh:
            head = fh.read(_GGUF_MAX_HEADER_BYTES)
    except OSError:
        return facts
    metadata, _reader, _count = _parse_gguf_header(head)
    if metadata is None:
        return facts
    arch = metadata.get("general.architecture")
    if isinstance(arch, str) and arch:
        facts["architecture"] = arch
        context = metadata.get(arch + ".context_length")
        if _is_count(context) and context > 0:
            facts["context_length"] = context
            facts["context_text"] = _format_context(context)
    file_type = metadata.get("general.file_type")
    if _is_count(file_type):
        quant = GGUF_FILE_TYPES.get(file_type)
        if quant is not None:
            facts["quantization"] = quant
    params = read_gguf_tensor_params(path)
    if params:
        facts["params"] = params
        facts["params_text"] = _format_params(params)
    return facts


def format_facts(facts):
    """A facts dict as one short card line: "2B · Q4_K_M · 32K
    context". Empty when the header carried nothing to show."""
    parts = []
    if facts.get("params_text"):
        parts.append(facts["params_text"])
    if facts.get("quantization"):
        parts.append(facts["quantization"])
    if facts.get("context_text"):
        parts.append(facts["context_text"])
    return " · ".join(parts)


def format_chat_export(transcript, model_name=None, when=None):
    """A chat transcript as the text of a saved file (task 13):
    a short header naming the model and when it was saved, then
    the transcript itself, in order, unchanged. The transcript
    is the Chat tab's text ("You: ...", "<model>: ...", turns
    divided), so what is saved is exactly what was on screen.
    when is a "YYYY-MM-DD HH:MM" stamp (now, when not given)."""
    if when is None:
        import time
        when = time.strftime("%Y-%m-%d %H:%M")
    model = model_name or "no model"
    header = ("Fyrn chat with %s\nSaved %s\n\n"
              % (model, when))
    return header + transcript.rstrip("\n") + "\n"


def find_projector(model_path):
    """The vision projector (mmproj) file for a model, if one sits
    next to it in the same folder: a .gguf file starting with
    "mmproj-". One candidate wins outright; several are matched by
    the longest shared name with the model, and a tie means no
    projector (guessing between two would pick the wrong eyes).
    Returns a Path or None.
    """
    model_path = Path(model_path)
    folder = model_path.parent
    if not folder.is_dir():
        return None
    candidates = []
    for entry in folder.iterdir():
        name = entry.name.lower()
        if (name.startswith("mmproj-")
                and name.endswith(".gguf")
                and entry.is_file()):
            candidates.append(entry)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    model_stem = _match_stem(model_path.stem)
    best = None
    best_score = -1
    tied = False
    for candidate in candidates:
        stem = _match_stem(candidate.stem)
        score = 0
        for left, right in zip(model_stem, stem):
            if left != right:
                break
            score += 1
        if score > best_score:
            best = candidate
            best_score = score
            tied = False
        elif score == best_score:
            tied = True
    return None if tied else best


def projector_size_bytes(model_path):
    """The bytes a model's vision projector adds to a load: the
    projector file's size when one sits next to the model, else 0.
    The fit check counts it, or a vision model would load with
    its eyes unaccounted for and run out of RAM."""
    projector = find_projector(model_path)
    if projector is None:
        return 0
    try:
        return projector.stat().st_size
    except OSError:
        return 0


def _match_stem(stem):
    """A file stem for projector matching: lowercased, the
    "mmproj-" prefix and the trailing quantization tag stripped,
    so "mmproj-Qwen3-VL-2B-Instruct-Q8_0" matches the model
    "Qwen3-VL-2B-Instruct-Q4_K_M" on "qwen3-vl-2b-instruct"."""
    stem = stem.lower()
    if stem.startswith("mmproj-"):
        stem = stem[len("mmproj-"):]
    # The last hyphen part is the quantization (Q4_K_M, Q8_0,
    # F16, ...) when it looks like one.
    if "-" in stem:
        head, _sep, tail = stem.rpartition("-")
        if (tail.startswith(("q", "iq", "f", "bf", "tq"))
                and any(ch.isdigit() for ch in tail)):
            stem = head
    return stem


def read_gguf_metadata(path):
    """Read the string metadata from a GGUF file's header.

    Returns a dict of key -> value for string-valued entries
    (general.name, general.description, ...). Anything that is
    not a readable GGUF file, or a header that cannot be parsed,
    returns {} or a partial dict: a bad file never raises.
    """
    return {key: value for key, value in _read_gguf_kv(path).items()
            if isinstance(value, str)}


def read_gguf_numbers(path):
    """Read the numeric metadata from a GGUF file's header.

    Returns a dict of key -> value for integer and float entries
    (block counts, head counts, dimensions, ...). Same never-
    raises promise as read_gguf_metadata.
    """
    return {key: value for key, value in _read_gguf_kv(path).items()
            if isinstance(value, (int, float))
            and not isinstance(value, bool)}


def scan_models(folder):
    """List the .gguf models in a folder.

    Returns (models, skipped). models is a list of dicts with name,
    path, size, and description (from the file's GGUF header, ""
    when the file has none), sorted by name. skipped is a list of
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
        # The description comes from the file's own GGUF header
        # (general.description), never invented: a file without
        # one simply has no description (L9). The facts (parameter
        # count, quantization, context length) come from the same
        # header, and a file without them simply has none.
        description = read_gguf_metadata(entry).get(
            "general.description", "")
        models.append({"name": entry.name, "path": entry,
                       "size": size, "description": description,
                       "source": "folder",
                       "facts": read_gguf_facts(entry)})
    return models, skipped


class ImportCancelled(Exception):
    """The user stopped an import. No partial file is left."""


def import_gguf(source, folder, progress_cb=None, cancel_event=None):
    """Copy a loose .gguf file into the models folder.

    Returns the destination path. If the file is already in the
    folder, it is left alone and its path is returned. The copy
    runs in chunks: progress_cb(copied, total) reports it, and
    cancel_event stops it. The copy lands in a .part file and is
    moved into place only when complete, so a stopped or failed
    import leaves no partial model behind. Raises ValueError with
    a plain-words message for anything that is not a readable
    .gguf file, and ImportCancelled when the copy is stopped.
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
        total = src.stat().st_size
    except OSError as exc:
        raise ValueError("That file could not be read (%s)." % exc)
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        free = None
    if free is not None and total > free:
        raise ValueError(
            "There is not enough disk space for %s (%s needed, %s "
            "free)." % (src.name, format_size(total),
                        format_size(free)))
    part = dest.with_name(dest.name + ".part")
    copied = 0
    try:
        with open(src, "rb") as in_fh, open(part, "wb") as out_fh:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise ImportCancelled("Import stopped.")
                chunk = in_fh.read(1024 * 1024)
                if not chunk:
                    break
                out_fh.write(chunk)
                copied += len(chunk)
                if progress_cb is not None:
                    progress_cb(copied, total)
    except ImportCancelled:
        _drop_quietly(part)
        raise
    except OSError as exc:
        _drop_quietly(part)
        raise ValueError("The file could not be copied (%s)." % exc)
    try:
        os.replace(part, dest)
    except OSError as exc:
        _drop_quietly(part)
        raise ValueError("The file could not be copied (%s)." % exc)
    return dest


def _drop_quietly(path):
    """Delete a file if it is there; a missing file is fine."""
    try:
        Path(path).unlink()
    except OSError:
        pass


def list_part_files(folder):
    """Unfinished downloads in a folder: every *.part file, with
    name, path, and size, sorted by name. The .etag and .url
    sidecars belong to the .part file and are not listed."""
    folder = Path(folder)
    parts = []
    if not folder.is_dir():
        return parts
    for entry in sorted(folder.iterdir(),
                        key=lambda p: p.name.lower()):
        if not entry.name.endswith(".part"):
            continue
        if entry.is_dir():
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        parts.append({"name": entry.name, "path": entry,
                      "size": size})
    return parts


def remove_gguf(path):
    """Delete a .gguf model file from the models folder.

    Raises ValueError with a plain-words message when the file is
    gone, is not a .gguf file, or cannot be deleted.
    """
    path = Path(path)
    if not path.is_file():
        raise ValueError("That model file is already gone.")
    if path.suffix.lower() != ".gguf":
        raise ValueError("That is not a .gguf model file.")
    try:
        path.unlink()
    except OSError as exc:
        raise ValueError(
            "The file could not be removed (%s)." % exc)


def trash_dir():
    """The desktop trash folder (freedesktop spec): $XDG_DATA_HOME
    when set, otherwise ~/.local/share, plus /Trash."""
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "Trash"
    return Path.home() / ".local" / "share" / "Trash"


def move_to_trash(path):
    """Move a .gguf model file to the desktop trash instead of
    deleting it, so a slip is recoverable from the trash.

    Follows the freedesktop trash spec: the file lands in
    Trash/files (renamed with a number when the name is taken)
    and a .trashinfo file in Trash/info records where it came
    from and when. A move across filesystems falls back to a
    copy plus delete. Returns the path in the trash. Raises
    ValueError with a plain-words message when the file is gone,
    is not a .gguf file, or cannot be moved: a failed move never
    deletes the file.
    """
    from datetime import datetime
    from urllib.parse import quote

    path = Path(path)
    if not path.is_file():
        raise ValueError("That model file is already gone.")
    if path.suffix.lower() != ".gguf":
        raise ValueError("That is not a .gguf model file.")
    trash = trash_dir()
    files_dir = trash / "files"
    info_dir = trash / "info"
    try:
        files_dir.mkdir(parents=True, exist_ok=True)
        info_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(
            "The trash could not be used (%s)." % exc)
    dest = files_dir / path.name
    suffix = 1
    while dest.exists():
        dest = files_dir / ("%s.%d" % (path.name, suffix))
        suffix += 1
    try:
        os.replace(path, dest)
    except OSError as exc:
        if exc.errno != 18:  # not a cross-device move: fail, keep file
            raise ValueError(
                "The file could not be moved to the trash (%s)."
                % exc)
        try:
            shutil.copy2(path, dest)
            path.unlink()
        except OSError as exc2:
            _drop_quietly(dest)
            raise ValueError(
                "The file could not be moved to the trash (%s)."
                % exc2)
    info_path = info_dir / (dest.name + ".trashinfo")
    try:
        info_path.write_text(
            "[Trash Info]\nPath=%s\nDeletionDate=%s\n"
            % (quote(str(path.resolve()), safe="/"),
               datetime.now().strftime("%Y-%m-%dT%H:%M:%S")),
            encoding="utf-8")
    except OSError:
        # The file is already in the trash; a missing .trashinfo
        # only costs the restore path, not the file itself.
        pass
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

    def check_before_load(self, path, context_tokens=None,
                          projector=None):
        """RAM verdict for a model file, before loading it.

        context_tokens is the context the load will use when a
        caller chose one (the bridge's num_ctx); the KV part of
        the headroom is priced at that size, not the machine
        pick. projector is the model's vision projector file
        when discovery named one explicitly (an Ollama store
        model's, v0.13); its size counts toward the load exactly
        as a folder projector's does."""
        path = Path(path)
        if not path.is_file():
            raise ModelNotFoundError(
                "That model file is gone. It may have been moved or "
                "deleted.")
        try:
            size = path.stat().st_size
        except OSError:
            # The file vanished between the check above and the
            # stat: gone is gone, whatever the timing.
            raise ModelNotFoundError(
                "That model file is gone. It may have been moved or "
                "deleted.")
        free = self._free_ram_fn()
        # The context length the runner will actually use decides
        # the KV cache size, so the fit check uses the same number
        # instead of a fixed guess. A vision model's projector
        # counts too: it loads alongside the model.
        from .runner import auto_context_tokens
        if not (isinstance(context_tokens, int)
                and not isinstance(context_tokens, bool)
                and context_tokens > 0):
            context_tokens = auto_context_tokens(free)
        headroom = model_headroom_bytes(path, context_tokens)
        if projector is not None:
            try:
                projector_bytes = Path(projector).stat().st_size
            except OSError:
                projector_bytes = 0
        else:
            projector_bytes = projector_size_bytes(path)
        return ram_check(size + projector_bytes, free, headroom)

    def load(self, path, progress_cb=None, cancel_event=None,
             display_name=None, n_ctx=None, model_params=None,
             model_system=None, projector=None):
        """Load a model, auto-unloading the previous one.

        Raises ModelTooBigError when the RAM check refuses, without
        touching the currently loaded model. Returns a dict with
        load_time_s and warning ("" when the fit is clean).
        display_name is the name the UI shows for the model (an
        Ollama model's name, for example); the file name is used
        when it is not given. n_ctx asks the runner for that
        context size (the bridge's num_ctx); None keeps the
        runner's own pick. model_params, model_system, and
        projector are what discovery read from the model's own
        Ollama manifest (v0.13); they go straight to the runner.
        """
        path = Path(path)
        verdict, message = self.check_before_load(
            path, context_tokens=n_ctx, projector=projector)
        if verdict == "refuse":
            raise ModelTooBigError(message)
        if self.is_loaded:
            self.unload()
        # The manifest extras (v0.13) are passed only when
        # discovery actually supplied one: a load with nothing
        # extra calls the runner exactly as it always has, so a
        # runner that predates them is never surprised.
        extras = {}
        if model_params is not None:
            extras["model_params"] = model_params
        if model_system is not None:
            extras["model_system"] = model_system
        if projector is not None:
            extras["projector"] = projector
        result = self.runner.load(
            path, progress_cb=progress_cb, cancel_event=cancel_event,
            display_name=display_name, n_ctx=n_ctx, **extras)
        self.current_path = path
        self.current_name = display_name or path.name
        self.load_warning = message if verdict == "warn" else ""
        return {"load_time_s": result.get("load_time_s", 0.0),
                "warning": self.load_warning,
                "vision_warning": result.get("vision_warning", "")}

    def unload(self):
        """Unload the current model and free its RAM."""
        self.runner.unload()
        self.current_path = None
        self.current_name = None
        self.load_warning = ""
