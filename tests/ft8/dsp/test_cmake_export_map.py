from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[3]
PRODUCTION_EXPORTS = {
    "wsjt_get_abi_info;",
    "wsjt_ft8_encode;",
    "wsjt_ft8_decode_standard;",
    "wsjt_ft8_decode_improved;",
    "wsjt_ft8_decode_fast;",
}


def _global_exports(export_map: Path) -> set[str]:
    text = export_map.read_text(encoding="utf-8")
    assert "${" not in text
    global_block = text.split("global:", 1)[1].split("local:", 1)[0]
    return {
        line.strip()
        for line in global_block.splitlines()
        if line.strip()
    }


def test_elf_export_map_expands_optional_test_hook(tmp_path: Path) -> None:
    subprocess.run(
        [
            "cmake",
            f"-DROOT={ROOT}",
            f"-DOUTPUT_DIR={tmp_path}",
            "-P",
            str(ROOT / "tests" / "ft8" / "dsp" / "render_elf_export_maps.cmake"),
        ],
        cwd=ROOT,
        check=True,
    )

    assert _global_exports(tmp_path / "production.map") == PRODUCTION_EXPORTS
    assert _global_exports(tmp_path / "test-hooks.map") == (
        PRODUCTION_EXPORTS | {"wsjt_test_ft8_a8d;"}
    )
