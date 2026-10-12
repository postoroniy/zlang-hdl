# SPDX-License-Identifier: Apache-2.0
"""Require Python-using GitHub workflows to activate a checkout-local venv."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


_PYTHON_COMMAND = re.compile(
    r"^\s*(?:-\s*)?(?:python(?:3)?|pip|pytest|zlang)(?:\s|$)|"
    r"^\s*run:\s*(?:python(?:3)?|pip|pytest|zlang)(?:\s|$)"
)
_HOOK = "BASH_ENV: ${{ github.workspace }}/.github/workflows/local-venv-env.sh"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args(argv)
    workflows = arguments.root / ".github" / "workflows"
    failures: list[str] = []
    for path in sorted(workflows.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        uses_python = any(_PYTHON_COMMAND.search(line) for line in text.splitlines())
        if uses_python and _HOOK not in text:
            failures.append(
                f"{path}: Python commands require the local-venv BASH_ENV hook"
            )
    if failures:
        parser.error("\n".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
