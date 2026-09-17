from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"


def test_makefile_exposes_bounded_test_and_release_entry_points() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert ".ONESHELL:" in text
    assert ".SHELLFLAGS := -eu -o pipefail -c" in text
    assert (
        "release-candidate: source-check static audit release-tools "
        "test-release-twice" in text
    )
    assert "-p tools.pytest_no_skips" in text
    assert text.count('--junitxml="$$report_root/release-') == 2
    assert "test \"$${#first_wheels[@]}\" -eq 1" in text
    assert "cmp -- \"$${first_wheels[0]}\" \"$${second_wheels[0]}\"" in text
    assert "-m reuse --root . lint" in text
    assert "-m pip_audit . --progress-spinner off" in text
    assert "--check-tools" in text
    assert "check-export" not in text
    assert "git archive --format=tar HEAD" in text
    assert '-m build "$$source_root" --no-isolation' in text
    assert 'python_bin="$$(cd "$$(dirname "$$python_bin")" && pwd)' in text
    assert "realpath" not in text
    assert "gh release create" not in text
    assert "git tag" not in text
    assert "git push" not in text


def test_makefile_help_is_executable_and_documents_nonpublishing_gate() -> None:
    completed = subprocess.run(
        ("make", "-s", "help"),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "make test-release-twice" in completed.stdout
    assert "make source-check" in completed.stdout
    assert "make audit" in completed.stdout
    assert "make release-tools" in completed.stdout
    assert "make release-candidate" in completed.stdout
    assert "non-publishing" in completed.stdout


def test_release_workflow_uses_curated_changelog_notes() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert "tools/release_notes.py" in workflow
    assert '--notes-file "$RUNNER_TEMP/release-notes.md"' in workflow
    assert "--generate-notes" not in workflow


def test_package_target_rejects_a_stale_output_directory(tmp_path: Path) -> None:
    completed = subprocess.run(
        (
            "make",
            "-s",
            "package",
            f"BUILD_ROOT={tmp_path}",
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "already exists; choose a fresh BUILD_ROOT" in completed.stderr
