"""Command-line interface for the prototype compiler."""

from __future__ import annotations

import sys
from typing import Sequence

from zlang.cli_command import run_compile_command


def main(argv: Sequence[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    if effective_argv and effective_argv[0] == "sim":
        from zlang.sim_cli import main as simulation_main

        return simulation_main(effective_argv[1:])
    if effective_argv and effective_argv[0] == "verify":
        from zlang.verification_cli import main as verification_main

        return verification_main(effective_argv[1:], prog="zlang verify")
    if effective_argv and effective_argv[0] == "lock":
        from zlang.project_cli import main as project_main

        return project_main(effective_argv[1:], prog="zlang lock")
    if effective_argv and effective_argv[0] == "lsp":
        from zlang.lsp.server import main as lsp_main

        return lsp_main(effective_argv[1:], prog="zlang lsp")
    return run_compile_command(effective_argv)


if __name__ == "__main__":
    raise SystemExit(main())
