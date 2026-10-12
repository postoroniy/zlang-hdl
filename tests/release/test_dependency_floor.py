# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from tools.audit_dependency_floor import (
    DependencyFloorError,
    validate_dependency_floor,
)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


def _write_surface(root: Path, *, egglog: str, npm: str, action: str) -> None:
    (root / "pyproject.toml").write_text(
        "[project]\nname='floor'\nversion='1.0.0'\n"
        f"dependencies=['egglog=={egglog}']\n",
        encoding="utf-8",
    )
    editor = root / "editors/vscode/zlang-hdl"
    editor.mkdir(parents=True, exist_ok=True)
    (editor / "package-lock.json").write_text(
        json.dumps({
            "packages": {
                "": {"version": "1.0.0"},
                "node_modules/vscode-languageclient": {"version": npm},
            }
        }),
        encoding="utf-8",
    )
    workflows = root / ".github/workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "codeql.yml").write_text(
        "steps:\n  - uses: github/codeql-action/init@"
        f"{'a' * 40} # v{action}\n",
        encoding="utf-8",
    )
    (root / "Makefile").write_text("RUFF_VERSION ?= 0.12.12\n", encoding="utf-8")
    release = root / "release"
    release.mkdir(parents=True, exist_ok=True)
    (release / "status.json").write_text(
        json.dumps({"eda_toolchain": {"yosys": "0.69"}}),
        encoding="utf-8",
    )


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _write_surface(root, egglog="14.0.0", npm="10.1.2", action="4.38.2")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Dependency Floor Test")
    _git(root, "config", "user.email", "floor@example.invalid")
    _git(root, "add", "-A")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "baseline")
    _git(root, "tag", "baseline")
    return root


def test_dependency_floor_accepts_equal_or_newer_versions(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _write_surface(root, egglog="14.0.0", npm="10.1.3", action="4.38.3")

    report = validate_dependency_floor(root, previous_ref="baseline")

    assert report["downgrades"] == []
    assert report["compared"] == 5


@pytest.mark.parametrize(
    ("egglog", "npm", "action", "expected"),
    (
        ("13.2.0", "10.1.2", "4.38.2", "python:egglog: 14.0.0 -> 13.2.0"),
        ("14.0.0", "10.1.1", "4.38.2", "npm:vscode-languageclient"),
        ("14.0.0", "10.1.2", "4.38.1", "action:github/codeql-action/init"),
    ),
)
def test_dependency_floor_rejects_release_candidate_downgrades(
    tmp_path: Path,
    egglog: str,
    npm: str,
    action: str,
    expected: str,
) -> None:
    root = _repository(tmp_path)
    _write_surface(root, egglog=egglog, npm=npm, action=action)

    with pytest.raises(DependencyFloorError, match=expected):
        validate_dependency_floor(root, previous_ref="baseline")
