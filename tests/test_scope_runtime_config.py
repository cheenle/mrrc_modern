import os
import unittest
from unittest.mock import patch

from backends.ft710.scope_libraries import get_ft4222_clock_divider


class ScopeRuntimeConfigTests(unittest.TestCase):
    def test_default_spi_clock_divider_is_clk_div_64(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_ft4222_clock_divider(), 6)  # CLK_DIV_64 (wfview default)

    def test_spi_clock_divider_can_be_overridden_for_hardware_trials(self):
        with patch.dict(os.environ, {"FT710_FT4222_CLK_DIV": "1"}, clear=True):
            self.assertEqual(get_ft4222_clock_divider(), 1)


if __name__ == "__main__":
    unittest.main()
