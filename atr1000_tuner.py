"""
ATR-1000 Tuner LC-Learning Storage
==================================
Persistent LC-learning store backed by a JSON file.  Records the tuner
relay parameters (network type, inductance, capacitance) that produced
an acceptable SWR at each frequency, so later tunes can start from the
best known setting instead of a full search.

Record schema (JSON, version "2.0"):
{
    "freq": 7053000,          # frequency, Hz
    "sw": 0,                  # network type: 0=LC, 1=CL
    "ind": 45,                # inductance index (0-127)
    "cap": 32,                # capacitance index (0-127)
    "swr_avg": 1.15,          # running average SWR
    "swr_min": 1.10,          # minimum observed SWR
    "swr_max": 1.20,          # maximum observed SWR
    "sample_count": 5,        # number of samples
    "last_update": timestamp  # last update time
}
"""

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('ATR1000-Tuner')

# Default store file: atr1000_tuner.json at the repository root
# (same level as mem_channels.json).  FT710_ATR1000_STORE overrides the
# path — the frozen Windows launcher points it at the user data dir
# (%LOCALAPPDATA%\MRRC-FT710) so learned data survives reinstalls and
# stays writable; TunerStorage(storage_file=...) overrides both.
STORAGE_FILE = os.environ.get(
    'FT710_ATR1000_STORE',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'atr1000_tuner.json'),
)

# SWR learning gate: only record when SWR_LEARN_MIN <= swr <= SWR_LEARN_MAX
SWR_LEARN_MIN = 1.0
SWR_LEARN_MAX = 1.8
# Below 1.00 is physically impossible and needs verification; 1.00 itself
# is a legitimate perfect match.
SWR_OPTIMAL_MIN = 1.00

# Frequency match tolerance for find_best(): ±5kHz
FREQ_TOLERANCE = 5000


class TunerStorage:
    """Tuner parameter storage with dynamic learning."""

    def __init__(self, storage_file: str = None):
        self.storage_file = storage_file or STORAGE_FILE
        self.data: Dict[str, dict] = {}  # key: freq_key, value: record
        self.lock = threading.Lock()
        self._load()

    def _freq_key(self, freq: int) -> str:
        """Frequency key with 1kHz resolution."""
        return str(freq // 1000)

    def _load(self):
        """Load stored records from the JSON file."""
        try:
            if os.path.exists(self.storage_file):
                with open(self.storage_file, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                    # Convert the records list to a dict keyed by freq_key
                    for record in raw.get('records', []):
                        freq = record.get('freq', 0)
                        if freq > 0:
                            key = self._freq_key(freq)
                            self.data[key] = record
                logger.info(f"📂 Loaded {len(self.data)} tuner records")
        except Exception as e:
            logger.error(f"Failed to load tuner data: {e}")
            self.data = {}

    def _save(self):
        """Persist records (atomic write via temp file + rename)."""
        tmp_path = None
        try:
            raw = {
                'version': '2.0',
                'updated': datetime.now().isoformat(),
                'records': list(self.data.values())
            }
            tmp_dir = os.path.dirname(os.path.abspath(self.storage_file))
            with tempfile.NamedTemporaryFile('w', dir=tmp_dir, delete=False,
                                             suffix='.tmp', encoding='utf-8') as tmp:
                tmp_path = tmp.name
                json.dump(raw, tmp, indent=2, ensure_ascii=False)
                tmp.flush()
                os.fsync(tmp.fileno())
            try:
                os.rename(tmp_path, self.storage_file)
            except OSError:
                # Windows does not allow os.rename over an existing file;
                # os.replace is atomic on both POSIX and Windows (Python 3.3+).
                os.replace(tmp_path, self.storage_file)
        except Exception as e:
            logger.error(f"Failed to save tuner data: {e}")
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def learn(self, freq: int, sw: int, ind: int, cap: int, swr: float, force_update: bool = False) -> bool:
        """
        Learn tuner parameters — records only while SWR is within
        [SWR_LEARN_MIN, SWR_LEARN_MAX].

        Args:
            freq: frequency, Hz
            sw: network type (0=LC, 1=CL)
            ind: inductance index (0-127)
            cap: capacitance index (0-127)
            swr: standing wave ratio
            force_update: force-write the parameters, for Tune-linkage cases
                          where the improvement over the initial SWR was
                          already confirmed externally

        Returns:
            True if the sample was recorded
        """
        # SWR range gate. A Tune linkage that already verified "lower than
        # the initial SWR" may force the write past this gate.
        if not force_update and (swr < SWR_LEARN_MIN or swr > SWR_LEARN_MAX):
            return False

        # Parameter validity — ind and cap must carry real values
        if ind == 0 and cap == 0:
            logger.debug("Ignoring invalid params: ind=0, cap=0")
            return False

        with self.lock:
            key = self._freq_key(freq)

            if key in self.data:
                # Update existing record — running average
                record = self.data[key]
                old_avg = record['swr_avg']
                old_count = record['sample_count']
                new_count = old_count + 1

                # Cumulative (running) average
                record['swr_avg'] = (old_avg * old_count + swr) / new_count
                record['sample_count'] = new_count
                record['swr_min'] = min(record['swr_min'], swr)
                record['swr_max'] = max(record['swr_max'], swr)
                record['last_update'] = time.time()

                # Overwrite the relay parameters only if the new SWR is better
                # than the historical average, or a Tune linkage confirmed the
                # improvement (force_update).
                if force_update or (swr < record['swr_avg'] and swr >= SWR_OPTIMAL_MIN):
                    record['sw'] = sw
                    record['ind'] = ind
                    record['cap'] = cap

            else:
                # New record — SWR < 1.00 is physically impossible, flag for verification
                self.data[key] = {
                    'freq': freq,
                    'sw': sw,
                    'ind': ind,
                    'cap': cap,
                    'swr_avg': swr,
                    'swr_min': swr,
                    'swr_max': swr,
                    'sample_count': 1,
                    'last_update': time.time(),
                    'needs_verify': swr < SWR_OPTIMAL_MIN  # only SWR < 1.00 needs verification
                }

            self._save()
            logger.info(f"📝 Learn: {freq/1000:.1f}kHz, SWR={swr:.2f}, SW={'CL' if sw else 'LC'}, L={ind}, C={cap}")
            return True

    def find_best(self, freq: int) -> Optional[dict]:
        """
        Find the best tuner record for a frequency.

        Args:
            freq: target frequency, Hz

        Returns:
            A copy of the best matching record, or None.  Exact 1kHz key
            match first, otherwise the nearest record within FREQ_TOLERANCE.
        """
        with self.lock:
            # Exact match
            key = self._freq_key(freq)
            if key in self.data:
                return self.data[key].copy()

            # Range search (±FREQ_TOLERANCE)
            freq_khz = freq // 1000
            best_record = None
            best_dist = float('inf')

            for k, record in self.data.items():
                record_freq_khz = int(k)
                dist = abs(freq_khz - record_freq_khz)

                if dist <= (FREQ_TOLERANCE // 1000) and dist < best_dist:
                    best_dist = dist
                    best_record = record

            if best_record:
                return best_record.copy()

            return None

    def get_tune_params(self, freq: int) -> Optional[Tuple[int, int, int]]:
        """
        Get the tuner parameters for a frequency.

        Args:
            freq: target frequency, Hz

        Returns:
            (sw, ind, cap) or None if no record or the parameters are invalid
        """
        record = self.find_best(freq)
        if record:
            # Parameter validity check
            ind = record.get('ind', 0)
            cap = record.get('cap', 0)
            if ind == 0 and cap == 0:
                return None  # invalid parameters
            return (record['sw'], ind, cap)
        return None

    def get_all(self) -> List[dict]:
        """Return all records sorted by frequency."""
        with self.lock:
            records = list(self.data.values())
            records.sort(key=lambda x: x.get('freq', 0))
            return records

    def delete(self, freq: int) -> bool:
        """Delete the record for a frequency. Returns True if one existed."""
        with self.lock:
            key = self._freq_key(freq)
            if key in self.data:
                del self.data[key]
                self._save()
                return True
            return False

    def clear(self):
        """Delete all records."""
        with self.lock:
            self.data = {}
            self._save()

    def get_stats(self) -> dict:
        """Return aggregate statistics over all records."""
        with self.lock:
            if not self.data:
                return {'count': 0}

            records = list(self.data.values())
            swr_values = [r['swr_avg'] for r in records]

            return {
                'count': len(records),
                'swr_avg': sum(swr_values) / len(swr_values),
                'swr_min': min(swr_values),
                'swr_max': max(swr_values)
            }


# Global singleton
_storage = None
_storage_lock = threading.Lock()

def get_storage() -> TunerStorage:
    """Return the process-wide TunerStorage singleton."""
    global _storage
    with _storage_lock:
        if _storage is None:
            _storage = TunerStorage()
        return _storage
