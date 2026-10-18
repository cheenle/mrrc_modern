"""Off-by-default dump of the exact slot bytes — the recall rig's raw material.

``MRRC_FT8_SLOT_DUMP_DIR`` turns it on; nothing in the shipped product sets it.
The recall measurement (design §12) needs real 15 s slots, and the only place
that holds one is the slot source itself, so this is where the bytes are copied
out — before anything else touches them.

Two rules, both there because the alternative loses the measurement:

* **The first write for a slot wins.**  A slot can be polled more than once
  while the ring fills, and early dispatch means the copy the decoder consumed
  is the *first* one (the tail is silence by design — the FT8 waveform ends
  12.64 s into the 15 s slot).  Letting a later, longer read overwrite it would
  measure an input production never used.
* **Every failure is swallowed.**  The dump is a diagnostic; it must never be
  able to stop a decode, so a full disk costs one warning and nothing else.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("mrrc.ft8.slot_dump")

ENV_VAR = "MRRC_FT8_SLOT_DUMP_DIR"
KEEP_ENV_VAR = "MRRC_FT8_SLOT_DUMP_KEEP"

# 15 s per slot at 360 KB each is 2 GB a day: the research dump filled 1.3 GB of
# the box's eMMC in seven hours, and an unbounded dump on a *production* device
# is a defect no matter how useful the data is.  Rotation keeps the newest files
# (a corpus only needs a few thousand) and costs one directory scan per prune.
DEFAULT_KEEP = 2000


class SlotDumper:
    """Write one ``.raw`` file per slot into a directory, failures and all."""

    def __init__(self, directory: Path, keep: int = DEFAULT_KEEP) -> None:
        self._directory = Path(directory)
        self._keep = max(1, int(keep))
        self._warned = False
        self._written = 0

    @classmethod
    def from_environ(cls, environ) -> SlotDumper | None:
        """Build a dumper when the operator asked for one; ``None`` otherwise."""

        raw = (environ.get(ENV_VAR) or "").strip()
        if not raw:
            return None
        try:
            keep = int(environ.get(KEEP_ENV_VAR) or DEFAULT_KEEP)
        except (TypeError, ValueError):
            keep = DEFAULT_KEEP
        return cls(Path(raw), keep)

    def _prune(self) -> None:
        """Drop the oldest files once the directory has outgrown ``keep``."""

        try:
            files = sorted(self._directory.glob("slot_*.raw"))
            for stale in files[: max(0, len(files) - self._keep)]:
                stale.unlink(missing_ok=True)
        except OSError:
            pass

    def write(self, slot_id: int, samples: bytes) -> None:
        path = self._directory / f"slot_{int(slot_id):012d}.raw"
        if path.exists():  # first write wins — see the module docstring
            return
        try:
            path.write_bytes(samples)
            self._written += 1
            if self._written % 200 == 0:
                self._prune()
        except OSError as error:
            if not self._warned:
                self._warned = True
                log.warning(
                    "slot dump to %s failed (%s); further failures are silent",
                    self._directory,
                    error,
                )
