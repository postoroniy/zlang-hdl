"""Explicit project dependency lock command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from zlang._version import __version__
from zlang.cli_diagnostics import print_cli_diagnostic
from zlang.diagnostics import DiagnosticError
from zlang.workspace import WorkspaceError, update_project_lock


def main(
    argv: Sequence[str] | None = None,
    *,
    prog: str = "zlang lock",
) -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
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
    inspect = commands.add_parser("toolchain", help="inspect exact compiler/stdlib identities without writes")
    inspect.add_argument("--json", action="store_true", dest="as_json")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "toolchain":
            from zlang.toolchain_lock import current_toolchain
            snapshot = current_toolchain()
            print(json.dumps(snapshot.to_data() if arguments.as_json else snapshot.requirement.to_data(),
                             indent=2, sort_keys=True))
            return 0
        lock = update_project_lock(arguments.project)
    except DiagnosticError as error:
        print_cli_diagnostic(error, program=prog)
        return 1
    except WorkspaceError as error:
        print(f"{prog}: error: {error}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"{prog}: error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print_cli_diagnostic(error, program=prog)
        return 1
    if arguments.verbose:
        modules = sum(len(package.modules) for package in lock.packages)
        print(
            f"{prog}: ok: {len(lock.packages)} package(s), "
            f"{modules} module(s), identity {lock.identity}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
