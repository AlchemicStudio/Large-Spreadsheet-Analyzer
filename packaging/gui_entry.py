"""PyInstaller entry point for the GUI executable."""

import multiprocessing

from lsa.gui.app import main

if __name__ == "__main__":
    # The scan runs in a spawned subprocess: in a frozen build the child
    # re-executes this binary, and freeze_support() routes it correctly.
    multiprocessing.freeze_support()
    main()
