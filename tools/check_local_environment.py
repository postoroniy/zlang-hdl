# SPDX-License-Identifier: Apache-2.0
"""Fail closed when a ZLang command would use another checkout's environment."""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _stale_checkout_metadata(root: Path) -> tuple[Path, ...]:
    """Return build metadata that can shadow the local editable distribution."""

    return tuple(sorted(root.glob("zlang_hdl.egg-info")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--venv", required=True, type=Path)
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    venv = arguments.venv.resolve()

    stale_metadata = _stale_checkout_metadata(root)
    if stale_metadata:
        parser.error(
            "stale checkout distribution metadata can shadow the local venv: "
            f"{stale_metadata[0]}; move it below build/tmp and run 'make venv'"
        )

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
    try:
        installed_version = version("zlang-hdl")
    except PackageNotFoundError:
        parser.error("ZLang distribution metadata is missing; reinstall with 'make venv'")
    if installed_version != zlang.__version__:
        parser.error(
            "ZLang distribution metadata is stale: "
            f"source reports {zlang.__version__}, metadata reports {installed_version}; "
            "remove stale zlang_hdl.egg-info and run 'make venv'"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
