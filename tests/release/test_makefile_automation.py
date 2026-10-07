from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"


def test_makefile_exposes_bounded_test_and_release_entry_points() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert ".ONESHELL:" in text
    assert ".SHELLFLAGS := -eu -o pipefail -c" in text
    assert "VENV ?= $(CURDIR)/.venv" in text
    assert "PYTHON := $(VENV)/bin/python" in text
    assert "env-check:" in text
    assert "make venv" in text
    assert "release-review-clean:" in text
    assert (
        "release-review: release-review-clean release-regressions "
        "community-pdf-check public-check static test-release-twice" in text
    )
    assert "release review requires a clean committed public checkout" in text
    assert "tools/run_local_env.sh" in text
    assert "${TMPDIR:-/tmp}" not in text
    assert "release-preflight: release-regressions" in text
    assert "tools/release_regressions.py" in text
    assert (
        "release-candidate: release-preflight native-release-install community-pdf-check public-check static jit-check jit-audit "
        "jit-advisory-audit audit release-tools test-release-twice" in text
    )
    assert "tools/release_preflight.py" in text
    assert 'PYTHONPATH="$(CURDIR)" $(call RUN_PYTHON,release-preflight) tools/release_preflight.py' in text
    assert "--mode candidate" in text
    assert "--require-clean" in text
    assert "community-pdf:" not in text
    assert "tools/build_community_pdf.py" not in text
    assert (
        'PYTHONPATH="$(CURDIR)" $(call RUN_PYTHON,community-pdf) tools/release_status.py check '
        '--root . --tag "$(TAG)"'
    ) in text
    assert "-p tools.pytest_no_skips" in text
    assert text.count('--junitxml="$$report_root/release-') == 2
    assert "test \"$${#first_wheels[@]}\" -eq 1" in text
    assert "cmp -- \"$${first_wheels[0]}\" \"$${second_wheels[0]}\"" in text
    assert "public package requires a clean committed checkout" in text
    assert "Python wheel/sdist version does not match the release tag" in text
    assert "tools/materialize_native_test_extension.py" in text
    assert "tests/editor/test_vscode_package.py" in text
    assert "npm --prefix editors/vscode/zlang-hdl run test:host" in text
    assert (
        'PYTHONPATH="$(CURDIR)" xvfb-run -a npm '
        '--prefix editors/vscode/zlang-hdl run test:host'
    ) in text
    assert "--compatibility off" in text
    assert "ZLANG_NATIVE_RUNTIME_WHEEL" in text
    assert '-m reuse --root "$$public_root" lint' in text
    assert '-m pip_audit "$$public_root" --progress-spinner off' in text
    assert '-m reuse --root . lint' in text
    assert '-m pip_audit . --progress-spinner off' in text
    assert "--check-tools" in text
    assert "PYTHON_SCRIPTS := $(VENV)/bin" in text
    assert 'export PATH="$(PYTHON_SCRIPTS):$$PATH"' in text
    assert 'export PATH="$$(dirname "$$python_bin"):$$PATH"' in text
    assert text.count("tools/public_tree.py export") == 5
    assert 'check-export --source "$$public_root"' in text
    assert 'PYTHONPATH="$(CURDIR)" $(call RUN_PYTHON,public-check) tools/public_tree.py export' in text
    assert text.count("$(call RUN_PYTHON") >= 15
    assert "git archive --format=tar HEAD" in text
    assert '-m build "$$source_root" --no-isolation' in text
    assert 'cd "$$public_root"' in text
    assert 'export PYTHONPATH="$$public_root"' in text
    assert 'python_bin="$$(cd "$$(dirname "$$python_bin")" && pwd)' in text
    assert "realpath" not in text
    assert "gh release create" not in text
    assert "git tag" not in text
    assert "git push" not in text


def test_release_workflow_uses_curated_changelog_notes_and_native_set() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert "tools/release_notes.py" in workflow
    assert '--notes-file "$RUNNER_TEMP/release-notes.md"' in workflow
    assert "--generate-notes" not in workflow
    assert "--require-release-platforms" in workflow
    assert "zlang_native_sim-*.whl" in workflow
    assert "tools/audit_native_vulnerabilities.py" in workflow
    assert "tools/stage_release_pdf.py" in workflow
    assert "BASH_ENV: ${{ github.workspace }}/.github/workflows/local-venv-env.sh" in workflow
    assert "workflow_dispatch:" in workflow
    assert "tools/release_preflight.py" in workflow
    assert '--mode "$mode"' in workflow
    assert "release-preflight.json" in workflow
    assert "if: ${{ github.event_name == 'push' }}\n    needs: [validate, eda]" in workflow
    assert "Exercise the installed exact-tag VSIX" in workflow
    assert "zlang-hdl-*-language-reference.pdf" in workflow
    assert workflow.count("--maxfail=1") == 4
    assert 'printf \'%s\\n\' "$python_scripts" >> "$GITHUB_PATH"' in workflow
    assert 'export PATH="$python_scripts:$PATH"' in workflow
    assert workflow.index('export PATH="$python_scripts:$PATH"') < workflow.index(
        "python tools/release_status.py check --root . --check-tools"
    )


def test_makefile_help_is_executable_and_documents_nonpublishing_gate() -> None:
    completed = subprocess.run(
        ("make", "-s", "help"),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "make test-release-twice" in completed.stdout
    assert "make release-review" in completed.stdout
    assert "make release-regressions" in completed.stdout
    assert "make release-preflight" in completed.stdout
    assert "make community-pdf-check" in completed.stdout
    assert "make audit" in completed.stdout
    assert "make jit-check" in completed.stdout
    assert "make jit-audit" in completed.stdout
    assert "make jit-advisory-audit" in completed.stdout
    assert "make native-release-set" in completed.stdout
    assert "make native-release-install" in completed.stdout
    assert "make release-tools" in completed.stdout
    assert "make release-candidate" in completed.stdout
    assert "make editor-host-test" in completed.stdout
    assert "non-publishing" in completed.stdout


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


@pytest.mark.parametrize("change", ("unstaged", "staged", "untracked"))
def test_package_rejects_an_uncommitted_public_checkout(
    tmp_path: Path, change: str
) -> None:
    (tmp_path / "Makefile").write_text(MAKEFILE.read_text(encoding="utf-8"), encoding="utf-8")
    source = tmp_path / "README.md"
    source.write_text("committed\n", encoding="utf-8")
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    subprocess.run(("git", "add", "Makefile", "README.md"), cwd=tmp_path, check=True)
    subprocess.run(
        (
            "git", "-c", "commit.gpgsign=false", "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid", "commit", "-qm", "baseline",
        ),
        cwd=tmp_path,
        check=True,
    )
    if change == "untracked":
        (tmp_path / "new.txt").write_text("untracked\n", encoding="utf-8")
    else:
        source.write_text("changed\n", encoding="utf-8")
        if change == "staged":
            subprocess.run(("git", "add", "README.md"), cwd=tmp_path, check=True)

    completed = subprocess.run(
        (
            "make", "-s", "package", f"PYTHON={sys.executable}",
            "TAG=v0.1.0a16", f"BUILD_ROOT={tmp_path / 'dist'}",
        ),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "public package requires a clean committed checkout" in completed.stderr
    assert not (tmp_path / "dist").exists()
