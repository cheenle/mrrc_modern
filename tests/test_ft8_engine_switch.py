"""The FT8 engine switch in the menu (AD-028 follow-up).

The engine runs in the background from boot, which on a small box costs a real
core of DSP.  The menu therefore carries an on/off switch.  What must hold:

* the switch is bound to the **engine** (`/api/ft8/engine`), not to the on-air
  mode — the station can be on SSB while the engine decodes;
* its displayed state comes from the server (`/api/ft8/state` →
  ``engine.enabled``), so a reload shows the truth instead of a guess;
* the existing one-way "go to FT8" menu action stays as it is (it moves the
  radio and navigates), because the two answer different questions.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
UI = (REPO / "static" / "ft710_ui.js").read_text(encoding="utf-8")


class MenuMarkupTests(unittest.TestCase):
    def test_the_switch_lives_in_the_settings_block(self):
        start = HTML.index('<h3 class="menu-section-title">Settings</h3>')
        end = HTML.index("menu-version")          # the footer of the same panel
        self.assertIn('id="menu-ft8-engine"', HTML[start:end])

    def test_the_switch_starts_in_the_on_state(self):
        button = re.search(r'<button\s+id="menu-ft8-engine".*?</button>', HTML, re.S)
        self.assertIsNotNone(button)
        self.assertIn('data-enabled="true"', button.group(0))
        self.assertIn("ON", button.group(0))


class SwitchBehaviourTests(unittest.TestCase):
    def test_it_posts_to_the_engine_endpoint(self):
        self.assertIn("/api/ft8/station", UI)
        self.assertIn("body: JSON.stringify({engine_always_on: next})", UI)
        self.assertIn("method: 'PUT'", UI)

    def test_it_reads_its_state_from_the_server(self):
        self.assertIn("/api/ft8/station", UI)
        self.assertIn("engine_always_on", UI)

    def test_it_is_bound_once_at_startup(self):
        self.assertIn("initFt8EngineSwitch();", UI)
        self.assertIn("btn.dataset.bound === '1'", UI)

    def test_it_does_not_hijack_the_one_way_ft8_mode_action(self):
        # the navigation action keeps its own endpoint
        self.assertIn("case 'ft8-mode':", UI)
        self.assertIn("JSON.stringify({mode: 'ft8'})", UI)


if __name__ == "__main__":
    unittest.main()
