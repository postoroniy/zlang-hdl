from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from tools.check_local_environment import _stale_checkout_metadata


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tools" / "run_local_env.sh"
WORKFLOW_AUDIT = ROOT / "tools" / "audit_workflow_local_env.py"
WORKFLOW_STRUCTURE_AUDIT = ROOT / "tools" / "audit_workflow_structure.py"


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


def test_env_check_rejects_stale_checkout_distribution_metadata(
    tmp_path: Path,
) -> None:
    metadata = tmp_path / "zlang_hdl.egg-info"
    metadata.mkdir()
    (metadata / "PKG-INFO").write_text(
        "Metadata-Version: 2.4\nName: zlang-hdl\nVersion: 0.1.0a1\n",
        encoding="utf-8",
    )
    assert _stale_checkout_metadata(tmp_path) == (metadata,)
    completed = subprocess.run(
        (
            sys.executable,
            str(ROOT / "tools" / "check_local_environment.py"),
            "--root",
            str(tmp_path),
            "--venv",
            str(ROOT / ".venv"),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "stale checkout distribution metadata" in completed.stderr
    assert "run 'make venv' to quarantine it below build/tmp" in completed.stderr


def test_venv_bootstrap_quarantines_stale_checkout_distribution_metadata(
    tmp_path: Path,
) -> None:
    metadata = tmp_path / "zlang_hdl.egg-info"
    metadata.mkdir()
    package_info = metadata / "PKG-INFO"
    package_info.write_text(
        "Metadata-Version: 2.4\nName: zlang-hdl\nVersion: 0.1.0a1\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "tools.check_local_environment",
            "--root",
            str(tmp_path),
            "--quarantine-stale-metadata",
            "--tmp-root",
            str(tmp_path / "build" / "tmp"),
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    quarantines = tuple(
        (tmp_path / "build" / "tmp").glob("stale-checkout-metadata.*")
    )
    assert len(quarantines) == 1
    quarantine = quarantines[0]
    assert quarantine.parent == tmp_path / "build" / "tmp"
    assert str(quarantine) in completed.stderr
    assert not metadata.exists()
    preserved = quarantine / "zlang_hdl.egg-info" / "PKG-INFO"
    assert preserved.read_text(encoding="utf-8") == (
        "Metadata-Version: 2.4\nName: zlang-hdl\nVersion: 0.1.0a1\n"
    )
    assert _stale_checkout_metadata(tmp_path) == ()


def test_python_workflows_require_the_local_venv_hook() -> None:
    completed = _run(sys.executable, str(WORKFLOW_AUDIT), "--root", str(ROOT))
    assert completed.returncode == 0, completed.stderr


def test_workflows_have_unique_top_level_keys() -> None:
    completed = _run(
        sys.executable,
        str(WORKFLOW_STRUCTURE_AUDIT),
        "--root",
        str(ROOT),
    )
    assert completed.returncode == 0, completed.stderr


def test_workflow_structure_audit_rejects_duplicate_top_level_key(
    tmp_path: Path,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    workflow = workflows / "release.yml"
    workflow.write_text(
        "name: Release\nenv:\n  RELEASE_TAG: candidate\njobs: {}\nenv:\n"
        "  BASH_ENV: local-env.sh\n",
        encoding="utf-8",
    )

    completed = _run(
        sys.executable,
        str(WORKFLOW_STRUCTURE_AUDIT),
        "--root",
        str(tmp_path),
    )

    assert completed.returncode == 2
    assert "duplicate top-level key 'env'" in completed.stderr
    assert "first declared on line 2" in completed.stderr


def test_workflow_structure_audit_rejects_direct_release_status_script(
    tmp_path: Path,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "release.yml").write_text(
        "name: Release\njobs:\n  validate:\n    steps:\n"
        "      - run: python tools/release_status.py check --root .\n",
        encoding="utf-8",
    )

    completed = _run(
        sys.executable,
        str(WORKFLOW_STRUCTURE_AUDIT),
        "--root",
        str(tmp_path),
    )

    assert completed.returncode == 2
    assert "python -m tools.release_status" in completed.stderr
