# SPDX-License-Identifier: Apache-2.0
"""Reject GitHub workflow structures that fail before useful validation.

GitHub rejects a workflow before any job can run when its top-level mapping
contains the same key more than once.  Keep this deliberately small audit in
the independent static CI path so an invalid Release workflow cannot hide the
failure by being unable to start itself.  Repository tools that rely on package
imports must also run with ``python -m`` so hosted execution matches local
execution without a masking ``PYTHONPATH``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re


_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s|$)")
_DIRECT_RELEASE_STATUS = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:python|python3)\s+tools/release_status\.py(?:\s|$)"
)


def duplicate_top_level_keys(path: Path) -> tuple[str, ...]:
    first_lines: dict[str, int] = {}
    duplicates: list[str] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        match = _TOP_LEVEL_KEY.match(line)
        if match is None:
            continue
        key = match.group(1)
        first_line = first_lines.get(key)
        if first_line is None:
            first_lines[key] = line_number
            continue
        duplicates.append(
            f"{path}:{line_number}: duplicate top-level key {key!r} "
            f"(first declared on line {first_line})"
        )
    return tuple(duplicates)


def unsafe_repo_tool_invocations(path: Path) -> tuple[str, ...]:
    failures: list[str] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if _DIRECT_RELEASE_STATUS.search(line):
            failures.append(
                f"{path}:{line_number}: invoke release status as "
                "'python -m tools.release_status', not as a script"
            )
    return tuple(failures)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args(argv)
    workflows = arguments.root / ".github" / "workflows"
    failures = tuple(
        failure
        for path in sorted(workflows.glob("*.y*ml"))
        for failure in (
            *duplicate_top_level_keys(path),
            *unsafe_repo_tool_invocations(path),
        )
    )
    if failures:
        parser.error("\n".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
