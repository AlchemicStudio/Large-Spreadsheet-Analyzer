"""PyInstaller entry point for the CLI executable."""

import sys

from lsa.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
