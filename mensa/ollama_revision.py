"""Read the installed Ollama manifest revision without starting or downloading a model.

The manifest digest identifies the declared local model. It does not authenticate the
referenced weight blobs; managed runtime readiness still checks the reported digest.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import stat


MAX_MANIFEST_BYTES = 1024 * 1024
_PART = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
_REGISTRY = re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]+)?')


def _manifest_parts(model):
    if not isinstance(model, str) or len(model) > 255:
        raise ValueError('Invalid Ollama model reference')
    # A registry port precedes the last slash; only a later colon introduces a tag.
    last_slash = model.rfind('/')
    last_colon = model.rfind(':')
    name, tag = (model[:last_colon], model[last_colon + 1:]) if last_colon > last_slash else (model, 'latest')
    parts = name.split('/')
    if len(parts) == 1:
        registry, namespace, name = 'registry.ollama.ai', 'library', parts[0]
    elif len(parts) == 2:
        registry, namespace, name = 'registry.ollama.ai', *parts
    elif len(parts) == 3:
        registry, namespace, name = parts
    else:
        raise ValueError('Invalid Ollama model reference')
    if (_REGISTRY.fullmatch(registry) is None
            or any(_PART.fullmatch(value) is None for value in (namespace, name, tag))):
        raise ValueError('Invalid Ollama model reference')
    return ('manifests', registry.lower(), namespace.lower(), name.lower(), tag)


def installed_ollama_revision(models_dir, model):
    """Return Ollama's unprefixed SHA256 of a bounded regular manifest for a model reference."""
    if not isinstance(models_dir, Path) or not models_dir.is_absolute():
        raise ValueError('Ollama models_dir must be an absolute Path')
    parts = _manifest_parts(model)
    directory = descriptor = None
    try:
        directory = os.open(models_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_MANIFEST_BYTES:
            raise ValueError('Invalid or oversized installed Ollama manifest')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError('Oversized installed Ollama manifest')
        manifest = json.loads(raw)
        if (not isinstance(manifest, dict) or manifest.get('schemaVersion') != 2
                or not isinstance(manifest.get('config'), dict)
                or not isinstance(manifest.get('layers'), list)):
            raise ValueError('Invalid installed Ollama manifest')
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ValueError('Cannot read a valid installed Ollama manifest; provide --model-revision '
                         'with the exact /api/tags digest or restore the model manifest') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)
    return hashlib.sha256(raw).hexdigest()
