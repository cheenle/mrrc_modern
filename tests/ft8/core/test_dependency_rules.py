from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
_CTYPES_LOADERS = {"CDLL", "PyDLL", "WinDLL", "OleDLL"}
_CTYPES_LIBRARY_OBJECTS = {"cdll", "pydll", "windll", "oledll"}


def _server_modules(root: Path) -> list[Path]:
    return sorted((root / "ft8").rglob("*.py"))


def _uses_ctypes_loader(tree: ast.AST) -> bool:
    ctypes_names = {"ctypes"}
    numpy_names: set[str] = set()
    direct_loader_names: set[str] = set()
    library_object_names: set[str] = set()
    numpy_loader_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            ctypes_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "ctypes"
            )
            numpy_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "numpy"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "ctypes":
            for alias in node.names:
                local_name = alias.asname or alias.name
                if alias.name == "*":
                    return True
                if alias.name in _CTYPES_LOADERS:
                    direct_loader_names.add(local_name)
                elif alias.name in _CTYPES_LIBRARY_OBJECTS:
                    library_object_names.add(local_name)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module == "numpy.ctypeslib"
        ):
            numpy_loader_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "load_library"
            )

    if direct_loader_names or library_object_names or numpy_loader_names:
        return True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if (
            node.attr in _CTYPES_LOADERS | _CTYPES_LIBRARY_OBJECTS
            and isinstance(node.value, ast.Name)
            and node.value.id in ctypes_names
        ):
            return True
        if (
            node.attr == "load_library"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "ctypeslib"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id in numpy_names
        ):
            return True
    return False


def _ctypes_loader_offenders(root: Path) -> list[str]:
    offenders: list[str] = []
    for path in _server_modules(root):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if relative != "ft8/core/binding.py" and _uses_ctypes_loader(tree):
            offenders.append(relative)
    return offenders


def _imports_binding(tree: ast.AST) -> bool:
    return any(
        (
            isinstance(node, ast.Import)
            and any(alias.name == "ft8.core.binding" for alias in node.names)
        )
        or (
            isinstance(node, ast.ImportFrom)
            and node.module == "ft8.core.binding"
        )
        or (
            isinstance(node, ast.ImportFrom)
            and node.module == "ft8.core"
            and any(alias.name == "binding" for alias in node.names)
        )
        or (
            isinstance(node, ast.ImportFrom)
            and node.level > 0
            and (
                node.module == "binding"
                or (
                    node.module in {None, "ft8.core"}
                    and any(alias.name == "binding" for alias in node.names)
                )
            )
        )
        for node in ast.walk(tree)
    )


def _binding_import_offenders(root: Path) -> list[str]:
    offenders: list[str] = []
    for path in _server_modules(root):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if relative != "ft8/core/worker.py" and _imports_binding(tree):
            offenders.append(relative)
    return offenders


def test_only_binding_loads_ctypes_library() -> None:
    assert _ctypes_loader_offenders(ROOT) == []


def test_only_worker_imports_binding() -> None:
    assert _binding_import_offenders(ROOT) == []


def test_binding_sets_openmp_stack_before_numpy_import_or_cdll_load() -> None:
    path = ROOT / "ft8/core/binding.py"
    text = path.read_text(encoding="utf-8")
    stack_default = text.index('os.environ.setdefault("OMP_STACKSIZE", "10M")')
    numpy_import = text.index("import numpy")
    cdll_call = text.index("ctypes.CDLL(")
    assert stack_default < numpy_import
    assert stack_default < cdll_call


def test_binding_defines_exactly_one_module_level_dsp_lock() -> None:
    path = ROOT / "ft8/core/binding.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignments = [
        node
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and (
            any(
                isinstance(target, ast.Name) and target.id == "DSP_LOCK"
                for target in getattr(node, "targets", [])
            )
            or (
                isinstance(getattr(node, "target", None), ast.Name)
                and node.target.id == "DSP_LOCK"
            )
        )
    ]
    assert len(assignments) == 1


@pytest.mark.parametrize(
    "source",
    [
        "import ctypes\nctypes.CDLL('x')\n",
        "import ctypes\nctypes.WinDLL('x')\n",
        "def load():\n    import ctypes as c\n    return c.PyDLL('x')\n",
        "from ctypes import WinDLL as load\nload('x')\n",
        "from ctypes import OleDLL as load\nload('x')\n",
        "import ctypes\nctypes.cdll.LoadLibrary('x')\n",
        "import ctypes as c\nc.windll.LoadLibrary('x')\n",
        "import ctypes as c\nc.oledll.LoadLibrary('x')\n",
        "from ctypes import pydll as loader\nloader.LoadLibrary('x')\n",
    ],
)
def test_ctypes_loader_detector_covers_standard_alias_forms(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "ft8/engine/loader.py"
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")

    assert _ctypes_loader_offenders(tmp_path) == ["ft8/engine/loader.py"]


@pytest.mark.parametrize(
    "source",
    [
        "import ctypes\nload = ctypes.CDLL\nload('x')\n",
        "import ctypes\nloader = ctypes.cdll\nloader.LoadLibrary('x')\n",
        "from ctypes import *\nCDLL('x')\n",
        "import numpy as np\nnp.ctypeslib.load_library('x', '.')\n",
        "from numpy.ctypeslib import load_library as load\nload('x', '.')\n",
    ],
)
def test_ctypes_loader_detector_covers_assignment_star_and_numpy_forms(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "ft8/engine/loader.py"
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")

    assert _ctypes_loader_offenders(tmp_path) == ["ft8/engine/loader.py"]


def test_ctypes_loader_whitelist_is_exact_path(tmp_path: Path) -> None:
    allowed = tmp_path / "ft8/core/binding.py"
    offender = tmp_path / "ft8/other/binding.py"
    allowed.parent.mkdir(parents=True)
    offender.parent.mkdir(parents=True)
    source = "import ctypes\nctypes.CDLL('x')\n"
    allowed.write_text(source, encoding="utf-8")
    offender.write_text(source, encoding="utf-8")

    assert _ctypes_loader_offenders(tmp_path) == ["ft8/other/binding.py"]


def test_binding_import_whitelist_is_exact_worker_path(tmp_path: Path) -> None:
    allowed = tmp_path / "ft8/core/worker.py"
    offender = tmp_path / "ft8/other/worker.py"
    allowed.parent.mkdir(parents=True)
    offender.parent.mkdir(parents=True)
    source = "from ft8.core.binding import CoreBinding\n"
    allowed.write_text(source, encoding="utf-8")
    offender.write_text(source, encoding="utf-8")

    assert _binding_import_offenders(tmp_path) == ["ft8/other/worker.py"]
