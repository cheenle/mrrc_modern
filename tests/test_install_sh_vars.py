"""Every variable install.sh uses has a value by the time it is used.

The script runs under `set -euo pipefail`, so a variable that only gets assigned
inside one branch is *unbound* everywhere else — and `if ! $INSTALL_DEV; then`
is an unbound-variable error rather than a falsy test. Two of those shipped:

* ``INSTALL_DEV``, assigned only by ``--dev``, killed ``./install.sh`` at STEP 4b
  for every caller that did not pass it;
* ``USER``, a login-shell variable that a chroot simply does not have, killed it
  at STEP 9 while writing ``User=$USER`` into the systemd unit.

Neither is visible to review: the flag list, the defaults list and the use site
sit hundreds of lines apart and each looks right alone. So this checks the
property instead of the two instances.

Deliberately not covered: ``${VAR:-default}`` is safe (the default is only
expanded when needed) and ``\\$VAR`` is a literal, so both are stripped before
the scan. That makes ``"${MRRC_SERIAL_PORT:-$FT710_SERIAL_PORT}"`` pass, which
is correct — it cannot expand the unset name.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"

#: `--dev) INSTALL_DEV=true ;;` — the assignment target of an argument branch.
#: Branches without an assignment (--help) are not matched and need no default.
FLAG_ASSIGNMENT = re.compile(r"^\s*--[a-z|-]+\)\s*([A-Z_][A-Z0-9_]*)=", re.MULTILINE)

#: `${VAR:-…}` / `${VAR:?…}` / `${VAR:=…}`, one nesting level.
GUARDED = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:[-?=][^{}]*\}")

#: An escaped literal, e.g. the `\$USER` in a hint message.
ESCAPED = re.compile(r"\\\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")

#: `$VAR` or `${VAR}`, without the brace-parameter operators.
BARE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")

#: Assignments in any of the forms this script uses.
ASSIGNMENT = re.compile(
    r"(?:^|[\s;(])(?:local\s+|export\s+|declare(?:\s+-\w+)?\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?=",
    re.MULTILINE,
)
LOOP_VAR = re.compile(r"for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s")

#: `read -r -p "…" reply` — the target is the last word on the line, after any
#: options and their arguments.
READ_VAR = re.compile(r"\bread\b[^\n]*?\s([A-Za-z_][A-Za-z0-9_]*)\s*(?:$|\|\||&&|;)", re.MULTILINE)

#: `: "${VAR:=fallback}"` assigns VAR as a side effect of the expansion.
DEFAULT_ASSIGN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):=")

#: Referenced on a line that tests the same name first — `[ -n "${X:-}" ] &&
#: echo "$X"`. Safe, but only because of a guard the scanner cannot follow, so
#: it is named here rather than inferred.
GUARDED_BY_SAME_LINE_TEST = frozenset({"MRRC_SCOPE_BAUD"})

#: Set by bash itself, or by every environment this script can run in — a
#: container has PATH and PWD even though it has no login shell.
SHELL_PROVIDED = frozenset(
    {
        "BASH", "BASHOPTS", "BASH_SOURCE", "BASH_VERSION", "EUID", "FUNCNAME",
        "HOSTNAME", "IFS", "LINENO", "MACHTYPE", "OLDPWD", "OSTYPE", "PATH",
        "PPID", "PWD", "RANDOM", "SECONDS", "SHELLOPTS", "SHLVL", "UID",
    }
)


def _references(script: str) -> set[str]:
    """Names expanded unconditionally, with safe forms removed first."""
    script = GUARDED.sub("", ESCAPED.sub("", script))
    return {braced or plain for braced, plain in BARE.findall(script)}


def _assigned(script: str) -> set[str]:
    names = set(ASSIGNMENT.findall(script))
    names |= set(LOOP_VAR.findall(script))
    names |= set(READ_VAR.findall(script))
    names |= set(DEFAULT_ASSIGN.findall(script))
    return names


class InstallShUnboundVariableTests(unittest.TestCase):
    def setUp(self):
        self.script = INSTALL_SH.read_text(encoding="utf-8")

    def test_the_script_still_runs_under_nounset(self):
        """The premise of this file. Without -u these bugs are silent no-ops."""
        self.assertIn("set -euo pipefail", self.script)

    def test_the_scanner_finds_something(self):
        """A regex that matches nothing would make the next test vacuous."""
        self.assertGreater(len(_references(self.script)), 20)

    def test_every_referenced_variable_is_assigned_or_shell_provided(self):
        unassigned = (
            _references(self.script)
            - _assigned(self.script)
            - SHELL_PROVIDED
            - GUARDED_BY_SAME_LINE_TEST
        )
        self.assertEqual(
            unassigned,
            set(),
            "these are expanded unconditionally but nothing gives them a value; "
            "under set -u each is a hard failure at whatever line uses it first. "
            "Either assign a default (as INSTALL_DEV and USER now do) or guard "
            "the expansion.",
        )


class InstallShFlagDefaultTests(unittest.TestCase):
    def setUp(self):
        self.script = INSTALL_SH.read_text(encoding="utf-8")

    def _globals_block(self) -> str:
        """The declarations between the Globals and Help section banners."""
        start = self.script.index("# ── Globals")
        end = self.script.index("# ── Help")
        return self.script[start:end]

    def test_every_flag_variable_is_initialised(self):
        flags = set(FLAG_ASSIGNMENT.findall(self.script))
        self.assertTrue(flags, "the argument parser was not found — regex drift?")
        globals_block = self._globals_block()
        for name in sorted(flags):
            with self.subTest(flag=name):
                # MULTILINE matters: assertRegex searches, so a bare ^…$ would
                # anchor to the ends of the whole block and match nothing.
                self.assertRegex(
                    globals_block,
                    re.compile(rf"^{name}=", re.MULTILINE),
                    f"{name} is only assigned by its --flag branch, so every "
                    "other invocation dies on it under set -u",
                )

    def test_install_dev_defaults_to_off(self):
        """The specific regression: --dev is opt-in, so the default is false."""
        self.assertRegex(
            self._globals_block(), re.compile(r"^INSTALL_DEV=false$", re.MULTILINE)
        )


if __name__ == "__main__":
    unittest.main()
