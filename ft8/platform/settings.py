"""FT8 settings — independent of the platform's own config (design §4.9).

Stores the operator's FT8 identity and radio preferences in a single JSON
file under the platform's writable runtime dir.  No secrets here.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("mrrc.ft8.settings")

RIG_MODES = ("DATA-U", "USB")
DEFAULT_TX_AUDIO_HZ = 1500


@dataclass(frozen=True)
class Ft8Settings:
    my_call: str = ""
    my_grid: str = ""
    rig_mode: str = "DATA-U"
    tx_audio_frequency: int = DEFAULT_TX_AUDIO_HZ
    engine_always_on: bool = True
    """Run the FT8 engine (RX monitor + decoder + waterfall) at startup.

    RX is read-only — TX still requires an explicit arm — so a dedicated
    station box can keep decoding across restarts.  Turn it off to save the
    ~5% CPU when the station is used for SSB only.
    """
    settings_revision: int = 0

    def validate(self) -> None:
        if self.rig_mode not in RIG_MODES:
            raise ValueError(f"rig_mode must be one of {RIG_MODES}")
        if not 200 <= int(self.tx_audio_frequency) <= 2800:
            raise ValueError("tx_audio_frequency must be 200..2800 Hz")
        if not isinstance(self.engine_always_on, bool):
            raise ValueError("engine_always_on must be a boolean")


class Ft8SettingsStore:
    """One JSON file; atomic writes; a corrupt file degrades to defaults."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Ft8Settings:
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return Ft8Settings()
        except (OSError, ValueError) as exc:
            log.warning("ft8 settings unreadable (%s); using defaults", exc)
            return Ft8Settings()
        if not isinstance(raw, dict):
            log.warning("ft8 settings are not an object; using defaults")
            return Ft8Settings()
        known = {f.name for f in dataclasses.fields(Ft8Settings)}
        return Ft8Settings(**{k: v for k, v in raw.items() if k in known})

    def save(self, settings: Ft8Settings) -> None:
        settings.validate()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = dataclasses.asdict(settings)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".ft8-settings-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            os.replace(tmp, self._path)          # atomic
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
