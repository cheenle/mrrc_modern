"""
Xiegu backend package
=====================
The Xiegu G7250 speaks Icom CI-V over USB (*not* Yaesu ASCII CAT), so this
package reuses the shared CI-V core in ``backends/ic7300`` and adds only what
is Xiegu-specific:

- ``xiegu_profiles.py`` — model facts (CI-V address candidates, 115200 baud,
  LAN IQ constants, mode/band/meter tables) with per-value provenance.
- ``lan_iq.py`` — the UDP IQ scope producer that replaces the CI-V ``0x27``
  waveform this radio does not have.
- ``backend.py`` — ``G7250Backend``: address discovery, no CI-V scope,
  CAT-plus-LAN tuning, TX gate.

Register a new Xiegu model by adding a profile entry and a backend subclass
overriding ``_profile``/``_display_name`` (same shape as the Icom models).
"""
