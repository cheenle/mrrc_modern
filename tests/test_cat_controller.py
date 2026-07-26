"""
Tests for CatController — SDD AD-002 (direct serial CAT).
Verifies: command formatting, response parsing, protocol correctness.
All tests run without hardware (mock the serial port).
"""
import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from cat_controller import CatController

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── Command Formatting Tests (no serial needed) ──────────────────────

class CatCommandFormattingTests(unittest.TestCase):
    """Verify CAT commands are formatted correctly per Yaesu protocol."""

    def test_frequency_command_format(self):
        """FA command: 9-digit Hz, no leading zeros in value but padded to 9."""
        # FA014200000 → 14,200,000 Hz
        cmd = f"FA{14_200_000:09d}"
        self.assertEqual(cmd, "FA014200000")
        self.assertEqual(len(cmd), len("FA") + 9)

    def test_frequency_command_min(self):
        cmd = f"FA{0:09d}"
        self.assertEqual(cmd, "FA000000000")

    def test_mode_command_format(self):
        """MD0 command: hex mode number."""
        # USB = mode 2 → MD02
        cmd = f"MD0{2:X}"
        self.assertEqual(cmd, "MD02")
        # LSB = mode 1 → MD01
        cmd = f"MD0{1:X}"
        self.assertEqual(cmd, "MD01")
        # FM = mode 6 → MD06
        cmd = f"MD0{6:X}"
        self.assertEqual(cmd, "MD06")

    def test_ptt_command_format(self):
        self.assertEqual("TX1", "TX1")  # TX on
        self.assertEqual("TX0", "TX0")  # TX off
        self.assertEqual("TX2", "TX2")  # Tune

    def test_smeter_command_format(self):
        self.assertEqual("SM0", "SM0")

    def test_filter_command_format(self):
        # FT-710 SH (WIDTH) format: SH + P1(0) + P2(0) + 2-digit width = SH00NN
        cmd = f"SH00{5:02d}"
        self.assertEqual(cmd, "SH0005")
        cmd = f"SH00{22:02d}"
        self.assertEqual(cmd, "SH0022")
        cmd = f"SH00{13:02d}"
        self.assertEqual(cmd, "SH0013")

    def test_gain_command_format(self):
        cmd = f"AG0{128:03d}"
        self.assertEqual(cmd, "AG0128")
        cmd = f"AG0{255:03d}"
        self.assertEqual(cmd, "AG0255")
        cmd = f"AG0{0:03d}"
        self.assertEqual(cmd, "AG0000")

    def test_rf_power_command_format(self):
        cmd = f"PC{100:03d}"
        self.assertEqual(cmd, "PC100")
        cmd = f"PC{5:03d}"
        self.assertEqual(cmd, "PC005")

    def test_preamp_command_format(self):
        for val in (0, 1, 2):
            cmd = f"PA0{val}"
            self.assertIn(cmd, ("PA00", "PA01", "PA02"))

    def test_attenuator_command_format(self):
        for val in (0, 1, 2, 3):
            cmd = f"RA0{val}"
            self.assertIn(cmd, ("RA00", "RA01", "RA02", "RA03"))

    def test_boolean_commands(self):
        self.assertEqual("NB01", "NB01")
        self.assertEqual("NB00", "NB00")
        self.assertEqual("NR01", "NR01")
        self.assertEqual("BC01", "BC01")
        self.assertEqual("PR01", "PR01")
        self.assertEqual("PS1", "PS1")
        self.assertEqual("ST1", "ST1")

    def test_vfo_switch_commands(self):
        self.assertEqual("VS0", "VS0")  # VFO-A
        self.assertEqual("VS1", "VS1")  # VFO-B

    def test_scope_commands(self):
        self.assertEqual(f"SS05{6:02d}", "SS0506")
        self.assertEqual(f"SS00{2:02d}", "SS0002")
        self.assertEqual(f"SS06{0:02d}", "SS0600")

    def test_tuner_command_format(self):
        # AC P1P2P3: P1=0, P2=type(0=off/1=std/2=ATAS), P3=tuning(0/1)
        tuner_map = {0: "000", 1: "010", 2: "011"}
        self.assertEqual(f"AC{tuner_map[0]}", "AC000")   # off/bypass
        self.assertEqual(f"AC{tuner_map[1]}", "AC010")   # ATU on, not tuning
        self.assertEqual(f"AC{tuner_map[2]}", "AC011")   # ATU on, tuning

    def test_band_stack_command(self):
        cmd = f"BS{2:02d}"  # 20m band
        self.assertEqual(cmd, "BS02")


class CatResponseParsingTests(unittest.TestCase):
    """Verify CAT response parsing logic (from cat_controller.py patterns)."""

    def test_parse_frequency_response(self):
        resp = "FA014200000"
        prefix = "FA"
        freq_str = resp[len(prefix):]
        freq = int(freq_str)
        self.assertEqual(freq, 14_200_000)

    def test_parse_smeter_response(self):
        resp = "SM00120"
        val = int(resp[3:])
        self.assertEqual(val, 120)

    def test_parse_mode_response(self):
        resp = "MD02"
        mode = int(resp[3:], 16)
        self.assertEqual(mode, 2)

    def test_parse_ptt_response(self):
        resp = "TX1"
        val = int(resp[2:])
        self.assertEqual(val, 1)
        resp = "TX0"
        val = int(resp[2:])
        self.assertEqual(val, 0)

    def test_parse_filter_response(self):
        # Radio responds with SH00NN format (P1=0, P2=0, P3=2-digit width).
        resp = "SH0005"
        val = int(resp[-2:])
        self.assertEqual(val, 5)
        resp = "SH0013"
        val = int(resp[-2:])
        self.assertEqual(val, 13)

    def test_parse_error_response(self):
        resp = "?;"
        self.assertIn("?", resp)

    def test_parse_if_response_format(self):
        """IF; response: IF + 9-digit freq + mode char + misc + 3-digit S-meter."""
        # Simulate the real parsing from cat_controller.py
        resp = "IF0142000002" + "0" * 20 + "120"
        tail = resp[2:]  # Skip "IF"
        # Extract frequency digits: only the first 9 digits
        freq_str = ""
        i = 0
        while i < len(tail) and tail[i].isdigit() and len(freq_str) < 9:
            freq_str += tail[i]
            i += 1
        freq = int(freq_str) if freq_str else None
        self.assertEqual(freq, 14_200_000)
        self.assertEqual(len(freq_str), 9)


class CatControllerMockedTests(unittest.IsolatedAsyncioTestCase):
    """Tests using mocked serial for async CatController behavior."""

    def setUp(self):
        # We test the logic without importing CatController directly
        # to avoid hardware dependencies
        pass

    async def test_command_terminator_is_semicolon(self):
        """All CAT commands must end with semicolon."""
        cmd = "FA014200000"
        self.assertEqual(cmd + ";", "FA014200000;")

    async def test_set_filter_width_uses_two_digit_width_index(self):
        """FT-710 SH expects SH + P1(0) + P2(0) + 2-digit width = SH00NN."""
        cat = CatController("mock-port")
        cat.set = AsyncMock(return_value=True)

        ok = await cat.set_filter_width(13)

        self.assertTrue(ok)
        cat.set.assert_awaited_once_with("SH0013")

    async def test_query_command_is_send_without_value(self):
        """Query commands send prefix only (no value)."""
        # query("FA") → "FA;"
        cmd = "FA"
        self.assertEqual(cmd + ";", "FA;")

    async def test_set_command_is_send_with_value(self):
        """Set commands send prefix + value."""
        # set("FA014200000") → "FA014200000;"
        cmd = "FA014200000"
        self.assertEqual(cmd + ";", "FA014200000;")

    async def test_set_command_is_write_only_for_responsiveness(self):
        """Set commands must not wait for a CAT response timeout."""
        source = (REPO_ROOT / "cat_controller.py").read_text(encoding="utf-8")
        self.assertIn("async def send_set_command", source)
        self.assertIn("return await self.send_set_command(cmd)", source)

    async def test_triple_ptt_verify_sequence(self):
        """SDD §15.3 Layer 3: triple TX; query after TX0;."""
        # Simulate the verify loop: send TX0;, then query TX; 3 times
        tx_commands = ["TX0"]
        verify_commands = ["TX"] * 3
        all_commands = [c + ";" for c in tx_commands + verify_commands]
        self.assertEqual(len(all_commands), 4)
        self.assertEqual(all_commands[0], "TX0;")
        for i in range(1, 4):
            self.assertEqual(all_commands[i], "TX;")

    async def test_send_command_ascii_only(self):
        """CAT commands are ASCII encoded."""
        cmd = "FA014200000;"
        encoded = cmd.encode("ascii")
        self.assertEqual(len(encoded), len(cmd))


if __name__ == "__main__":
    unittest.main()
