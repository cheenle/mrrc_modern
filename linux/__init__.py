"""Linux-side helpers for a headless box install.

This exists so ``linux.*`` is importable as a package (``linux/mrrc_radio.py``
is tested directly, and a namespace package gives no module to import from).
The modules here are run either by the venv's python on the box or, in tests,
from the repository root.
"""
