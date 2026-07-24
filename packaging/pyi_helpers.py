"""Shared helpers for the PyInstaller spec files."""

from __future__ import annotations

import glob
import os
import sys


def tcltk9_binaries() -> list[tuple[str, str]]:
    """Collect Tcl/Tk 9 shared libraries that PyInstaller's hook misses.

    python-build-standalone (uv-managed) CPython links _tkinter against
    Tcl/Tk 9.0; PyInstaller's tkinter support predates the 9.x naming and
    collects the data directories but not these libraries.  A no-op on
    interpreters using Tcl/Tk 8.6.
    """
    roots = (
        os.path.join(sys.base_prefix, "lib"),
        os.path.join(sys.base_prefix, "DLLs"),
        os.path.join(sys.base_prefix, "bin"),
    )
    patterns = ("libtcl9*", "libtk9*", "tcl9*.dll", "tk9*.dll", "*tcl9*.dylib", "*tk9*.dylib")
    found: dict[str, tuple[str, str]] = {}
    for root in roots:
        for pattern in patterns:
            for path in glob.glob(os.path.join(root, pattern)):
                if os.path.isfile(path):
                    found[os.path.basename(path)] = (path, ".")
    return list(found.values())
