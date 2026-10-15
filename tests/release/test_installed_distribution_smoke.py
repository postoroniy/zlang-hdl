# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
from __future__ import annotations

from pathlib import Path

import pytest

from tools.installed_distribution_smoke import (
    InstalledDistributionError,
    check_environment,
)


def _environment(tmp_path: Path, *, lock_name: str = "zlang lock") -> Path:
    environment = tmp_path / "environment"
    bin_dir = environment / "bin"
    bin_dir.mkdir(parents=True)
    programs = {
        "zlang": "zlang",
        "zlang-lock": lock_name,
        "zlang-verify": "zlang verify",
    }
    for program, display_name in programs.items():
        executable = bin_dir / program
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"DISPLAY = {display_name!r}\n"
            "if sys.argv[1:] == ['--version']:\n"
            "    print(f'{DISPLAY} 0.1.0a24')\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(0 if DISPLAY == 'zlang' and sys.argv[-1:] == ['--check'] else 2)\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
    return environment


def test_installed_distribution_smoke_accepts_canonical_cli_names(
    tmp_path: Path,
) -> None:
    source = tmp_path / "smoke.zhl"
    source.write_text("module Smoke { in x:u8 out y:u8 y=x }", encoding="utf-8")

    check_environment(_environment(tmp_path), version="0.1.0a24", source=source)


def test_installed_distribution_smoke_rejects_legacy_hyphenated_display_name(
    tmp_path: Path,
) -> None:
    source = tmp_path / "smoke.zhl"
    source.write_text("module Smoke { in x:u8 out y:u8 y=x }", encoding="utf-8")

    with pytest.raises(InstalledDistributionError, match="zlang-lock --version"):
        check_environment(
            _environment(tmp_path, lock_name="zlang-lock"),
            version="0.1.0a24",
            source=source,
        )


def test_release_workflow_delegates_installed_cli_contract() -> None:
    workflow = (Path(__file__).parents[2] / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )

    assert "python tools/installed_distribution_smoke.py" in workflow
    assert 'zlang-lock\" --version' not in workflow
    assert 'zlang-verify\" --version' not in workflow
