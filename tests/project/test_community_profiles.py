# SPDX-License-Identifier: Apache-2.0
"""Community rejects unavailable product profiles before any tooling import."""

import subprocess
import sys

import pytest

from zlang.workspace import update_project_lock


@pytest.mark.parametrize("key", ["dft", "hardware-debug"])
def test_unavailable_profile_is_a_source_diagnostic(tmp_path, key):
    source = tmp_path / "src/top.zhl"
    source.parent.mkdir()
    source.write_text("module Top { in x:bit out y:bit y=x }")
    manifest = tmp_path / "zlang.toml"
    manifest.write_text(
        'schema=1\n[project]\nname="surface"\nversion="1"\nsource-root="src"\n[profiles.unavailable]\nbackend="systemverilog"\n[profiles.unavailable.'
        + key
        + "]\n"
    )
    update_project_lock(manifest)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "zlang.cli",
            str(source),
            "--check",
            "--project",
            str(manifest),
            "--profile",
            "unavailable",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "error[ZL-IMPL-001]" in result.stderr
    assert f"unknown implementation profile key '{key}'" in result.stderr
    assert "Traceback" not in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
