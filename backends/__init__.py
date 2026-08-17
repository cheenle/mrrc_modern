"""
Pluggable radio backends
========================
Use ``create_backend(model, ...)`` to instantiate the CAT backend for a
radio model.  Currently registered: "ft710" (Yaesu FT-710), "ic7300"
(Icom IC-7300) and its alias "ic7300mk2".
"""
from __future__ import annotations

from backends.base import RadioBackend, RadioCapabilities, ScopeProducer

__all__ = ["RadioBackend", "RadioCapabilities", "ScopeProducer", "create_backend"]

# model key -> (module path, class name); imported lazily so that
# backend-specific dependencies are only loaded when selected.
_BACKENDS = {
    "ft710": ("backends.ft710.backend", "FT710Backend"),
    "ic7300": ("backends.ic7300.backend", "IC7300Backend"),
    "ic7300mk2": ("backends.ic7300.backend", "IC7300MK2Backend"),
}


def create_backend(model: str, *args, **kwargs) -> RadioBackend:
    """Create the RadioBackend for the given model key.

    Positional/keyword arguments are forwarded to the backend
    constructor (FT-710: ``port: str, baudrate: int = 38400``).
    Raises ValueError for unknown models.
    """
    import importlib

    key = (model or "").strip().lower()
    entry = _BACKENDS.get(key)
    if entry is None:
        known = ", ".join(sorted(_BACKENDS)) or "none"
        raise ValueError(f"unknown radio model {model!r} (registered: {known})")
    module = importlib.import_module(entry[0])
    cls = getattr(module, entry[1])
    return cls(*args, **kwargs)
