# SPDX-License-Identifier: Apache-2.0
"""Fail closed when a ZLang command would use another checkout's environment."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--venv", required=True, type=Path)
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    venv = arguments.venv.resolve()

    if not _is_within(venv, root):
        parser.error(f"virtual environment must be below this worktree: {root}")
    if Path(sys.prefix).resolve() != venv:
        parser.error(
            "Python is not running from the requested local virtual environment; "
            "run 'make venv' then 'source .venv/bin/activate'"
        )
    executable = Path(sys.executable).absolute()
    try:
        executable.relative_to(venv)
    except ValueError:
        parser.error("Python executable is not below the local virtual environment")

    import zlang

    package = Path(zlang.__file__ or "").resolve()
    expected_package = root / "zlang"
    if not _is_within(package, expected_package):
        parser.error(
            f"ZLang import resolved outside this worktree: {package}; "
            "reinstall with 'make venv'"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
