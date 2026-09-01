"""Explicit project dependency lock command."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from zlang._version import __version__
from zlang.workspace import WorkspaceError, update_project_lock


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zlang-lock",
        description="Resolve and pin ZLang project dependencies",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    update = commands.add_parser("update", help="fetch and atomically update zlang.lock")
    update.add_argument(
        "--project",
        type=Path,
        default=Path("."),
        help="zlang.toml or its containing directory (default: current directory)",
    )
    update.add_argument("-v", "--verbose", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        lock = update_project_lock(arguments.project)
    except WorkspaceError as error:
        print(f"zlang-lock: error: {error}", file=sys.stderr)
        return 1
    if arguments.verbose:
        modules = sum(len(package.modules) for package in lock.packages)
        print(
            f"zlang-lock: ok: {len(lock.packages)} package(s), "
            f"{modules} module(s), identity {lock.identity}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
