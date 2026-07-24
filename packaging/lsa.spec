# PyInstaller spec: builds the windowed GUI (lsa-gui) and the console CLI (lsa)
# into one shared dist/lsa folder.  Build with:
#     pyinstaller packaging/lsa.spec --noconfirm
#
# --collect-all customtkinter is mandatory (theme JSON assets are loaded from
# the package directory at runtime); here it is done via collect_all().

import os
import sys

from PyInstaller.utils.hooks import collect_all

sys.path.insert(0, SPECPATH)  # noqa: F821
from pyi_helpers import tcltk9_binaries

ctk_datas, ctk_binaries, ctk_hidden = collect_all("customtkinter")
tcltk_binaries = tcltk9_binaries()

# Our own package data (i18n catalogs) — added explicitly so the build works
# with editable installs too.
locales_src = os.path.join(SPECPATH, "..", "src", "lsa", "core", "locales")  # noqa: F821
lsa_datas = [(locales_src, "lsa/core/locales")]

# charset_normalizer ships a mypyc-compiled module PyInstaller cannot always
# detect statically.
hidden = [*ctk_hidden, "charset_normalizer.md__mypyc"]

a_gui = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "gui_entry.py")],  # noqa: F821
    datas=ctk_datas + lsa_datas,
    binaries=ctk_binaries + tcltk_binaries,
    hiddenimports=hidden,
)
a_cli = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "cli_entry.py")],  # noqa: F821
    datas=lsa_datas,
    hiddenimports=["charset_normalizer.md__mypyc"],
)

pyz_gui = PYZ(a_gui.pure)  # noqa: F821
pyz_cli = PYZ(a_cli.pure)  # noqa: F821

exe_gui = EXE(  # noqa: F821
    pyz_gui,
    a_gui.scripts,
    exclude_binaries=True,
    name="lsa-gui",
    console=False,
)
exe_cli = EXE(  # noqa: F821
    pyz_cli,
    a_cli.scripts,
    exclude_binaries=True,
    name="lsa",
    console=True,
)

coll = COLLECT(  # noqa: F821
    exe_gui,
    exe_cli,
    a_gui.binaries,
    a_gui.datas,
    a_cli.binaries,
    a_cli.datas,
    name="lsa",
)
