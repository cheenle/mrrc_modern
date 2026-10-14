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


class SlotDumper:
    """Write one ``.raw`` file per slot into a directory, failures and all."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)
        self._warned = False

    @classmethod
    def from_environ(cls, environ) -> SlotDumper | None:
        """Build a dumper when the operator asked for one; ``None`` otherwise."""

        raw = (environ.get(ENV_VAR) or "").strip()
        return cls(Path(raw)) if raw else None

    def write(self, slot_id: int, samples: bytes) -> None:
        path = self._directory / f"slot_{int(slot_id):012d}.raw"
        if path.exists():  # first write wins — see the module docstring
            return
        try:
            path.write_bytes(samples)
        except OSError as error:
            if not self._warned:
                self._warned = True
                log.warning(
                    "slot dump to %s failed (%s); further failures are silent",
                    self._directory,
                    error,
                )
