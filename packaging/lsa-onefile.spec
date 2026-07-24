# PyInstaller spec: SINGLE-FILE executables — every library bundled inside
# one binary per front end (lsa-gui windowed, lsa console).  Build with:
#     pyinstaller packaging/lsa-onefile.spec --noconfirm \
#         --distpath dist/onefile --workpath build/onefile
# or simply: python scripts/build_bundle.py
#
# Compared to packaging/lsa.spec (onedir, used for releases), onefile trades
# startup time (self-extraction) for single-file distribution.

import os
import sys

from PyInstaller.utils.hooks import collect_all

sys.path.insert(0, SPECPATH)  # noqa: F821
from pyi_helpers import tcltk9_binaries

ctk_datas, ctk_binaries, ctk_hidden = collect_all("customtkinter")
tcltk_binaries = tcltk9_binaries()

locales_src = os.path.join(SPECPATH, "..", "src", "lsa", "core", "locales")  # noqa: F821
lsa_datas = [(locales_src, "lsa/core/locales")]
hidden = [*ctk_hidden, "charset_normalizer.md__mypyc"]

a_gui = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "gui_entry.py")],  # noqa: F821
    datas=ctk_datas + lsa_datas,
    binaries=ctk_binaries + tcltk_binaries,
    hiddenimports=hidden,
)
pyz_gui = PYZ(a_gui.pure)  # noqa: F821
exe_gui = EXE(  # noqa: F821
    pyz_gui,
    a_gui.scripts,
    a_gui.binaries,
    a_gui.datas,
    name="lsa-gui",
    console=False,
    upx=False,
)

a_cli = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "cli_entry.py")],  # noqa: F821
    datas=lsa_datas,
    hiddenimports=["charset_normalizer.md__mypyc"],
)
pyz_cli = PYZ(a_cli.pure)  # noqa: F821
exe_cli = EXE(  # noqa: F821
    pyz_cli,
    a_cli.scripts,
    a_cli.binaries,
    a_cli.datas,
    name="lsa",
    console=True,
    upx=False,
)
