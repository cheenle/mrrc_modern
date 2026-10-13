"""Immutable value types shared by the DSP binding and Worker protocol."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from enum import StrEnum


FT8_RX_RATE = 12_000
FT8_RX_SAMPLES = 180_000
FT8_TX_RATE = 48_000
FT8_TX_SAMPLES = 606_720
RESULT_CAPACITY = 256
MAX_DECODE_THREADS = 12


def auto_thread_count(cpu_count: int | None = None) -> int:
    """I9 Auto thread policy: logical CPUs minus one reserved core, 1-12.

    Measured on Apple M2 (SDD §13.4): scaling saturates well before
    oversubscription and reserving one core keeps audio/UI responsive.
    """

    cpus = os.cpu_count() if cpu_count is None else cpu_count
    return min(max((cpus or 2) - 1, 1), MAX_DECODE_THREADS)


class DecodePath(StrEnum):
    """Native FT8 decoder implementation selected for one request."""

    FAST = "fast"
    STANDARD = "standard"
    IMPROVED = "improved"


@dataclass(frozen=True, slots=True)
class DecodeConfig:
    """One decode request's configuration.

    Validation deliberately belongs to :mod:`ft8.core.binding`, allowing a
    malformed protocol frame to be represented and then rejected at the DSP
    boundary.
    """

    path: DecodePath = DecodePath.IMPROVED
    sample_rate: int = FT8_RX_RATE
    sample_count: int = FT8_RX_SAMPLES
    profile: int = 3
    threads: int = 1
    cycles: int = 1
    sensitivity: int = 2
    ap: bool = True
    low_threshold: bool = False
    wide_dx: bool = False
    hide_duplicates: bool = True
    qso_progress: int = 0
    rx_frequency: int = 1500
    tx_frequency: int = 1500
    low_frequency: int = 200
    high_frequency: int = 3000
    ap_width: int = 50
    utc_hhmmss: int = 0
    my_call: str = ""
    dx_call: str = ""
    dx_grid: str = ""

    @classmethod
    def standard(cls) -> DecodeConfig:
        """Return the default configuration for the standard decoder."""

        return cls(path=DecodePath.STANDARD)

    def replace(self, **changes: object) -> DecodeConfig:
        """Return a copy with selected fields replaced."""

        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class DecodeResult:
    """One native decode copied into process-owned immutable data."""

    slot_id: int
    sync: float
    snr: int
    dt: float
    frequency: float
    text: str
    ap_type: int
    quality: float
    flags: int


@dataclass(frozen=True, slots=True)
class DecodeBatch:
    """One completed standard or Improved decode request."""

    slot_id: int
    path: DecodePath
    results: tuple[DecodeResult, ...]
    overflow: bool
    elapsed_seconds: float
    deadline_missed: bool = False


@dataclass(frozen=True, slots=True)
class EncodeResult:
    """Metadata for a waveform written into caller-owned shared memory."""

    message: str
    sample_rate: int
    sample_count: int
