from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).parents[3]
VENDOR = ROOT / "ft8" / "dsp" / "vendor-ft8lib"
MANIFEST = ROOT / "ft8" / "dsp" / "vendor-ft8lib.sha256"

# 快路径只需要这些翻译单元；不 vendor audio.c / wave.c（设备/文件 IO）。
# 注：上游 common/ 没有 common.c —— common.h 是纯头文件（只定义 M_PI 兜底），
# 所以这里只要求 common/monitor.c。
REQUIRED_SOURCES = {
    "ft8/constants.c", "ft8/crc.c", "ft8/decode.c", "ft8/encode.c",
    "ft8/ldpc.c", "ft8/message.c", "ft8/text.c",
    "common/monitor.c",
    "fft/kiss_fft.c", "fft/kiss_fftr.c",
}


def test_required_sources_are_vendored() -> None:
    present = {
        str(p.relative_to(VENDOR))
        for p in VENDOR.rglob("*.c")
    }
    assert REQUIRED_SOURCES <= present, REQUIRED_SOURCES - present


def test_manifest_matches_disk() -> None:
    recorded = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        recorded[rel] = digest
    for rel, digest in recorded.items():
        actual = hashlib.sha256((VENDOR / rel).read_bytes()).hexdigest()
        assert actual == digest, rel


def test_readme_records_provenance() -> None:
    text = (VENDOR / "README.md").read_text(encoding="utf-8")
    assert "github.com/kgoba/ft8_lib" in text
    assert "commit" in text.lower()
