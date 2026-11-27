"""Finding models already on this PC: Ollama's store.

Ollama keeps its models as content-addressed blobs (sha256
filenames, no .gguf extension) with JSON manifests mapping names
to blobs. Fyrn reads those manifests to list the models by name,
and loads a blob by path: it is a GGUF file whatever its name.
Fyrn never writes to Ollama's store and never deletes from it.

A manifest carries more than the model blob: the model's own
Modelfile settings (its params layer: sampling penalties,
temperature, stop tokens), its system prompt (the system
layer), and for a vision model its projector (the projector
layer). Discovery reads those too and carries them on the
model record (v0.13): a model Ollama runs with a presence
penalty was run by Fyrn with no penalty at all before, and a
store projector was never found because every blob is named
by hash, not "mmproj-...".

Standard library only. A store that cannot be read (permissions,
a missing folder) is skipped with a warning, never a crash.
"""

import json
import os
import shlex
from pathlib import Path

from .core import read_gguf_facts, read_gguf_metadata

_MODEL_LAYER = "application/vnd.ollama.image.model"
_PARAMS_LAYER = "application/vnd.ollama.image.params"
_SYSTEM_LAYER = "application/vnd.ollama.image.system"
_PROJECTOR_LAYER = "application/vnd.ollama.image.projector"


def ollama_store_dirs():
    """Where Ollama keeps its models, in the order to look: the
    OLLAMA_MODELS override first, then the per-user store, then
    the service install's store (Ollama runs as its own user
    there, so its home is /usr/share/ollama)."""
    dirs = []
    override = os.environ.get("OLLAMA_MODELS")
    if override:
        dirs.append(Path(override))
    dirs.append(Path.home() / ".ollama" / "models")
    dirs.append(Path("/usr/share/ollama/.ollama/models"))
    return dirs


def _manifest_name(manifest_path, manifests_root):
    """Display name from a manifest's place in the tree:
    manifests/registry.ollama.ai/library/qwen3/2b becomes
    qwen3:2b. library/ is the default namespace, so it is
    dropped; other namespaces are kept (acme/model:tag)."""
    try:
        rel = manifest_path.relative_to(manifests_root)
    except ValueError:
        return None
    parts = list(rel.parts)
    # Drop the registry host: registry.ollama.ai/...
    if parts and "." in parts[0]:
        parts = parts[1:]
    if len(parts) < 2:
        return None
    tag = parts[-1]
    name_parts = parts[:-1]
    if name_parts and name_parts[0] == "library":
        name_parts = name_parts[1:]
    if not name_parts:
        return None
    return "%s:%s" % ("/".join(name_parts), tag)


# The Modelfile parameters Fyrn honors, and the engine shape
# each takes. num_ctx is deliberately absent: the context size
# is Fyrn's own choice (its fit check prices the KV cache at
# the size it picks), so a Modelfile cannot move it. Anything
# else a Modelfile names (seed, mirostat, ...) is not passed:
# an unmapped parameter is ignored, never guessed at.
_PARAM_FLOATS = ("temperature", "top_p", "presence_penalty",
                 "frequency_penalty", "repeat_penalty")
_PARAM_INTS = ("top_k",)


def _coerce_params(raw):
    """A raw params mapping (from a JSON params blob or a
    Modelfile's PARAMETER lines) reduced to the parameters Fyrn
    honors, with honest types. Values that cannot be read as
    their kind are dropped, never passed on malformed."""
    params = {}
    if not isinstance(raw, dict):
        return params
    for key in _PARAM_FLOATS:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(
                value, bool):
            params[key] = float(value)
    for key in _PARAM_INTS:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(
                value, bool):
            params[key] = int(value)
    stop = raw.get("stop")
    if isinstance(stop, str):
        stop = [stop]
    if isinstance(stop, (list, tuple)):
        stops = [str(item) for item in stop if str(item)]
        if stops:
            params["stop"] = stops
    return params


def parse_params_blob(text):
    """A manifest params blob as the model's own parameters.

    The blob is either a JSON object (what current Ollama
    writes) or Modelfile text with PARAMETER lines (what a
    Modelfile carries before it is packed). Both reduce to the
    same honored set (see _coerce_params). Text that is neither
    gives {}: no parameters, the model runs on engine defaults,
    exactly as a folder model does.
    """
    if not text:
        return {}
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            return _coerce_params(json.loads(stripped))
        except ValueError:
            return {}
    raw = {}
    stops = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 3 or parts[0].upper() != "PARAMETER":
            continue
        name = parts[1].lower()
        value = parts[2].strip()
        if name == "stop":
            try:
                words = shlex.split(value)
            except ValueError:
                words = [value]
            if words:
                stops.append(words[0])
            continue
        # A number when it reads as one, else the bare word.
        try:
            raw[name] = int(value)
        except ValueError:
            try:
                raw[name] = float(value)
            except ValueError:
                raw[name] = value.strip("\"'")
    if stops:
        raw["stop"] = stops
    return _coerce_params(raw)


def _read_blob_text(blobs, digest, max_bytes=1024 * 1024):
    """A small manifest blob (params, system) as text, or None
    when it is missing, unreadable, or implausibly large."""
    if not digest or not digest.startswith("sha256:"):
        return None
    blob = blobs / ("sha256-" + digest[len("sha256:"):])
    try:
        if blob.stat().st_size > max_bytes:
            return None
        return blob.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _layer_digest(manifest, media_type):
    for layer in manifest.get("layers") or []:
        if layer.get("mediaType") == media_type:
            return layer.get("digest")
    return None


def discover_ollama_models(store_dirs=None):
    """List the models in Ollama's stores.

    Returns (models, warnings). models is a list of dicts with
    name, path, size, description, and source "ollama", sorted
    by name; a manifest's params, system, and projector layers
    add "params", "system", and "projector" entries when the
    store carries them. warnings is a list of (name, reason) for stores
    that could not be read, so nothing is silently skipped.
    """
    models = []
    warnings = []
    # One entry per model name: two stores can hold the same
    # model, and the first store in the order wins (the explicit
    # OLLAMA_MODELS override before the defaults).
    seen_names = set()
    stores = (store_dirs if store_dirs is not None
              else ollama_store_dirs())
    for store in stores:
        store = Path(store)
        manifests = store / "manifests"
        blobs = store / "blobs"
        if not store.is_dir():
            continue
        try:
            entries = sorted(manifests.rglob("*"))
        except OSError as exc:
            warnings.append(("Ollama models",
                             "could not read %s (%s)" % (store, exc)))
            continue
        for manifest_path in entries:
            if manifest_path.is_dir():
                continue
            try:
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            digest = _layer_digest(manifest, _MODEL_LAYER)
            if not digest or not digest.startswith("sha256:"):
                continue
            blob = blobs / ("sha256-" + digest[len("sha256:"):])
            try:
                size = blob.stat().st_size
            except OSError:
                continue
            name = _manifest_name(manifest_path, manifests)
            if name is None or name in seen_names:
                continue
            seen_names.add(name)
            # The description comes from the blob's own GGUF
            # header, the same as a folder model: never invented.
            # The facts (parameter count, quantization, context
            # length) come from the same header.
            description = read_gguf_metadata(blob).get(
                "general.description", "")
            record = {"name": name, "path": blob, "size": size,
                      "description": description,
                      "source": "ollama",
                      "facts": read_gguf_facts(blob)}
            # The model's own Modelfile settings, its system
            # prompt, and its vision projector ride along when
            # the manifest carries them (v0.13): the runner
            # honors the parameters on every generation, the
            # system prompt leads when the chat has none of its
            # own, and the projector is how a store vision model
            # gets its eyes (its blob is hash-named, so the
            # folder search beside the model can never find it).
            params = parse_params_blob(_read_blob_text(
                blobs, _layer_digest(manifest, _PARAMS_LAYER)))
            if params:
                record["params"] = params
            system = _read_blob_text(
                blobs, _layer_digest(manifest, _SYSTEM_LAYER))
            if system and system.strip():
                record["system"] = system.strip()
            projector_digest = _layer_digest(manifest,
                                             _PROJECTOR_LAYER)
            if (projector_digest
                    and projector_digest.startswith("sha256:")):
                projector = blobs / (
                    "sha256-" + projector_digest[len("sha256:"):])
                if projector.is_file():
                    record["projector"] = projector
            models.append(record)
    models.sort(key=lambda m: m["name"].lower())
    return models, warnings
