#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Validate the supported CLI surface of an installed ZLang distribution."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess


CLI_VERSION_CONTRACT = (
    ("zlang", "zlang"),
    ("zlang-lock", "zlang lock"),
    ("zlang-verify", "zlang verify"),
)


class InstalledDistributionError(RuntimeError):
    """Raised when an installed release artifact violates its CLI contract."""


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONNOUSERSITE": "1"}
    return subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def check_environment(environment: Path, *, version: str, source: Path) -> None:
    """Check CLI identities and one compiler invocation in ``environment``."""
    bin_dir = environment.resolve() / "bin"
    for program, display_name in CLI_VERSION_CONTRACT:
        executable = bin_dir / program
        if not executable.is_file():
            raise InstalledDistributionError(
                f"installed distribution is missing executable: {executable}"
            )
        result = _run([str(executable), "--version"], cwd=source.parent)
        expected = f"{display_name} {version}\n"
        if result.returncode != 0 or result.stderr or result.stdout != expected:
            raise InstalledDistributionError(
                f"{program} --version violated the installed CLI contract: "
                f"expected stdout {expected!r}, got stdout {result.stdout!r}, "
                f"stderr {result.stderr!r}, exit {result.returncode}"
            )

    result = _run([str(bin_dir / "zlang"), source.name, "--check"], cwd=source.parent)
    if result.returncode != 0:
        raise InstalledDistributionError(
            "installed zlang compile smoke failed: "
            f"stdout {result.stdout!r}, stderr {result.stderr!r}, "
            f"exit {result.returncode}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", action="append", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source", type=Path, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    source = arguments.source.resolve()
    if not source.is_file():
        raise SystemExit(f"installed distribution smoke source is missing: {source}")
    try:
        for environment in arguments.environment:
            check_environment(environment, version=arguments.version, source=source)
    except InstalledDistributionError as exc:
        raise SystemExit(f"installed-distribution-smoke: error: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
