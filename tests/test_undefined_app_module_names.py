"""A module used but never imported: the bug class that shipped twice (guard test).

``server.py`` of v1.24.5 called ``ssl_bootstrap.sign_for(...)`` with no ``import
ssl_bootstrap`` in the file. Nothing caught it: the call sits inside a broad ``except``, so
the server started, logged one line, and served plain HTTP while the launcher opened
HTTPS — the "black screen after installing" field report. v1.24.1 had the identical shape
with ``cloud_hub``, and it too passed the whole suite, PyInstaller x3, Inno Setup, and a
matching published SHA-256.

Both were NameErrors on an *app module attribute base* — a closed set you can check
 statically. ``pyflakes``/``ruff`` would do this and more, but they are not installed here
 and adding one to ``requirements-build.txt`` (PyInstaller only, today) would force every
 VM build venv to be rebuilt, so this guard is stdlib ``ast`` instead.

Scope on purpose: it checks names that are known app modules and nothing else. A typo in
``some_random_helper()`` is not this bug class, and widening the rule to every attribute
base would make the suite fail on constructs it cannot resolve. Real undefined names
outside this set are pyflakes' job; the two releases this repo actually lost are here.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Where app code lives. Tests, tooling and packaging are excluded: they are not frozen
# into a shipped entry-point and a NameError there is a loud local failure, not a field
# outage.
SOURCE_DIRS = ["windows", "macos", "backends", "audio", "web"]


def app_module_names() -> set[str]:
    """Top-level names importable as bare app modules (root modules + packages)."""
    names = {p.stem for p in ROOT.glob("*.py")}
    for child in ROOT.iterdir():
        if child.is_dir() and (child / "__init__.py").exists():
            names.add(child.name)
    names.discard("__init__")
    return names


def _bound_names(tree: ast.Module) -> set[str]:
    """Every name the file binds: imports, defs, assignments, comprehension/walrus targets.

    Deliberately generous. A name that appears as a store target anywhere counts as bound,
    because a locally reassigned variable with the same spelling as a module
    (``recorder = get_recorder()`` then ``recorder.save()``) is not a missing import, and a
    guard that cries wolf gets switched off.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b.c` binds `a`; `import a as x` binds `x`.
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                bound.add(arg.arg)
            if node.args.vararg:
                bound.add(node.args.vararg.arg)
            if node.args.kwarg:
                bound.add(node.args.kwarg.arg)
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
            for base in node.bases:            # `class C(Mixin, recorder.Base):`
                if isinstance(base, ast.Name):
                    bound.add(base.id)
            for kw in node.keywords:
                if isinstance(kw.value, ast.Name):
                    bound.add(kw.value.id)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound.update(node.names)
    return bound


def undefined_app_attributes(source: str, modules: set[str],
                            filename: str = "<string>") -> list[str]:
    """App modules used as ``name.thing`` in this source but never bound by it."""
    tree = ast.parse(source, filename=filename)
    if any(isinstance(node, ast.ImportFrom)
           and any(alias.name == "*" for alias in node.names) for node in ast.walk(tree)):
        # A star import can bind a module we cannot see; refuse to guess instead of
        # reporting a false failure. Nothing in the app does this today.
        return []
    bound = _bound_names(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        base = node.value
        if not isinstance(base, ast.Name):
            continue
        name = base.id
        if name in modules and name not in bound and name not in hits:
            hits.append(name)
    return hits


class GuardSelfTests(unittest.TestCase):
    """The checker itself: it must fire on the real bug and stay quiet otherwise."""

    MODULES = {"ssl_bootstrap", "cloud_hub", "recorder"}

    def test_catches_the_exact_1_24_5_defect(self):
        """What shipped in 1.24.5: the call is inside a try/except, so it only ever raised
        a swallowed NameError at runtime — and no test in the suite noticed."""
        source = '''
import logging

log = logging.getLogger("server")


def _resolve_ssl_kwargs(args):
    try:
        return ssl_bootstrap.sign_for(args.host, args.port)
    except Exception:
        log.warning("falling back to plain HTTP")
        return {}
'''
        self.assertEqual(undefined_app_attributes(source, self.MODULES), ["ssl_bootstrap"])

    def test_catches_the_exact_1_24_1_defect(self):
        self.assertEqual(
            undefined_app_attributes("def f():\n    return cloud_hub.start()\n",
                                     self.MODULES),
            ["cloud_hub"])

    def test_imported_module_is_clean(self):
        self.assertEqual(
            undefined_app_attributes("import ssl_bootstrap\ndef f():\n"
                                     "    return ssl_bootstrap.sign_for(1, 2)\n",
                                     self.MODULES), [])

    def test_function_level_import_is_clean(self):
        """Importing inside the function is a normal way to defer a heavy dependency."""
        self.assertEqual(
            undefined_app_attributes("def f():\n    import ssl_bootstrap\n"
                                     "    return ssl_bootstrap.sign_for(1, 2)\n",
                                     self.MODULES), [])

    def test_local_rebinding_is_not_reported(self):
        source = ("class Box:\n    def save(self):\n        pass\n\n\n"
                  "def f():\n    recorder = Box()\n    recorder.save()\n")
        self.assertEqual(undefined_app_attributes(source, self.MODULES), [])

    def test_names_outside_app_modules_are_not_reported(self):
        """A typo in a third-party or stdlib base is not this bug class."""
        self.assertEqual(
            undefined_app_attributes("def f():\n    mystery.helper()\n", self.MODULES), [])

    def test_star_import_disables_the_guess(self):
        source = "from somemod import *\n\n\ndef f():\n    return recorder.x()\n"
        self.assertEqual(undefined_app_attributes(source, self.MODULES), [])


class AppCodeGuardTests(unittest.TestCase):
    """The build gate: no app module used without being bound, anywhere in shipped code."""

    def test_app_sources_have_no_unimported_module_attributes(self):
        modules = app_module_names()
        files = sorted(ROOT.glob("*.py"))
        for dirname in SOURCE_DIRS:
            directory = ROOT / dirname
            if directory.is_dir():
                files.extend(sorted(directory.rglob("*.py")))
        self.assertTrue(files, "no sources found — check the path constants")

        offenders: list[str] = []
        for path in files:
            try:
                source = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            try:
                tree_hits = undefined_app_attributes(
                    source, modules, filename=str(path))
            except SyntaxError:                       # not our business; py_compile owns it
                continue
            for name in tree_hits:
                offenders.append(f"{path.relative_to(ROOT)}: `{name}` used, never imported")
        self.assertEqual(
            offenders, [],
            "An app module is referenced without being imported. This is the shipped-twice "
            "bug class (v1.24.1 cloud_hub, v1.24.5 ssl_bootstrap): the call lives in a "
            "broad except, so the suite stays green while the product degrades in the "
            "field. Add the import.")

    def test_server_entry_point_imports_the_tls_bootstrap(self):
        """The one-line regression test for the field failure itself.

        ``import ssl_bootstrap`` must stay a top-level import: a deferred or conditional
        import is what let the 1.24.5 package ship without it reaching the call site.
        """
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        top_level = {alias.name
                     for node in tree.body if isinstance(node, ast.Import)
                     for alias in node.names}
        self.assertIn("ssl_bootstrap", top_level)


if __name__ == "__main__":
    unittest.main()
