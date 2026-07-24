"""Build single-file executables that bundle every library.

Produces, in ``dist/bundle/``:

- ``lsa-gui-<os>-<arch>[.exe]`` — the GUI, one self-contained binary
- ``lsa-<os>-<arch>[.exe]``     — the CLI, one self-contained binary

Usage (PyInstaller must be installed, e.g. ``uv sync --group build``):

    uv run python scripts/build_bundle.py [--skip-smoke]

Used by ``.github/workflows/build.yml`` on every pushed branch.  Note that
a Linux binary runs on distributions with a glibc at least as new as the
build machine's — CI builds on the oldest available runner for that reason.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _platform_tag() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64"}.get(machine, machine)
    return f"{system}-{machine}"


def build(skip_smoke: bool) -> int:
    dist = ROOT / "dist" / "onefile"
    bundle = ROOT / "dist" / "bundle"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(ROOT / "packaging" / "lsa-onefile.spec"),
            "--noconfirm",
            "--distpath",
            str(dist),
            "--workpath",
            str(ROOT / "build" / "onefile"),
        ],
        check=True,
        cwd=ROOT,
    )

    tag = _platform_tag()
    suffix = ".exe" if platform.system() == "Windows" else ""
    bundle.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for built_name, out_base in (("lsa", f"lsa-{tag}"), ("lsa-gui", f"lsa-gui-{tag}")):
        src = dist / f"{built_name}{suffix}"
        if not src.is_file():
            print(f"error: expected build output missing: {src}", file=sys.stderr)
            return 1
        target = bundle / f"{out_base}{suffix}"
        shutil.copy2(src, target)
        outputs.append(target)

    if not skip_smoke:
        cli = outputs[0]
        subprocess.run([str(cli), "--version"], check=True)
        subprocess.run(
            [str(cli), "validate-rules", str(ROOT / "docs" / "rules.example.json")],
            check=True,
        )

    print("\nbundle ready:")
    for path in outputs:
        print(f"  {path.relative_to(ROOT)}  ({path.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-smoke", action="store_true", help="do not run the built CLI as a smoke test"
    )
    args = parser.parse_args(argv)
    return build(skip_smoke=args.skip_smoke)


if __name__ == "__main__":
    raise SystemExit(main())
