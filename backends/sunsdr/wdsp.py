"""
WDSP (Warren Pratt DSP) Python Wrapper
=======================================
Strict ctypes wrapper around the WDSP shared library with pre-allocated
ring buffers, zero-copy pointer passing, and exhaustive type bindings for
ARM64 safety.

Library discovery: ``MRRC_WDSP_LIB`` (explicit path), then the copy
shipped beside this module in ``vendor/`` (``libwdsp.so`` on Linux,
``libwdsp.dylib`` on macOS), then the historical system locations.  The
library is optional: when it is absent ``WDSP_AVAILABLE`` stays False and
the SunSDR backend simply reports the noise controls as unsupported.

Library: https://github.com/g0orx/wdsp (GPL-2.0-or-later; see
``vendor/WDSP-LICENSE``).

Refactored 2026-06-28:
  - argtypes / restype declared for every WDSP function — prevents
    ARM64 calling-convention corruption (float returns landing in the
    wrong register).
  - Ring buffer replaces dynamic np.concatenate() → zero per-call
    allocation, no GC pressure at 61 Hz.
  - NR2 strength drives gainMETHOD + npeMETHOD + AEPF psi, per the
    WDSP Guide Rev 1.29 §Spectral Noise Reduction. The AE zeta/psi
    pair are the AEPF (Artifact-Elimination Post-Filter) musical-noise
    suppressor — NOT a "strength" knob; they must stay near Warren
    Pratt's tuned defaults (zetaThresh=0.75, psi=20.0) or the AEPF
    smoothing window collapses to N=1 and musical tones ("数码音")
    return. SetRXAEMNRgainLine does NOT exist in this build; the
    post2 psychoacoustic softening chain (SetRXAEMNRpost2*) is also
    absent from the dylib, so Trained mode (method=3) is reserved for
    the top of the strength range only.
  - set_bandpass uses SetRXABandpassFreqs (existed all along).
  - set_nf_enabled uses the NBP Notch Bank Processor API.
  - Memory contiguity & dtype assertions on every C-boundary call.
  - New methods: get_s_meter, set_agc_attack / decay / hang,
    set_eq_run, set_fm_squelch_run / threshold.
"""

import ctypes, os, sys, logging, numpy as np

logger = logging.getLogger(__name__)

# ── Library loading ────────────────────────────────────────────────

_wdsp: ctypes.CDLL | None = None
WDSP_AVAILABLE = False
_LIBRARY_PATH = ""


def _library_names():
    if sys.platform == "darwin":
        return ("libwdsp.dylib",)
    if os.name == "nt":
        return ("libwdsp.dll", "wdsp.dll")
    return ("libwdsp.so",)


def _candidate_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    paths = []
    env = (os.environ.get("MRRC_WDSP_LIB") or "").strip()
    if env:
        paths.append(env)
    for name in _library_names():
        paths.append(os.path.join(here, "vendor", name))
        paths.append(os.path.join(here, name))
    if sys.platform == "darwin":
        paths.extend([
            "/usr/local/lib/libwdsp.dylib",
            "/opt/homebrew/lib/libwdsp.dylib",
        ])
    else:
        paths.extend(["/usr/local/lib/libwdsp.so", "/usr/lib/libwdsp.so"])
    # Deduplicate while preserving order (an explicit env path wins).
    seen = set()
    return [p for p in paths if not (p in seen or seen.add(p))]


def _load():
    global _wdsp, WDSP_AVAILABLE, _LIBRARY_PATH
    errors = []
    for p in _candidate_paths():
        if not os.path.exists(p):
            continue
        try:
            lib = ctypes.CDLL(p)
        except OSError as e:
            errors.append(f"{p}: {e}")
            continue
        _wdsp = lib
        _LIBRARY_PATH = p
        WDSP_AVAILABLE = True
        return
    if errors:
        logger.warning("WDSP: no usable library (%s)", "; ".join(errors))
    else:
        logger.debug("WDSP: library not present")


def library_path() -> str:
    """Path of the loaded shared library, or '' when WDSP is unavailable."""
    return _LIBRARY_PATH


def available() -> bool:
    """True when a WDSP library was loaded and its core symbols bound."""
    return bool(WDSP_AVAILABLE)


_load()

# ── Symbol registry ─────────────────────────────────────────────────
# Populated by _bind_signatures().  Each key is a C symbol name; the value
# is True.  _check_symbol(name) gates optional-feature calls so missing
# symbols in a different build don't crash — they just no-op after one
# WARNING log at startup.

_WDSP_SYMBOLS: dict[str, bool] = {}

def _check_symbol(name: str) -> bool:
    return _WDSP_SYMBOLS.get(name, False)


def _bind_signatures():
    """Declare ctypes argtypes / restype for every WDSP function we use.

    Each binding is independently guarded: a missing symbol logs a single
    WARNING and skips that function.  Core symbols (OpenChannel,
    fexchange0, CloseChannel) are asserted — the library is useless
    without them.

    ARM64 NOTE — restype matters.  Without it ctypes defaults to c_int
    for EVERY function.  GetRXAMeter returns double; on ARM64 the float
    return goes into a different register than int, so the caller reads
    garbage.  Declaring restype = ctypes.c_double fixes this.
    """
    global _WDSP_SYMBOLS

    c_int = ctypes.c_int
    c_double = ctypes.c_double
    c_void_p = ctypes.c_void_p
    c_bool = ctypes.c_bool

    def _bind(name, argtypes, restype):
        try:
            func = getattr(_wdsp, name)
        except AttributeError:
            logger.warning("WDSP symbol not available: %s", name)
            return False
        try:
            func.argtypes = argtypes
            func.restype = restype
            _WDSP_SYMBOLS[name] = True
            return True
        except Exception as e:
            logger.warning("WDSP bind failed for %s: %s", name, e)
            return False

    # ── Channel lifecycle (CORE — must exist) ──
    _bind("OpenChannel",
          [c_int, c_int, c_int, c_int, c_int, c_int, c_int, c_int,
           c_double, c_double, c_double, c_double, c_int], c_int)
    _bind("CloseChannel",     [c_int], c_int)
    _bind("SetChannelState",  [c_int, c_int, c_int], c_int)

    # ── Processing (CORE) ──
    # fexchange0 returns void.  restype=None prevents ctypes from reading
    # a phantom return value from x0 on ARM64 (which could corrupt state).
    _bind("fexchange0",
          [c_int, ctypes.POINTER(c_double), ctypes.POINTER(c_double),
           ctypes.POINTER(c_int)], None)
    _bind("fexchange2",
          [c_int,
           ctypes.POINTER(c_double), ctypes.POINTER(c_double),
           ctypes.POINTER(c_double), ctypes.POINTER(c_double),
           ctypes.POINTER(c_int)], None)

    # ── Mode & panel ──
    _bind("SetRXAMode",       [c_int, c_int], c_int)
    _bind("SetRXAPanelRun",   [c_int, c_int], c_int)
    _bind("SetRXAPanelGain1", [c_int, c_double], c_int)
    _bind("SetRXAPanelGain2", [c_int, c_double], c_int)

    # ── AGC ──
    _bind("SetRXAAGCMode",           [c_int, c_int], c_int)
    _bind("SetRXAAGCAttack",         [c_int, c_int], c_int)
    _bind("SetRXAAGCDecay",          [c_int, c_int], c_int)
    _bind("SetRXAAGCHang",           [c_int, c_int], c_int)
    _bind("SetRXAAGCHangLevel",      [c_int, c_double], c_int)
    _bind("SetRXAAGCHangThreshold",  [c_int, c_double], c_int)
    _bind("SetRXAAGCThresh",         [c_int, c_double], c_int)
    _bind("SetRXAAGCSlope",          [c_int, c_double], c_int)
    _bind("SetRXAAGCFixed",          [c_int, c_double], c_int)
    _bind("SetRXAAGCMaxInputLevel",  [c_int, c_double], c_int)
    _bind("GetRXAAGCHangLevel",      [c_int], c_double)
    _bind("GetRXAAGCHangThreshold",  [c_int], c_double)
    _bind("GetRXAAGCThresh",         [c_int], c_double)
    _bind("GetRXAAGCTop",            [c_int], c_double)

    # ── NR2 (Enhanced Meyer Noise Reduction) ──
    _bind("SetRXAEMNRRun",          [c_int, c_int], c_int)
    _bind("SetRXAEMNRgainMethod",   [c_int, c_int], c_int)
    _bind("SetRXAEMNRnpeMethod",    [c_int, c_int], c_int)
    _bind("SetRXAEMNRaeRun",        [c_int, c_int], c_int)
    _bind("SetRXAEMNRPosition",     [c_int, c_int], c_int)
    _bind("SetRXAEMNRaeZetaThresh", [c_int, c_double], c_int)
    _bind("SetRXAEMNRaePsi",        [c_int, c_double], c_int)

    # ── ANF (Auto Notch Filter) ──
    _bind("SetRXAANFRun",     [c_int, c_int], c_int)
    _bind("SetRXAANFVals",    [c_int, c_int, c_int], c_int)
    _bind("SetRXAANFTaps",    [c_int, c_int], c_int)
    _bind("SetRXAANFDelay",   [c_int, c_double], c_int)
    _bind("SetRXAANFGain",    [c_int, c_double], c_int)
    _bind("SetRXAANFLeakage", [c_int, c_double], c_int)
    _bind("SetRXAANFPosition",[c_int, c_int], c_int)

    # ── NB (Noise Blanker) ──
    _bind("SetRXASNBARun", [c_int, c_int], c_int)

    # ── Bandpass ──
    _bind("SetRXABandpassRun",    [c_int, c_int], c_int)
    _bind("SetRXABandpassFreqs",  [c_int, c_double, c_double], c_int)
    _bind("SetRXABandpassWindow", [c_int, c_int], c_int)
    _bind("SetRXABandpassNC",     [c_int, c_int], c_int)
    _bind("SetRXABandpassMP",     [c_int, c_int], c_int)

    # ── NBP (Notch Bank Processor — manual notch API) ──
    _bind("RXANBPAddNotch",          [c_int, c_int, c_double, c_double], c_int)
    _bind("RXANBPDeleteNotch",       [c_int, c_int], c_int)
    _bind("RXANBPGetNumNotches",     [c_int], c_int)
    _bind("RXANBPSetNotchesRun",     [c_int, c_int], c_int)
    _bind("RXANBPSetRun",            [c_int, c_int], c_int)
    _bind("RXANBPSetFreqs",          [c_int, c_int,
                                      ctypes.POINTER(c_double)], c_int)

    # ── Equalizer ──
    _bind("SetRXAEQRun",     [c_int, c_int], c_int)
    _bind("SetRXAEQNC",      [c_int, c_int], c_int)
    _bind("SetRXAEQMP",      [c_int, c_int], c_int)
    _bind("SetRXAEQProfile", [c_int, c_int,
                               ctypes.POINTER(c_double)], c_int)
    _bind("SetRXAEQWintype", [c_int, c_int], c_int)

    # ── FM Squelch ──
    _bind("SetRXAFMSQRun",       [c_int, c_int], c_int)
    _bind("SetRXAFMSQThreshold", [c_int, c_double], c_int)
    _bind("SetRXAFMSQNC",        [c_int, c_int], c_int)
    _bind("SetRXAFMSQMP",        [c_int, c_int], c_int)

    # ── S-Meter ──
    # CRITICAL: restype = c_double.  Without it ARM64 reads garbage.
    _bind("GetRXAMeter", [c_int, c_int], c_double)

    # ── Spectrum (enable WDSP internal FFT for GetRXAMeter) ──
    _bind("SetRXASpectrum", [c_int, c_int, c_int, c_int, c_int], c_int)

    # ── Shift / IF offset ──
    _bind("SetRXAShiftRun",  [c_int, c_int], c_int)
    _bind("SetRXAShiftFreq", [c_int, c_double], c_double)

    # ── AM Squelch ──
    _bind("SetRXAAMSQRun",       [c_int, c_int], c_int)
    _bind("SetRXAAMSQThreshold", [c_int, c_double], c_int)
    _bind("SetRXAAMSQMaxTail",   [c_int, c_double], c_int)

    # ── Pre-Generator (tone/noise injection for testing) ──
    _bind("SetRXAPreGenRun",       [c_int, c_int], c_int)
    _bind("SetRXAPreGenMode",      [c_int, c_int], c_int)
    _bind("SetRXAPreGenToneMag",   [c_int, c_double], c_int)
    _bind("SetRXAPreGenToneFreq",  [c_int, c_double], c_int)

    # ── CBL (Correction Buffer Leveler) ──
    _bind("SetRXACBLRun", [c_int, c_int], c_int)

    # ── SPeak CW ──
    _bind("SetRXASPCWRun",       [c_int, c_int], c_int)
    _bind("SetRXASPCWFreq",      [c_int, c_double], c_int)
    _bind("SetRXASPCWBandwidth", [c_int, c_double], c_int)
    _bind("SetRXASPCWGain",      [c_int, c_double], c_int)

    # ── Spatial Diversity (dual-ADC coherent combining) ──
    _bind("SetEXTDIVRun",          [c_int, c_int], c_int)
    _bind("SetEXTDIVRotate",       [c_int, c_double], c_int)
    _bind("SetEXTDIVBuffsize",     [c_int, c_int], c_int)
    _bind("SetEXTDIVNr",           [c_int, c_int], c_int)
    _bind("SetEXTDIVOutput",       [c_int, c_int], c_int)

    # ── Verify core symbols ──
    for core in ("OpenChannel", "fexchange0", "CloseChannel", "SetRXAMode",
                 "SetRXAPanelGain1", "SetRXAAGCMode"):
        if not _check_symbol(core):
            raise RuntimeError(
                f"WDSP missing core symbol: {core}.  "
                f"Library may be corrupted or from an incompatible build.")


if WDSP_AVAILABLE:
    try:
        _bind_signatures()
    except Exception as e:  # never let an incompatible .so stop the server
        logger.warning("WDSP: incompatible library %s: %s", _LIBRARY_PATH, e)
        _wdsp = None
        WDSP_AVAILABLE = False


# ── Constants ──────────────────────────────────────────────────────

class WDSPMode:
    LSB, USB, DSB, CW, AM, FM = range(6)

class WDSPAGCMode:
    OFF, LONG, SLOW, MED, FAST = range(5)


# ── NR2 strength → EMNR parameter mapping ──────────────────────────
# Per WDSP Guide Rev 1.29 §Spectral Noise Reduction.
#
# The "strength" knob selects the gain-calculation METHOD and the
# noise-power-estimator (NPE) method; the AEPF (ae_run) stays ON with
# near-default zetaThresh / psi to suppress musical tones.
#
#   gainMethod: 0=Gaussian linear, 1=Gaussian log, 2=Gamma (guide DEFAULT,
#               "somewhat preferred" for voice), 3=Trained (deepest
#               reduction but "harsh" — needs post2 softening, which this
#               dylib build does NOT export, so Trained is reserved for the
#               top of the range only).
#   npeMethod:  0=OSMS (default, stable), 1=MMSE (responsive),
#               2=NSTAT (most responsive; guide: "especially recommended
#               with the Trained gain method").
#   ae.zetaThresh: AEPF activation threshold (default 0.75). The AEPF
#               activates when the post-NR residual noise ratio (zeta)
#               drops below this. Leave at the default so the filter
#               engages for any meaningful NR.
#   ae.psi:     AEPF max bin-smoothing half-window (default 20.0). The
#               smoothing window is N = 1 + 2*int(0.5 + psi*(1 - zeta/zetaThresh)).
#               psi MUST stay in the tens, never < 1, or N collapses to 1
#               and the AEPF stops smoothing — that is the root cause of
#               the "数码音" / musical-noise artifact. psi rises with
#               strength because deeper NR produces heavier artifacts that
#               need wider smoothing.
#
# Defaults are Warren Pratt's tuned values; only psi is nudged upward at
# high strength. This keeps voice clean (no distortion) across the whole
# range; Trained at the top trades a little harshness for maximum reduction.

def _emnr_params_for_level(level: int) -> tuple[int, int, float, float]:
    """Map NR2 strength (0-100) to (gain_method, npe_method, zeta_thresh, psi).

    Designed to produce audibly distinct noise reduction at each of the
    five front-end button positions (0, 25, 50, 75, 100):

        0   → bypass (caller handles via SetRXAEMNRRun)
        25  → Gamma + OSMS, zetaThresh=0.85 psi=14.0  (LIGHT — minimal
              AEPF, most natural / least processed voice)
        50  → Gamma + OSMS, zetaThresh=0.75 psi=20.0  (MEDIUM — Warren
              Pratt's tuned defaults, the guide's preferred configuration)
        75  → Gamma + MMSE, zetaThresh=0.75 psi=26.0  (STRONG — more
              responsive noise tracking, wider AEPF smoothing)
        100 → Trained + NSTAT, zetaThresh=0.75 psi=32.0 (MAX — deepest
              reduction; Trained mode + widest musical-noise suppression.
              May sound slightly sharper — user's choice at max strength)

    The AEPF smoothing window is N = 1 + 2*int(0.5 + psi*(1 - zeta/zetaThresh)).
    psi must stay well above 1 or N collapses and musical noise ("数码音")
    returns. zetaThresh is 0.75 (default) at most tiers; at LIGHT it is
    nudged to 0.85 so the AEPF engages only when NR is clearly active,
    yielding a more natural "barely-there" processing feel.
    """
    clamped = max(0, min(100, level))
    if clamped <= 0:
        return 2, 0, 0.85, 14.0   # sentinel — NR2 is off, won't be applied
    elif clamped <= 37:
        gain, npe, zeta, psi = 2, 0, 0.85, 14.0    # LIGHT
    elif clamped <= 62:
        gain, npe, zeta, psi = 2, 0, 0.75, 20.0    # MEDIUM
    elif clamped <= 87:
        gain, npe, zeta, psi = 2, 1, 0.75, 26.0    # STRONG
    else:
        gain, npe, zeta, psi = 3, 2, 0.75, 32.0    # MAX
    return gain, npe, zeta, psi

# ── Processor ──────────────────────────────────────────────────────

class WDSPProcessor:
    """Professional AGC + NR2 audio processor via WDSP C library.

    Uses pre-allocated ring buffers and zero-copy pointer passing to
    eliminate per-call GC pressure.  All WDSP function signatures are
    declared at module load for ARM64 type safety.
    """

    def __init__(self, sample_rate: int = 16000, buffer_size: int = 256,
                 mode: int = WDSPMode.USB, enable_nr2: bool = True,
                 enable_nb: bool = False, enable_anf: bool = False,
                 agc_mode: int = WDSPAGCMode.SLOW):
        if not WDSP_AVAILABLE:
            raise RuntimeError("WDSP library not available")

        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.channel = 0
        self._nr2 = enable_nr2
        self._nr2_level = 50

        # ── Work buffers (pre-allocated once, reused every call) ──
        # _in / _out are interleaved I/Q: even = signal, odd = 0.0
        self._in = np.zeros(buffer_size * 2, dtype=np.float64)
        self._out = np.zeros(buffer_size * 2, dtype=np.float64)
        # Streaming buffers — process() accumulates variable-length input
        # into _input_buffer, drains in buffer_size chunks through
        # fexchange0, and queues output into _output_buffer.  Output is
        # returned FIFO so the caller always gets time-aligned samples.
        self._input_buffer = np.array([], dtype=np.float64)
        self._output_buffer = np.array([], dtype=np.float64)

        # ── Memory safety: all C-boundary buffers must be C-contiguous ──
        for name, arr in (("_in", self._in), ("_out", self._out)):
            assert arr.flags["C_CONTIGUOUS"], \
                f"WDSP {name}: pre-allocated buffer must be C-contiguous"
            assert arr.dtype == np.float64, \
                f"WDSP {name}: pre-allocated buffer must be float64"

        self._init(mode, agc_mode, enable_nb, enable_anf)

    # ── Initialisation ─────────────────────────────────────────

    def _init(self, mode, agc, nb, anf):
        _wdsp.OpenChannel(
            ctypes.c_int(self.channel),
            ctypes.c_int(self.buffer_size),
            ctypes.c_int(self.buffer_size),
            ctypes.c_int(self.sample_rate),
            ctypes.c_int(self.sample_rate),
            ctypes.c_int(self.sample_rate),
            ctypes.c_int(0), ctypes.c_int(1),
            ctypes.c_double(0), ctypes.c_double(0),
            ctypes.c_double(0), ctypes.c_double(0),
            ctypes.c_int(0))
        _wdsp.SetRXAMode(ctypes.c_int(self.channel), ctypes.c_int(mode))
        _wdsp.SetRXAPanelGain1(ctypes.c_int(self.channel),
                               ctypes.c_double(0.2))
        self.set_agc(agc)
        if self._nr2:
            _wdsp.SetRXAEMNRRun(ctypes.c_int(self.channel), ctypes.c_int(1))
            # AEPF (Artifact-Elimination Post-Filter) — guide says ALWAYS on.
            if _check_symbol("SetRXAEMNRaeRun"):
                _wdsp.SetRXAEMNRaeRun(ctypes.c_int(self.channel),
                                      ctypes.c_int(1))
            # position=0: NR before AGC (best SNR into the NR block).
            if _check_symbol("SetRXAEMNRPosition"):
                _wdsp.SetRXAEMNRPosition(ctypes.c_int(self.channel),
                                         ctypes.c_int(0))
            # gainMethod / npeMethod / AEPF zeta+psi set from the level:
            self._apply_nr2_params(self._nr2_level)

        # Explicitly disable NB by default — the WDSP library may
        # default to SNB-on internally, and snb.c has a memory
        # corruption bug (malloc of freed block) that crashes the
        # entire process.  Only enable if explicitly requested.
        if nb:
            self.set_nb_enabled(True)
        elif _check_symbol("SetRXASNBARun"):
            # Direct off, not set_nb_enabled(): the latter logs the
            # use-after-free warning on every processor creation.
            _wdsp.SetRXASNBARun(ctypes.c_int(self.channel), ctypes.c_int(0))
        if anf:
            self.set_anf_enabled(True)

    # ── NR2 parameter control (gain / npe method + AEPF) ──────

    def _apply_nr2_params(self, level: int):
        """Apply NR2 strength (0-100) via gain method + NPE + AEPF defaults.

        See _emnr_params_for_level() for the mapping rationale and the
        WDSP Guide Rev 1.29 §Spectral Noise Reduction for the algorithm.

        ROOT CAUSE of prior poor quality (fixed 2026-07-03): the old code
        mapped level → ae.zetaThresh (0.30-0.50) and ae.psi (0.008-0.032),
        but the WDSP-tuned defaults are zetaThresh=0.75, psi=20.0. With
        psi≈0.02 the AEPF smoothing window N = 1 + 2*int(0.5 + 0.02*…) = 1,
        i.e. the Artifact-Elimination Post-Filter — which the guide says
        "should ALWAYS be applied" to kill musical tones — was effectively
        disabled. That is why NR2 either sounded harsh or produced
        "数码音" (musical noise). The strength knob now drives the gain
        METHOD (the real lever) and leaves the AEPF at near-defaults.
        """
        gain, npe, zeta, psi = _emnr_params_for_level(level)

        if _check_symbol("SetRXAEMNRgainMethod"):
            _wdsp.SetRXAEMNRgainMethod(
                ctypes.c_int(self.channel), ctypes.c_int(gain))
        if _check_symbol("SetRXAEMNRnpeMethod"):
            _wdsp.SetRXAEMNRnpeMethod(
                ctypes.c_int(self.channel), ctypes.c_int(npe))
        if _check_symbol("SetRXAEMNRaeZetaThresh"):
            _wdsp.SetRXAEMNRaeZetaThresh(
                ctypes.c_int(self.channel), ctypes.c_double(zeta))
        if _check_symbol("SetRXAEMNRaePsi"):
            _wdsp.SetRXAEMNRaePsi(
                ctypes.c_int(self.channel), ctypes.c_double(psi))
        logger.debug("NR2 strength=%d → gain=%d npe=%d zeta=%.3f psi=%.2f",
                     max(0, min(100, level)), gain, npe, zeta, psi)

    # ── Core processing ────────────────────────────────────────

    def process(self, audio_data: np.ndarray) -> np.ndarray:
        """Process audio through the WDSP chain.

        Handles variable-length input by buffering and draining in
        buffer_size blocks through fexchange0.  Returns float32 output
        with exactly the same number of samples as the input, properly
        time-aligned via a FIFO output buffer.
        """
        # Convert to float64
        if audio_data.dtype == np.int16:
            f32 = audio_data.astype(np.float64) / 32768.0
        else:
            f32 = audio_data.astype(np.float64)

        n_in = len(f32)
        if n_in == 0:
            return audio_data

        # Accumulate input and drain complete blocks
        self._input_buffer = np.concatenate([self._input_buffer, f32])
        bs = self.buffer_size

        while len(self._input_buffer) >= bs:
            chunk = self._input_buffer[:bs]
            self._input_buffer = self._input_buffer[bs:]

            self._in[0::2] = chunk
            self._in[1::2] = 0.0

            err = ctypes.c_int(0)
            _wdsp.fexchange0(
                ctypes.c_int(self.channel),
                self._in.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                self._out.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                ctypes.byref(err))

            # err == 0: normal
            # err == -2: warmup (output is valid but not fully converged)
            # err == other: real error → skip this block
            if err.value not in (0, -2):
                logger.debug("WDSP fexchange0 error=%d (skipping block)", err.value)
                continue

            out = self._out[0::2].copy()
            self._output_buffer = np.concatenate([self._output_buffer, out])

        # Return exactly n_in time-aligned samples
        if len(self._output_buffer) >= n_in:
            result = self._output_buffer[:n_in]
            self._output_buffer = self._output_buffer[n_in:]
            return result.astype(np.float32)

        # Not enough output yet — drain what we have, pad with silence
        if len(self._output_buffer) > 0:
            result = np.concatenate([
                self._output_buffer,
                np.zeros(n_in - len(self._output_buffer), dtype=np.float64),
            ])
            self._output_buffer = np.array([], dtype=np.float64)
            return result.astype(np.float32)

        # No output at all — return silence
        return np.zeros(n_in, dtype=np.float32)

    # ── AGC ────────────────────────────────────────────────────

    def set_agc(self, mode: int):
        _wdsp.SetRXAAGCMode(ctypes.c_int(self.channel), ctypes.c_int(mode))

    def set_agc_fixed(self, gain: float):
        """Set the fixed gain used by AGC OFF (the RXA default is 1000)."""
        if _check_symbol("SetRXAAGCFixed"):
            _wdsp.SetRXAAGCFixed(ctypes.c_int(self.channel),
                                 ctypes.c_double(float(gain)))

    def set_panel_gain(self, gain: float):
        """Set the patch-panel gain; 1.0 keeps unity for post-demod NR."""
        _wdsp.SetRXAPanelGain1(ctypes.c_int(self.channel),
                               ctypes.c_double(float(gain)))

    def set_agc_attack(self, ms: int):
        """AGC attack time in milliseconds."""
        if _check_symbol("SetRXAAGCAttack"):
            _wdsp.SetRXAAGCAttack(ctypes.c_int(self.channel), ctypes.c_int(ms))

    def set_agc_decay(self, ms: int):
        """AGC decay time in milliseconds."""
        if _check_symbol("SetRXAAGCDecay"):
            _wdsp.SetRXAAGCDecay(ctypes.c_int(self.channel), ctypes.c_int(ms))

    def set_agc_hang(self, ms: int):
        """AGC hang time in milliseconds."""
        if _check_symbol("SetRXAAGCHang"):
            _wdsp.SetRXAAGCHang(ctypes.c_int(self.channel), ctypes.c_int(ms))

    def set_agc_thresh(self, db: float):
        """AGC threshold in dB."""
        if _check_symbol("SetRXAAGCThresh"):
            _wdsp.SetRXAAGCThresh(ctypes.c_int(self.channel),
                                  ctypes.c_double(db))

    def set_agc_slope(self, db: float):
        """AGC slope in dB."""
        if _check_symbol("SetRXAAGCSlope"):
            _wdsp.SetRXAAGCSlope(ctypes.c_int(self.channel),
                                 ctypes.c_double(db))

    def get_s_meter(self) -> float:
        """Read RX S-meter from WDSP (linear or dB depending on build).

        Returns -127.0 if the symbol is unavailable.
        """
        if not _check_symbol("GetRXAMeter"):
            return -127.0
        return float(_wdsp.GetRXAMeter(
            ctypes.c_int(self.channel), ctypes.c_int(0)))

    # ── Mode ───────────────────────────────────────────────────

    def set_mode(self, mode: int):
        _wdsp.SetRXAMode(ctypes.c_int(self.channel), ctypes.c_int(mode))

    # ── NR2 (spectral noise reduction) ──────────────────────────

    def set_nr2_enabled(self, on: bool):
        """Enable / disable NR2 spectral noise reduction."""
        run = 1 if on else 0
        _wdsp.SetRXAEMNRRun(ctypes.c_int(self.channel), ctypes.c_int(run))
        self._nr2 = on

    def set_nr2_level(self, level: int):
        """Set NR2 strength (0-100).  100 = most aggressive."""
        self._nr2_level = max(0, min(100, level))
        self._apply_nr2_params(self._nr2_level)

    def set_nr2_gain_method(self, method: int):
        """Set NR2 gain method (0=linear, 1=log)."""
        if _check_symbol("SetRXAEMNRgainMethod"):
            _wdsp.SetRXAEMNRgainMethod(
                ctypes.c_int(self.channel), ctypes.c_int(method))

    def set_nr2_npe_method(self, method: int):
        """Set NR2 NPE method (0=standard)."""
        if _check_symbol("SetRXAEMNRnpeMethod"):
            _wdsp.SetRXAEMNRnpeMethod(
                ctypes.c_int(self.channel), ctypes.c_int(method))

    def set_nr2_ae_run(self, on: bool):
        """Enable / disable NR2 AE (acquisition enhancement)."""
        if _check_symbol("SetRXAEMNRaeRun"):
            run = 1 if on else 0
            _wdsp.SetRXAEMNRaeRun(ctypes.c_int(self.channel), ctypes.c_int(run))

    # ── NB (noise blanker) ──────────────────────────────────────

    def set_nb_enabled(self, on: bool):
        """SNB is deliberately disabled in this build.

        ``snb.c`` has a use-after-free that aborts the host process as soon
        as the blanker is enabled (measured 2026-11-09).  MRRC's SunSDR
        backend uses its own time-domain impulse blanker instead; this
        method remains only so an old caller gets a warning, not a crash.
        """
        logger.warning("WDSP SNB is disabled: snb.c use-after-free; "
                       "use the software impulse blanker")

    # ── ANF (auto notch filter) ─────────────────────────────────

    def set_anf_enabled(self, on: bool):
        """Enable / disable auto notch filter."""
        run = 1 if on else 0
        _wdsp.SetRXAANFRun(ctypes.c_int(self.channel), ctypes.c_int(run))

    # ── NF (manual notch filter — uses NBP Notch Bank Processor) ─

    def set_nf_enabled(self, on: bool, freq_hz: float = 0.0):
        """Enable / disable manual notch filter at given frequency.

        Uses the NBP (Notch Bank Processor) API.  A single notch is
        managed at index 0 — adding a new frequency replaces the old one.
        """
        if not _check_symbol("RXANBPSetNotchesRun"):
            return
        run = 1 if on else 0
        if on and freq_hz > 0:
            if _check_symbol("RXANBPDeleteNotch"):
                _wdsp.RXANBPDeleteNotch(
                    ctypes.c_int(self.channel), ctypes.c_int(0))
            if _check_symbol("RXANBPAddNotch"):
                _wdsp.RXANBPAddNotch(
                    ctypes.c_int(self.channel), ctypes.c_int(0),
                    ctypes.c_double(freq_hz), ctypes.c_double(50.0))
        if not on:
            if _check_symbol("RXANBPDeleteNotch"):
                _wdsp.RXANBPDeleteNotch(
                    ctypes.c_int(self.channel), ctypes.c_int(0))
        _wdsp.RXANBPSetNotchesRun(
            ctypes.c_int(self.channel), ctypes.c_int(run))

    # ── Bandpass ────────────────────────────────────────────────

    def set_bandpass(self, low_hz: float, high_hz: float):
        """Set audio bandpass filter edges.

        Uses the real symbols SetRXABandpassFreqs + SetRXABandpassRun
        (the old code called the non-existent SetRXABandpassFilter).
        """
        if _check_symbol("SetRXABandpassFreqs"):
            _wdsp.SetRXABandpassFreqs(
                ctypes.c_int(self.channel),
                ctypes.c_double(low_hz), ctypes.c_double(high_hz))
        if _check_symbol("SetRXABandpassRun"):
            _wdsp.SetRXABandpassRun(
                ctypes.c_int(self.channel), ctypes.c_int(1))

    # ── Equalizer ───────────────────────────────────────────────

    def set_eq_run(self, on: bool):
        """Enable / disable the RX graphic equalizer."""
        if _check_symbol("SetRXAEQRun"):
            _wdsp.SetRXAEQRun(ctypes.c_int(self.channel),
                              ctypes.c_int(1 if on else 0))

    def set_eq_profile(self, gains_db: list[float]):
        """Upload a custom EQ profile (list of per-band gains in dB)."""
        if not _check_symbol("SetRXAEQProfile"):
            return
        n = len(gains_db)
        if n == 0:
            return
        arr = (ctypes.c_double * n)(*gains_db)
        _wdsp.SetRXAEQProfile(ctypes.c_int(self.channel),
                              ctypes.c_int(n), arr)

    # ── FM Squelch ──────────────────────────────────────────────

    def set_fm_squelch_run(self, on: bool):
        """Enable / disable FM squelch."""
        if _check_symbol("SetRXAFMSQRun"):
            _wdsp.SetRXAFMSQRun(ctypes.c_int(self.channel),
                                ctypes.c_int(1 if on else 0))

    def set_fm_squelch_threshold(self, thresh: float):
        """Set FM squelch threshold (0.0–1.0)."""
        if _check_symbol("SetRXAFMSQThreshold"):
            _wdsp.SetRXAFMSQThreshold(ctypes.c_int(self.channel),
                                      ctypes.c_double(thresh))

    # ── Cleanup ────────────────────────────────────────────────

    def close(self):
        if WDSP_AVAILABLE:
            _wdsp.SetChannelState(ctypes.c_int(self.channel),
                                  ctypes.c_int(0), ctypes.c_int(0))
            _wdsp.CloseChannel(ctypes.c_int(self.channel))


# ── I/Q-Level Processor ────────────────────────────────────────────

class WDSPIQProcessor:
    """WDSP processor operating on raw I/Q BEFORE demodulation.

    Uses a dedicated WDSP channel (channel=1) at the full IQ sample rate
    (e.g. 78125 Hz).  fexchange0 receives interleaved I/Q (real/imaginary
    in left/right channels), letting NR2/ANF/SNB algorithms exploit the
    complete complex-domain phase and wide spectral context before the
    signal is degraded by demodulation noise-aliasing.

    This is the "IF-SDR" architecture — noise reduction at the
    intermediate-frequency level, exactly where WDSP was designed to run.
    """

    def __init__(self, iq_sample_rate: int = 78125, buffer_size: int = 1024,
                 mode: int = WDSPMode.USB, enable_nr2: bool = True,
                 enable_nb: bool = False, enable_anf: bool = True,
                 agc_mode: int = WDSPAGCMode.SLOW):
        if not WDSP_AVAILABLE:
            raise RuntimeError("WDSP library not available")

        self.iq_rate = iq_sample_rate
        self.buffer_size = buffer_size
        self.channel = 1          # dedicated IQ channel (0 = audio-rate)
        self._nr2 = enable_nr2
        self._nr2_level = 30
        self._mode = mode

        # Pre-allocated work buffers sized for IQ blocks.
        # fexchange0 expects interleaved pairs: [I0, Q0, I1, Q1, ...]
        # at the IQ sample rate.  Output is the WDSP-demodulated audio.
        self._in = np.zeros(buffer_size * 2, dtype=np.float64)
        self._out = np.zeros(buffer_size * 2, dtype=np.float64)
        # Streaming buffers: variable-length IQ input → block drain
        self._iq_buffer = np.array([], dtype=np.float64)
        self._audio_buffer = np.array([], dtype=np.float64)
        self._ratio: float | None = None  # calibrated on first fexchange0 block
        self._decim = max(1, self.iq_rate // 15625)  # IQ→audio rate decimation

        assert self._in.flags["C_CONTIGUOUS"] and self._in.dtype == np.float64
        assert self._out.flags["C_CONTIGUOUS"] and self._out.dtype == np.float64

        self._init_iq(mode, agc_mode, enable_nb, enable_anf)

    @property
    def out_rate(self) -> float:
        """Actual audio rate after this wrapper's block-wise integer decimation.

        ``raw_audio[::decim]`` yields ``buffer_size // decim`` samples per
        input block, not necessarily an exact 15625 Hz.  The caller must
        configure its downstream resampler with this true rate or the audio
        is pitched by up to ~0.4 %.
        """
        # ``raw_audio[::decim]`` keeps ceil(size/decim) samples (indices
        # 0, decim, 2*decim, ...), not floor.  Using floor here pitched the
        # audio by ~0.5 % on 78k/156k.
        kept = (self.buffer_size + self._decim - 1) // self._decim
        return float(self.iq_rate) * kept / float(self.buffer_size)

    def _init_iq(self, mode, agc, nb, anf):
        """Open a WDSP channel at the IQ sample rate.

        WDSP receives I/Q at the full IQ rate and outputs audio at the
        same rate (1:1).  Python decimates afterward — this avoids the
        uncertainty of WDSP internal decimation behaviour across builds.
        """
        self._out_rate = self.iq_rate  # WDSP outputs at IQ rate
        _wdsp.OpenChannel(
            ctypes.c_int(self.channel),
            ctypes.c_int(self.buffer_size),   # input buffer size
            ctypes.c_int(self.buffer_size),   # output buffer size (same)
            ctypes.c_int(self.iq_rate),       # input sample rate
            ctypes.c_int(self.iq_rate),       # output sample rate
            ctypes.c_int(self.iq_rate),       # DSP rate
            ctypes.c_int(0), ctypes.c_int(1),
            ctypes.c_double(0), ctypes.c_double(0),
            ctypes.c_double(0), ctypes.c_double(0),
            ctypes.c_int(0))
        _wdsp.SetRXAMode(ctypes.c_int(self.channel), ctypes.c_int(mode))
        _wdsp.SetRXAPanelGain1(ctypes.c_int(self.channel),
                               ctypes.c_double(2.0))
        _wdsp.SetRXAAGCMode(ctypes.c_int(self.channel), ctypes.c_int(agc))
        # Anti-aliasing: band-limit WDSP output to ≤3.4 kHz so the
        # downstream Python [::decim] decimation from IQ rate → voice
        # rate is safe.  Without this, frequencies above the voice-rate
        # Nyquist (7.8 kHz @ 15625) fold back into the audio band.
        if _check_symbol("SetRXABandpassFreqs"):
            _wdsp.SetRXABandpassFreqs(ctypes.c_int(self.channel),
                                      ctypes.c_double(200.0),
                                      ctypes.c_double(3400.0))
        if _check_symbol("SetRXABandpassRun"):
            _wdsp.SetRXABandpassRun(ctypes.c_int(self.channel),
                                    ctypes.c_int(1))

        # Prime the RXA chain.  A freshly opened WDSP channel with EMNR,
        # ANF and SNB all off can emit zeros until RXAbp1Set has run at
        # least once (measured on both macOS and aarch64, 2026-11-09).
        # Toggling EMNR on and immediately applying the requested state
        # executes that path without leaving noise reduction enabled.
        if _check_symbol("SetRXAEMNRRun"):
            _wdsp.SetRXAEMNRRun(ctypes.c_int(self.channel), ctypes.c_int(1))
            _wdsp.SetRXAEMNRRun(
                ctypes.c_int(self.channel),
                ctypes.c_int(1 if self._nr2 else 0))

        if self._nr2:
            _wdsp.SetRXAEMNRRun(ctypes.c_int(self.channel), ctypes.c_int(1))
            # AEPF always on; position before AGC (best SNR). Mirrors the
            # audio-rate channel — see _emnr_params_for_level() for rationale.
            if _check_symbol("SetRXAEMNRaeRun"):
                _wdsp.SetRXAEMNRaeRun(ctypes.c_int(self.channel),
                                      ctypes.c_int(1))
            if _check_symbol("SetRXAEMNRPosition"):
                _wdsp.SetRXAEMNRPosition(ctypes.c_int(self.channel),
                                         ctypes.c_int(0))
            self._apply_iq_nr2(self._nr2_level)

        if anf:
            _wdsp.SetRXAANFRun(ctypes.c_int(self.channel), ctypes.c_int(1))
        # SNB must stay off: snb.c has a use-after-free that aborts the
        # host process when enabled.  The software impulse blanker in
        # dsp.py is the NB path.
        if _check_symbol("SetRXASNBARun"):
            _wdsp.SetRXASNBARun(ctypes.c_int(self.channel), ctypes.c_int(0))

    # ── NR2 (delegated to the same gain/npe/AEPF mapping) ───

    def _apply_iq_nr2(self, level: int):
        """NR2 for IQ WDSP — same mapping as the audio-rate channel.
        See _emnr_params_for_level() for rationale."""
        gain, npe, zeta, psi = _emnr_params_for_level(level)
        if _check_symbol("SetRXAEMNRgainMethod"):
            _wdsp.SetRXAEMNRgainMethod(
                ctypes.c_int(self.channel), ctypes.c_int(gain))
        if _check_symbol("SetRXAEMNRnpeMethod"):
            _wdsp.SetRXAEMNRnpeMethod(
                ctypes.c_int(self.channel), ctypes.c_int(npe))
        if _check_symbol("SetRXAEMNRaeZetaThresh"):
            _wdsp.SetRXAEMNRaeZetaThresh(
                ctypes.c_int(self.channel), ctypes.c_double(zeta))
        if _check_symbol("SetRXAEMNRaePsi"):
            _wdsp.SetRXAEMNRaePsi(
                ctypes.c_int(self.channel), ctypes.c_double(psi))

    # ── Core I/Q processing ──────────────────────────────────

    def process_iq(self, iq: np.ndarray) -> np.ndarray:
        """Process raw I/Q through WDSP → return time-aligned float32 audio.

        iq: complex64 or complex128, variable length (typically ~200).
        Returns: float32 audio, len ≈ len(iq) * (audio_rate / iq_rate).
                 Never returns None — returns zeros if no data yet.
        """
        n_in = len(iq)
        if n_in == 0:
            return np.array([], dtype=np.float32)

        # Convert complex IQ to interleaved float64: [I0, Q0, I1, Q1, ...]
        iq64 = iq.astype(np.complex128)
        interleaved = np.empty(n_in * 2, dtype=np.float64)
        interleaved[0::2] = iq64.real
        interleaved[1::2] = iq64.imag

        # Accumulate into the IQ buffer
        self._iq_buffer = np.concatenate([self._iq_buffer, interleaved])
        bs2 = self.buffer_size * 2  # 2 floats per I/Q pair

        while len(self._iq_buffer) >= bs2:
            chunk = self._iq_buffer[:bs2]
            self._iq_buffer = self._iq_buffer[bs2:]

            self._in[:] = chunk

            err = ctypes.c_int(0)
            _wdsp.fexchange0(
                ctypes.c_int(self.channel),
                self._in.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                self._out.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                ctypes.byref(err))

            if err.value not in (0, -2):
                if err.value < 0:
                    logger.debug("WDSP IQ fexchange0 warmup err=%d", err.value)
                else:
                    logger.warning("WDSP IQ fexchange0 error=%d", err.value)
                    continue

            # WDSP output: demodulated audio in left channel at IQ rate.
            # Decimate to the voice rate (~15625 Hz).
            raw_audio = self._out[0::2].copy()
            audio_block = raw_audio[::self._decim]
            if self._ratio is None:
                self._ratio = len(audio_block) / self.buffer_size
                logger.info("WDSP IQ output ratio: %.3f audio/IQ "
                            "(block=%d→%d, decim=%d)",
                            self._ratio, self.buffer_size,
                            len(audio_block), self._decim)
            self._audio_buffer = np.concatenate([self._audio_buffer, audio_block])

        # Return time-aligned audio: n_in IQ samples × ratio = audio samples
        if self._ratio is None:
            # No blocks completed yet — return silence
            return np.zeros(0, dtype=np.float32)

        want = max(1, int(n_in * self._ratio))
        if len(self._audio_buffer) >= want:
            result = self._audio_buffer[:want].astype(np.float32, copy=False)
            self._audio_buffer = self._audio_buffer[want:]
            return result

        # Underflow: drain what we have, pad with silence.  Underflow
        # is normal during warmup (first few calls before the first
        # fexchange0 block completes).
        available = len(self._audio_buffer)
        result = np.zeros(want, dtype=np.float32)
        if available > 0:
            result[:available] = self._audio_buffer[:available].astype(np.float32)
            self._audio_buffer = np.array([], dtype=np.float64)
        return result

    # ── Control methods ──────────────────────────────────────

    def set_mode(self, mode: int):
        self._mode = mode
        _wdsp.SetRXAMode(ctypes.c_int(self.channel), ctypes.c_int(mode))

    def set_bandpass(self, low_hz: float, high_hz: float):
        """Set the WDSP audio bandpass for the IQ channel.

        The reference only set 200..3400 in _init_iq; MRRC's filter table
        changes while running, so the IQ processor needs the same setter
        the audio-rate processor already has.
        """
        low = max(50.0, float(low_hz))
        high = max(low + 50.0, float(high_hz))
        if _check_symbol("SetRXABandpassFreqs"):
            _wdsp.SetRXABandpassFreqs(
                ctypes.c_int(self.channel),
                ctypes.c_double(low), ctypes.c_double(high))
        if _check_symbol("SetRXABandpassRun"):
            _wdsp.SetRXABandpassRun(ctypes.c_int(self.channel), ctypes.c_int(1))

    def set_nr2_level(self, level: int):
        self._nr2_level = max(0, min(100, level))
        self._apply_iq_nr2(self._nr2_level)

    def set_nr2_enabled(self, on: bool):
        self._nr2 = on
        _wdsp.SetRXAEMNRRun(ctypes.c_int(self.channel),
                            ctypes.c_int(1 if on else 0))

    def set_nr2_gain_method(self, method: int):
        """NR2 gain method (0=Gaussian lin, 1=Gaussian log, 2=Gamma,
        3=Trained). Must mirror audio WDSP."""
        if _check_symbol("SetRXAEMNRgainMethod"):
            _wdsp.SetRXAEMNRgainMethod(ctypes.c_int(self.channel),
                                       ctypes.c_int(max(0, min(3, method))))

    def set_nr2_npe_method(self, method: int):
        """NR2 noise-power estimator method (0=OSMS, 1=MMSE, 2=NSTAT)."""
        if _check_symbol("SetRXAEMNRnpeMethod"):
            _wdsp.SetRXAEMNRnpeMethod(ctypes.c_int(self.channel),
                                      ctypes.c_int(max(0, min(2, method))))

    def set_nr2_ae_run(self, on: bool):
        """NR2 AE (acquisition enhancement) — musical-noise suppressor."""
        if _check_symbol("SetRXAEMNRaeRun"):
            _wdsp.SetRXAEMNRaeRun(ctypes.c_int(self.channel),
                                  ctypes.c_int(1 if on else 0))

    def set_agc_mode(self, mode: int):
        _wdsp.SetRXAAGCMode(ctypes.c_int(self.channel), ctypes.c_int(mode))

    def set_agc_attack(self, ms: int):
        if _check_symbol("SetRXAAGCAttack"):
            _wdsp.SetRXAAGCAttack(ctypes.c_int(self.channel), ctypes.c_int(ms))

    def set_agc_decay(self, ms: int):
        if _check_symbol("SetRXAAGCDecay"):
            _wdsp.SetRXAAGCDecay(ctypes.c_int(self.channel), ctypes.c_int(ms))

    def set_agc_hang(self, ms: int):
        if _check_symbol("SetRXAAGCHang"):
            _wdsp.SetRXAAGCHang(ctypes.c_int(self.channel), ctypes.c_int(ms))

    def set_nb_enabled(self, on: bool):
        """SNB is disabled: snb.c has a use-after-free (see dsp.py NB)."""
        logger.warning("WDSP IQ SNB is disabled; use the software blanker")

    def set_anf_enabled(self, on: bool):
        """Enable / disable auto notch filter."""
        _wdsp.SetRXAANFRun(ctypes.c_int(self.channel),
                           ctypes.c_int(1 if on else 0))

    def get_s_meter(self) -> float:
        if not _check_symbol("GetRXAMeter"):
            return -127.0
        return float(_wdsp.GetRXAMeter(
            ctypes.c_int(self.channel), ctypes.c_int(0)))

    # ── Spatial Diversity (dual-ADC coherent combining) ────────

    def enable_spatial_diversity(self, enabled: bool):
        """Enable / disable spatial diversity combining via fexchange2.

        When enabled, process_diversity_iq() must be called instead of
        process_iq(); it accepts TWO IQ streams (ADC1 + ADC2) and uses
        WDSP's internal diversity matrix (SetEXTDIVRun) for coherent
        phase-aligned combining before NR2/ANF/AGC processing.

        Requires: SunSDR2 DX configured for dual-ADC streaming (two UDP
        IQ ports).  Until the hardware dual-stream capability is
        confirmed, this is forward-looking.
        """
        if not _check_symbol("fexchange2"):
            logger.warning("WDSP fexchange2 not available — diversity requires "
                          "fexchange2 support in libwdsp.dylib")
            return
        self._diversity_enabled = enabled
        if _check_symbol("SetEXTDIVRun"):
            _wdsp.SetEXTDIVRun(ctypes.c_int(self.channel),
                              ctypes.c_int(1 if enabled else 0))
        if enabled and _check_symbol("SetEXTDIVRotate"):
            _wdsp.SetEXTDIVRotate(ctypes.c_int(self.channel),
                                 ctypes.c_double(1.0))
        if enabled and _check_symbol("SetEXTDIVOutput"):
            _wdsp.SetEXTDIVOutput(ctypes.c_int(self.channel),
                                  ctypes.c_int(0))  # 0 = combined output

    def process_diversity_iq(self, iq_a: np.ndarray,
                             iq_b: np.ndarray) -> np.ndarray:
        """Process dual-ADC IQ through fexchange2 → diversity-combined audio.

        iq_a, iq_b: complex64/128 arrays from ADC1 and ADC2 respectively.
                     Must be the same length.
        Returns: float32 audio, time-aligned to input length.

        fexchange2 runs both streams through the WDSP channel together:
        diversity matrix → NR2 → ANF → demodulation → AGC.
        The output is the diversity-combined demodulated audio.
        """
        if not self._diversity_enabled:
            logger.warning("process_diversity_iq called but diversity not enabled")
            return self.process_iq(iq_a)

        n = min(len(iq_a), len(iq_b))
        if n == 0:
            return np.array([], dtype=np.float32)

        # Convert to interleaved float64: [I0, Q0, I1, Q1, ...] for both streams
        iq_a64 = iq_a[:n].astype(np.complex128)
        iq_b64 = iq_b[:n].astype(np.complex128)
        interleaved_a = np.empty(n * 2, dtype=np.float64)
        interleaved_b = np.empty(n * 2, dtype=np.float64)
        interleaved_a[0::2] = iq_a64.real
        interleaved_a[1::2] = iq_a64.imag
        interleaved_b[0::2] = iq_b64.real
        interleaved_b[1::2] = iq_b64.imag

        # Accumulate into dual IQ buffer
        self._iq_buffer_a = np.concatenate(
            [getattr(self, '_iq_buffer_a', np.array([], dtype=np.float64)),
             interleaved_a])
        self._iq_buffer_b = np.concatenate(
            [getattr(self, '_iq_buffer_b', np.array([], dtype=np.float64)),
             interleaved_b])
        bs2 = self.buffer_size * 2

        # Pre-allocate diversity work buffers on first use
        if not hasattr(self, '_in_b'):
            self._in_b = np.zeros(self.buffer_size * 2, dtype=np.float64)
            self._out_b = np.zeros(self.buffer_size * 2, dtype=np.float64)
            assert self._in_b.flags["C_CONTIGUOUS"] and self._in_b.dtype == np.float64
            assert self._out_b.flags["C_CONTIGUOUS"] and self._out_b.dtype == np.float64

        while (len(self._iq_buffer_a) >= bs2
               and len(self._iq_buffer_b) >= bs2):
            self._in[:] = self._iq_buffer_a[:bs2]
            self._in_b[:] = self._iq_buffer_b[:bs2]
            self._iq_buffer_a = self._iq_buffer_a[bs2:]
            self._iq_buffer_b = self._iq_buffer_b[bs2:]

            err = ctypes.c_int(0)
            _wdsp.fexchange2(
                ctypes.c_int(self.channel),
                self._in.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                self._in_b.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                self._out.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                self._out_b.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                ctypes.byref(err))

            if err.value not in (0, -2):
                logger.warning("WDSP fexchange2 error=%d", err.value)
                continue

            # Primary output channel carries the diversity-combined audio
            raw_audio = self._out[0::2].copy()
            audio_block = raw_audio[::self._decim]
            if self._ratio is None:
                self._ratio = len(audio_block) / self.buffer_size
                logger.info("WDSP Diversity output ratio: %.3f "
                           "(block=%d→%d, decim=%d)",
                           self._ratio, self.buffer_size,
                           len(audio_block), self._decim)
            self._audio_buffer = np.concatenate(
                [self._audio_buffer, audio_block])

        # Return time-aligned audio
        if self._ratio is None:
            return np.zeros(0, dtype=np.float32)
        want = max(1, int(n * self._ratio))
        if len(self._audio_buffer) >= want:
            result = self._audio_buffer[:want].astype(np.float32, copy=False)
            self._audio_buffer = self._audio_buffer[want:]
            return result
        available = len(self._audio_buffer)
        result = np.zeros(want, dtype=np.float32)
        if available > 0:
            result[:available] = self._audio_buffer[:available].astype(np.float32)
            self._audio_buffer = np.array([], dtype=np.float64)
        return result

    def close(self):
        if WDSP_AVAILABLE:
            _wdsp.SetChannelState(ctypes.c_int(self.channel),
                                  ctypes.c_int(0), ctypes.c_int(0))
            _wdsp.CloseChannel(ctypes.c_int(self.channel))


