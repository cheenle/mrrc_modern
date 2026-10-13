from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[3]
FIXTURE = Path(__file__).parent / "fixtures" / "ft8lib_reference.json"


def test_fast_path_matches_reference_decode_count(
    call_fast, raw_library, busy_slot
) -> None:
    status, _results, count, _overflow = call_fast(raw_library, busy_slot)
    assert status == 0
    reference = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert count >= reference["decode_count"], (
        f"fast path decoded {count}, reference decoded {reference['decode_count']}"
    )
