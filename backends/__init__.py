"""
Pluggable radio backends
========================
Use ``create_backend(model, ...)`` to instantiate the CAT backend for a
radio model and ``known_models()`` to validate a model key.  Registered:
"ft710" (Yaesu FT-710), the Icom CI-V family "ic7300" (IC-7300),
"ic7300mk2", "ic705", "ic7610" and "ic7760", the Yaesu ASCII-CAT
family "ftdx10", "ftdx101d", "ftdx101mp", "ftx1", "ft891" and "ft991a",
and the Xiegu CI-V family "g7250" (G7250 — same CI-V core as the Icom
family, own profile + LAN IQ scope), and "sunsdr2dx" (Expert Electronics
SunSDR2 DX — native UDP backend, LAN audio + LAN IQ scope).
"""
from __future__ import annotations

from backends.base import RadioBackend, RadioCapabilities, ScopeProducer

__all__ = ["RadioBackend", "RadioCapabilities", "ScopeProducer",
           "create_backend", "known_models", "model_connection",
           "model_verification"]

# model key -> (module path, class name); imported lazily so that
# backend-specific dependencies are only loaded when selected.
_BACKENDS = {
    "ft710": ("backends.ft710.backend", "FT710Backend"),
    "ic7300": ("backends.ic7300.backend", "IC7300Backend"),
    "ic7300mk2": ("backends.ic7300.backend", "IC7300MK2Backend"),
    "ic705": ("backends.ic7300.backend", "IC705Backend"),
    "ic7610": ("backends.ic7300.backend", "IC7610Backend"),
    "ic7760": ("backends.ic7300.backend", "IC7760Backend"),
    # Yaesu ASCII-CAT family (spec 2026-09-12; FT-891 added 2026-10-05,
    # FT-991A added 2026-10-31): same core, six profiles.  Unverified models:
    # TX gated behind MRRC_ALLOW_UNVERIFIED_TX.
    "ftdx10": ("backends.yaesu.backend", "FTDX10Backend"),
    "ftdx101d": ("backends.yaesu.backend", "FTDX101DBackend"),
    "ftdx101mp": ("backends.yaesu.backend", "FTDX101MPBackend"),
    "ftx1": ("backends.yaesu.backend", "FTX1Backend"),
    "ft891": ("backends.yaesu.backend", "FT891Backend"),
    "ft991a": ("backends.yaesu.backend", "FT991ABackend"),
    # Xiegu CI-V family (spec 2026-11-07): Icom CI-V framing, Xiegu address
    # discovery, LAN IQ scope instead of CI-V 0x27.  Unverified: TX gated
    # behind MRRC_ALLOW_UNVERIFIED_TX.
    "g7250": ("backends.xiegu.backend", "G7250Backend"),
    # Expert Electronics SunSDR2 DX (spec 2026-11-08): native UDP backend —
    # control 50001 + IQ 50002 in one engine, LAN audio/scope adapters.
    # No serial port and no USB sound card.  Unverified: TX gated behind
    # MRRC_ALLOW_UNVERIFIED_TX.
    "sunsdr2dx": ("backends.sunsdr.backend", "SunSDR2DXBackend"),
}


def known_models() -> tuple:
    """Registered model keys — the server's validation source of truth.

    Derived from the registry so a new backend cannot be silently
    forgotten in a hardcoded tuple (the connection dialog and /api/setup
    validate against this).
    """
    return tuple(_BACKENDS)


def model_connection(model: str) -> dict:
    """JSON-safe setup metadata; unknown keys fail like create_backend()."""
    key = (model or "").strip().lower()
    if key not in _BACKENDS:
        raise ValueError(f"unknown radio model {model!r}")
    if key == "sunsdr2dx":
        return {"transport": "network", "host_required": True}
    return {"transport": "serial", "host_required": False}


def model_verification() -> dict:
    """``{model_key: {"name": ..., "verified": bool}}`` for every registered model.

    Read from the per-family profile tables rather than instantiating a backend:
    a backend needs a serial port and its connect path touches hardware, while a
    settings page only needs to name the models whose transmit is gated.  The
    FT-710 is the one model with no profile module — it *is* the field-proven
    path (AD-018), so it is reported verified by construction.
    """
    from backends.ic7300.civ_profiles import get_profile as civ_profile
    from backends.yaesu.yaesu_profiles import get_profile as yaesu_profile
    from backends.xiegu.xiegu_profiles import get_profile as xiegu_profile

    out = {}
    for key, (module, _cls) in _BACKENDS.items():
        if module == "backends.yaesu.backend":
            profile = yaesu_profile(key)
        elif module == "backends.ic7300.backend":
            profile = civ_profile(key)
        elif module == "backends.xiegu.backend":
            profile = xiegu_profile(key)
        elif module == "backends.sunsdr.backend":
            # No profile module on the native backend — the module itself
            # carries the display name and the verification flag.
            from backends.sunsdr import backend as sunsdr_backend
            out[key] = {"name": sunsdr_backend.DISPLAY_NAME,
                        "verified": bool(getattr(sunsdr_backend, "VERIFIED",
                                                 False))}
            continue
        else:
            out[key] = {"name": "Yaesu FT-710", "verified": True}
            continue
        out[key] = {"name": profile.display_name,
                    "verified": bool(getattr(profile, "verified", False))}
    return out


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
