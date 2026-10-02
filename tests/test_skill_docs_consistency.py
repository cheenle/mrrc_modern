"""Skill-document consistency guards (`.agents/skills/**/SKILL.md`).

These are reference documents that agents load mid-task, so a wrong pointer costs
a debugging cycle at the worst possible moment. Two real defects motivated the
guards, both found while updating the skills for the v1.24.6 release:

1. **A renumber broke every cross-reference.** Inserting a new gotcha in the
   middle of `dual-platform-release` shifted the following items, so
   "`macos-installer` gotcha 10" (quoted by two plan documents and by another
   skill) pointed at the wrong entry. The fix was to append new items at the end
   instead of inserting them — and to check the references mechanically.

2. **A single leading space hid four gotchas.** In `windows-installer`, items
   10-13 were written as ``" 1. **…"`` … ``" 4. **…"``. Markdown reads an indented
   ordered list as a *new* list starting at 1, so the rendered document showed
   1-9, a nested 1-4, then 14-20: the four items in between were unreachable by
   number, and three of them were referenced from the Common Mistakes table and
   from `macos-installer`.

Also enforced: valid frontmatter (the loader keys on `name`/`description`),
balanced code fences, and self-references ("陷阱 N" / "(see gotcha N)") that
resolve inside the same file.

Scope: authoritative docs only — `.agents/skills/**`, `docs/**`, `SDD/**` and
`CHANGELOG.md`. `.superpowers/**` is deliberately excluded: those are dated
scratch progress logs, and rewriting a historical note to chase a renumbered
skill would falsify the record. The global copy at `~/.pi/agent/skills/` is
outside the repository and machine-specific, so it is not covered here; note that
the two `windows-installer` skills are *different documents* with independent
numbering (in the repo's, gotcha 4 is "TX audio cannot be verified on this VM";
in the global one, item 4 is the PowerShell 5.1 quoting trap and TX audio is 10).

Hardware-free: pure file reads over the repository.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".agents" / "skills"

#: Item lines of the numbered gotcha lists: "12. **Title.**" at column 0.
ITEM = re.compile(r"^(?P<num>\d+)\. \*\*", re.MULTILINE)
#: The defect in motivation 2: exactly one leading space before an item number.
INDENTED_ITEM = re.compile(r"^ \d+\. \*\*", re.MULTILINE)
#: "…(see gotcha 7)", "…(gotcha 7)", "…见 gotcha 7", "…陷阱 7".
SELF_REF = re.compile(r"(?:\((?:see )?gotcha (\d+)\)|见 gotcha (\d+)|陷阱 (\d+))")
#: "…`windows-installer` gotcha 4", "…macos-installer skill (gotcha 8", "…陷阱 8".
CROSS_REF = re.compile(
    r"(windows-installer|macos-installer|dual-platform-release|sdd-guardian)"
    r"[^\n|]{0,24}?(?:gotcha|陷阱)\s*(\d+)")

DOC_GLOBS = (".agents/skills/*/SKILL.md", "docs/**/*.md", "SDD/*.md", "CHANGELOG.md")


def skill_files() -> list[Path]:
    return sorted(SKILLS.glob("*/SKILL.md"))


def item_numbers(text: str) -> set[int]:
    """Every number used by a top-level item line, across all lists in the file."""
    return {int(m.group("num")) for m in ITEM.finditer(text)}


def numbered_lists(text: str) -> list[list[int]]:
    """The item numbers grouped into lists; a list starts wherever the count restarts at 1."""
    lists: list[list[int]] = []
    current: list[int] = []
    for m in ITEM.finditer(text):
        n = int(m.group("num"))
        if n == 1:
            if current:
                lists.append(current)
            current = [1]
        else:
            current.append(n)
    if current:
        lists.append(current)
    return lists


def in_scope_docs() -> list[Path]:
    out: list[Path] = []
    for glob in DOC_GLOBS:
        out.extend(p for p in ROOT.glob(glob) if p.is_file())
    return sorted(set(out))


class FrontmatterTests(unittest.TestCase):
    def test_every_skill_declares_a_name_matching_its_directory(self):
        for path in skill_files():
            with self.subTest(skill=path.parent.name):
                text = path.read_text(encoding="utf-8")
                m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
                if m is None:
                    self.fail("missing YAML frontmatter — the loader will skip it")
                fm = m.group(1)
                name = re.search(r"^name:\s*(\S+)\s*$", fm, re.MULTILINE)
                if name is None:
                    self.fail("frontmatter has no name:")
                self.assertEqual(name.group(1), path.parent.name,
                                 "name: must equal the directory name or the skill will not resolve")
                desc = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
                if desc is None:
                    self.fail("frontmatter has no description:")
                self.assertGreater(len(desc.group(1).strip()), 80,
                                   "the description is what decides whether the skill gets loaded; "
                                   "a terse one never triggers")


class NumberingTests(unittest.TestCase):
    def test_no_item_is_hidden_by_a_single_leading_space(self):
        """Motivation 2: an indented item starts a NEW list, so its number is unreachable."""
        for path in skill_files():
            with self.subTest(skill=path.parent.name):
                text = path.read_text(encoding="utf-8")
                hits = INDENTED_ITEM.findall(text)
                self.assertEqual(
                    hits, [],
                    f"{len(hits)} item line(s) start with one space; markdown renders them as a "
                    "separate list restarting at 1, so cross-references to those numbers break")

    def test_every_numbered_list_runs_from_one_without_gaps(self):
        """Motivation 1: inserting an item mid-list silently shifts every later number."""
        for path in skill_files():
            with self.subTest(skill=path.parent.name):
                for lst in numbered_lists(path.read_text(encoding="utf-8")):
                    self.assertEqual(lst, list(range(1, len(lst) + 1)),
                                     "a numbered list restarted or skipped: renumber it, or append "
                                     "new items at the end so existing references stay valid")


class ReferenceTests(unittest.TestCase):
    def test_self_references_resolve_inside_the_same_skill(self):
        for path in skill_files():
            text = path.read_text(encoding="utf-8")
            have = item_numbers(text)
            for m in SELF_REF.finditer(text):
                n = int(next(g for g in m.groups() if g))
                with self.subTest(skill=path.parent.name, ref=n):
                    self.assertIn(n, have,
                                  f"references item {n}, but this skill's items are {sorted(have)}")

    def test_cross_references_resolve_in_the_named_skill(self):
        """A reference names a skill, so it must exist in *that* skill's numbering."""
        numbers = {p.parent.name: item_numbers(p.read_text(encoding="utf-8"))
                   for p in skill_files()}
        checked = 0
        for path in in_scope_docs():
            text = path.read_text(encoding="utf-8", errors="replace")
            for m in CROSS_REF.finditer(text):
                skill, n = m.group(1), int(m.group(2))
                if skill not in numbers:
                    continue                      # a skill that lives outside this repository
                checked += 1
                with self.subTest(doc=path.relative_to(ROOT).as_posix(), skill=skill, ref=n):
                    self.assertIn(n, numbers[skill],
                                  f"`{skill}` has no item {n} (its items are {sorted(numbers[skill])}); "
                                  "the referenced skill was probably renumbered")
        self.assertGreater(checked, 0,
                           "no cross-reference was checked — the pattern or the scope has drifted")


class StructureTests(unittest.TestCase):
    def test_code_fences_are_balanced(self):
        """An unclosed fence swallows the rest of the document, headings included."""
        for path in skill_files():
            with self.subTest(skill=path.parent.name):
                text = path.read_text(encoding="utf-8")
                fences = re.findall(r"^ *```", text, re.MULTILINE)
                self.assertEqual(len(fences) % 2, 0,
                                 f"{len(fences)} fence lines — one is unclosed")

    def test_the_skills_actually_cover_the_release_pipeline(self):
        """A guard against a skill being renamed/removed while docs still point at it."""
        names = {p.parent.name for p in skill_files()}
        for expected in ("windows-installer", "macos-installer", "dual-platform-release"):
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
