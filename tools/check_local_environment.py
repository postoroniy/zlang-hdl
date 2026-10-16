# SPDX-License-Identifier: Apache-2.0
"""Fail closed when a ZLang command would use another checkout's environment."""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys
import tempfile


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _stale_checkout_metadata(root: Path) -> tuple[Path, ...]:
    """Return build metadata that can shadow the local editable distribution."""

    return tuple(sorted(root.glob("zlang_hdl.egg-info")))


def _quarantine_stale_checkout_metadata(root: Path, tmp_root: Path) -> Path | None:
    """Move legacy checkout metadata aside without deleting recovery evidence."""

    stale_metadata = _stale_checkout_metadata(root)
    if not stale_metadata:
        return None
    tmp_root.mkdir(parents=True, exist_ok=True)
    quarantine = Path(
        tempfile.mkdtemp(prefix="stale-checkout-metadata.", dir=tmp_root)
    ).resolve()
    for metadata in stale_metadata:
        metadata.replace(quarantine / metadata.name)
    return quarantine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--venv", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--bootstrap", action="store_true",
        help="validate interpreter ownership before project dependencies are installed",
    )
    mode.add_argument("--quarantine-stale-metadata", action="store_true")
    parser.add_argument("--tmp-root", type=Path)
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()

    if arguments.quarantine_stale_metadata:
        if arguments.tmp_root is None:
            parser.error("--tmp-root is required with --quarantine-stale-metadata")
        quarantine = _quarantine_stale_checkout_metadata(
            root, arguments.tmp_root.resolve()
        )
        if quarantine is not None:
            print(
                "quarantined stale checkout distribution metadata under "
                f"{quarantine}",
                file=sys.stderr,
            )
        return 0

    if arguments.venv is None:
        parser.error("--venv is required unless --quarantine-stale-metadata is used")
    venv = arguments.venv.resolve()

    stale_metadata = _stale_checkout_metadata(root)
    if stale_metadata and not arguments.bootstrap:
        parser.error(
            "stale checkout distribution metadata can shadow the local venv: "
            f"{stale_metadata[0]}; run 'make venv' to quarantine it below build/tmp"
        )

    if not _is_within(venv, root):
        parser.error(f"virtual environment must be below this worktree: {root}")
    if Path(sys.prefix).resolve() != venv:
        parser.error(
            "Python is not running from the requested local virtual environment; "
            "run 'make venv' then 'source .venv/bin/activate'"
        )
    if sys.implementation.name != "cpython" or not (3, 12) <= sys.version_info < (3, 13):
        parser.error("this worktree requires CPython 3.12; preserve the incompatible venv and rerun 'make venv'")
    executable = Path(sys.executable).absolute()
    try:
        executable.relative_to(venv)
    except ValueError:
        parser.error("Python executable is not below the local virtual environment")

    if arguments.bootstrap:
        return 0

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
