"""Finding models already on this PC: Ollama's store.

Ollama keeps its models as content-addressed blobs (sha256
filenames, no .gguf extension) with JSON manifests mapping names
to blobs. Fyrn reads those manifests to list the models by name,
and loads a blob by path: it is a GGUF file whatever its name.
Fyrn never writes to Ollama's store and never deletes from it.

Standard library only. A store that cannot be read (permissions,
a missing folder) is skipped with a warning, never a crash.
"""

import json
import os
from pathlib import Path

from .core import read_gguf_facts, read_gguf_metadata

_MODEL_LAYER = "application/vnd.ollama.image.model"


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


def discover_ollama_models(store_dirs=None):
    """List the models in Ollama's stores.

    Returns (models, warnings). models is a list of dicts with
    name, path, size, description, and source "ollama", sorted
    by name. warnings is a list of (name, reason) for stores
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
            digest = None
            for layer in manifest.get("layers") or []:
                if layer.get("mediaType") == _MODEL_LAYER:
                    digest = layer.get("digest")
                    break
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
            models.append({"name": name, "path": blob, "size": size,
                           "description": description,
                           "source": "ollama",
                           "facts": read_gguf_facts(blob)})
    models.sort(key=lambda m: m["name"].lower())
    return models, warnings
