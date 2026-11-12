"""
Xiegu backend package
=====================
The Xiegu G7250 speaks Icom CI-V over USB (*not* Yaesu ASCII CAT), so this
package reuses the shared CI-V core in ``backends/ic7300`` and adds only what
is Xiegu-specific:

- ``xiegu_profiles.py`` — model facts (CI-V address candidates, 115200 baud,
  scope geometry, mode/band/meter tables) with per-value provenance.
- ``backend.py`` — ``G7250Backend``: address discovery, the two capability
  corrections the vendor table proves (auto notch, Vd/Id), TX gate.

The radio's **network** port is Icom's RS-BA1 protocol (50001 control, 50002
CI-V tunnel, 50003 audio) — not the raw IQ stream an earlier revision of this
package implemented.  Spectrum therefore comes from CI-V ``0x27`` on the CAT
port, exactly like the Icom models.  Evidence and the vendor rig file live in
``docs/g7250-wfview-analysis.md`` / ``docs/wfview-g7250.rig``.

Register a new Xiegu model by adding a profile entry and a backend subclass
overriding ``_profile``/``_display_name`` (same shape as the Icom models).
"""
