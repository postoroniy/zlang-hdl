from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tools" / "run_local_env.sh"
WORKFLOW_AUDIT = ROOT / "tools" / "audit_workflow_local_env.py"


def _run(*arguments: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        **kwargs,
    )


def test_env_check_requires_this_worktree_venv() -> None:
    completed = _run("make", "-s", "env-check")
    assert completed.returncode == 0, completed.stderr


def test_runner_uses_unique_worktree_local_scratch() -> None:
    command = (
        "import json, os, sys; "
        "print(json.dumps({'prefix': sys.prefix, 'tmp': os.environ['TMPDIR'], "
        "'tmp_alias': os.environ['TMP'], 'temp': os.environ['TEMP']}))"
    )
    first = _run(str(RUNNER), "--purpose", "environment-test", "--", sys.executable, "-c", command)
    second = _run(str(RUNNER), "--purpose", "environment-test", "--", sys.executable, "-c", command)
    assert first.returncode == second.returncode == 0
    first_values = json.loads(first.stdout)
    second_values = json.loads(second.stdout)
    for values in (first_values, second_values):
        scratch = Path(values["tmp"])
        assert values["prefix"] == str(ROOT / ".venv")
        assert values["tmp"] == values["tmp_alias"] == values["temp"]
        assert scratch.parent == ROOT / "build" / "tmp"
        assert not scratch.exists()
    assert first_values["tmp"] != second_values["tmp"]


def test_env_check_rejects_another_active_environment(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign-venv"
    foreign.mkdir()
    environment = os.environ | {"VIRTUAL_ENV": str(foreign)}
    completed = _run("make", "-s", "env-check", env=environment)
    assert completed.returncode == 2
    assert "active VIRTUAL_ENV is not this worktree" in completed.stderr


def test_env_check_reports_a_missing_local_venv(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text((ROOT / "Makefile").read_text(), encoding="utf-8")
    completed = subprocess.run(
        ("make", "-s", "env-check"),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "missing local virtual environment" in completed.stderr


def test_python_workflows_require_the_local_venv_hook() -> None:
    completed = _run(sys.executable, str(WORKFLOW_AUDIT), "--root", str(ROOT))
    assert completed.returncode == 0, completed.stderr
