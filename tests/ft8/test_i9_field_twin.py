"""The field twin must measure the decoder production runs, not a variant of it.

``tools/ft8/i9_thread_matrix.py`` exists because ``packaging/common/stage-tree.sh`` excludes
``tests/``, ``tools/`` and ``dev_tools/``: a production box has no test tree, so the A53
thread matrix can only be measured by a standalone script.  Its entire value is that its
numbers describe **the deployed decoder** — which is why it mirrors the pytest fixture's slot
construction and request rather than inventing its own.

One invariant that is easy to lose is the process working directory.  The vendored decoder
resolves its callsign database as ``trim(share_dir)//'/ALLCALL7.TXT'`` with ``share_dir = '.'``
(``ft8/dsp/vendor/prog_args.f90:4``) — that is, **relative to the cwd** — and the service unit
pins ``WorkingDirectory=$MRRC_HOME`` (``packaging/common/mrrc-service-layer.sh:200``), so
production always finds it.  A twin started from anywhere else runs the decoder without that
database, and the only symptom is one line on stdout (``ALLCALL7.TXT is too short or
broken?``) that the pytest harness never prints.

Measured on 2026-10-14 (macOS, profile 3, threads 3, 1 warmup + 3 measured): the missing
database does **not** move this workload's wall or CPU time — with it 0.193 s / 0.452 s,
without it 0.193–0.202 s / 0.453–0.470 s across three runs, the first reading being the
outlier.  So the field numbers survive it.  What does not survive is the twin's own claim to
reproduce production, and a divergence whose only symptom is a warning nobody reads is how
the next measurement quietly stops meaning what it says.  Hence the twin puts itself in the
app root, and this test holds it there.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from ft8.core.paths import app_root

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "ft8" / "i9_thread_matrix.py"


def _load_tool():
    """Import the twin by path — ``tools/`` is not a package and must not become one."""
    assert TOOL.is_file(), f"the field twin moved: {TOOL}"
    spec = importlib.util.spec_from_file_location("_i9_thread_matrix_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_callsign_database_is_shipped_where_the_decoder_looks_for_it():
    """``share_dir = '.'`` means the app root, so the file has to live there."""
    assert (REPO / "ALLCALL7.TXT").is_file(), (
        "the decoder opens trim(share_dir)//'/ALLCALL7.TXT' with share_dir='.' "
        "(ft8/dsp/vendor/prog_args.f90:4) and prints a warning when it is absent"
    )


def test_the_tool_still_reads_its_library_from_the_package_not_the_cwd():
    """The cwd fix must not be paid for with a cwd-dependent library path.

    ``default_library_path()`` resolves through ``ft8.core.paths.app_root()``, which is
    ``Path(__file__).parents[2]`` in source — the twin's ``--app-root`` only feeds ``sys.path``.
    """
    assert app_root() == REPO


def test_the_twin_puts_the_process_in_the_app_root(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO)
    before = list(sys.path)
    try:
        resolved = _load_tool().enter_app_root(str(tmp_path))
        assert Path.cwd().resolve() == tmp_path.resolve()
        assert resolved == str(tmp_path.resolve())
        assert resolved in sys.path
    finally:
        sys.path[:] = before


def test_entering_the_app_root_is_idempotent(tmp_path, monkeypatch):
    """The deploy script may already have cd'd there, and twice must not mean twice on
    ``sys.path`` — a duplicated entry shadows nothing today but is a silent surprise the
    moment the two paths differ only by a symlink."""
    monkeypatch.chdir(REPO)
    before = list(sys.path)
    try:
        module = _load_tool()
        first = module.enter_app_root(str(tmp_path))
        second = module.enter_app_root(str(tmp_path))
        assert first == second == str(tmp_path.resolve())
        assert sys.path.count(first) == 1
    finally:
        sys.path[:] = before


def test_relative_app_root_resolves_against_the_original_cwd(tmp_path, monkeypatch):
    """``--app-root`` is a user-facing flag; ``.`` has to mean where they typed it, not
    where an earlier chdir left the process."""
    (tmp_path / "ft8").mkdir()
    monkeypatch.chdir(tmp_path)
    before = list(sys.path)
    try:
        resolved = _load_tool().enter_app_root(".")
        assert resolved == str(tmp_path.resolve())
        assert os.getcwd() == resolved
    finally:
        sys.path[:] = before
