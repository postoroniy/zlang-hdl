from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tomllib

import pytest
import yaml

from tools.audit_workflow_structure import codeql_version_failures, hosted_lane_contract_failures
from tools.release_status import StatusError, _check_solver_pin


ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"


def test_oss_cad_workflows_match_reviewed_suite_pin() -> None:
    status = json.loads((ROOT / "release/status.json").read_text(encoding="utf-8"))
    suite = status["oss_cad_suite"]
    assert len(suite["linux_x64_sha256"]) == 64
    assert all(c in "0123456789abcdef" for c in suite["linux_x64_sha256"])
    setups = 0
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        for job in workflow.get("jobs", {}).values():
            for step in job.get("steps", ()):
                if step.get("uses", "").startswith("YosysHQ/setup-oss-cad-suite@"):
                    assert step["with"]["version"] == suite["version"], path
                    setups += 1
    assert setups > 0


def _junit(path: Path, name: str) -> None:
    path.write_text(
        f'<testsuite tests="1"><testcase name="{name}"/></testsuite>',
        encoding="utf-8",
    )


def _status_root(path: Path) -> Path:
    release = path / "release"
    release.mkdir(parents=True)
    (release / "status.json").write_text(
        json.dumps(
            {
                "schema": 3,
                "validation": {
                    "minimum_tests_passed": 2,
                    "minimum_tests_collected": 2,
                    "maximum_tests_skipped": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_makefile_owns_every_hosted_lane_entry_point() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    for target in (
        "ci-bootstrap",
        "ci-fast-core",
        "ci-full-regression",
        "ci-performance-regression",
        "ci-hosted-edge",
        "ci-random-smoke",
        "ci-editor",
        "ci-test-floor",
        "ci-contract-smoke",
    ):
        assert f"{target}:" in text
    assert (
        "release-sanity: release-dependency-floor release-regressions "
        "community-pdf-check " in text
    )
    assert "ci-contract-smoke" in text
    full_regression = text.split("ci-full-regression:", 1)[1].split(
        "\nci-performance-regression:", 1
    )[0]
    assert "-m tools.release_status check-junit" in full_regression
    assert '--junit "$(CI_JUNIT)"' in full_regression
    release_workflow = (ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )
    assert '"$OSV_SCANNER_IMAGE" scan source --format=json --all-packages' in release_workflow
    assert "--sbom /scan/zlang-native-sim.cdx.json" in release_workflow
    assert "-L /scan/zlang-native-sim.cdx.json" not in release_workflow
    release_twice = text.split("test-release-twice:", 1)[1].split(
        "\neditor-test:", 1
    )[0]
    assert release_twice.count("-m 'not performance'") == 2
    assert release_twice.count("-m performance") == 2
    assert release_twice.count("--performance-junit") == 2


def test_dependency_pins_have_one_repository_owner() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    test_dependencies = set(project["project"]["optional-dependencies"]["test"])
    assert "build==1.6.1" in test_dependencies
    assert "PyYAML==6.0.3" in test_dependencies
    status = json.loads((ROOT / "release/status.json").read_text(encoding="utf-8"))
    _check_solver_pin(project, status["eda_toolchain"])

    makefile = MAKEFILE.read_text(encoding="utf-8")
    for pin in (
        "PIP_VERSION ?= 26.2.1",
        "SETUPTOOLS_VERSION ?= 84.2.1",
        "RUFF_VERSION ?= 0.12.12",
        "PIP_AUDIT_VERSION ?= 2.10.1",
        "TWINE_VERSION ?= 6.2.0",
        "WHEEL_VERSION ?= 0.46.3",
        "CYCLONEDX_BOM_VERSION ?= 7.0.0",
    ):
        assert pin in makefile

    migrated = "\n".join(
        (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        for name in ("ci.yml", "daily-regression.yml", "eda.yml", "release.yml")
    )
    for obsolete_or_duplicated in (
        "build==",
        "z3-solver==",
        "ruff==",
        "setuptools==",
        "twine==",
        "wheel==",
    ):
        assert obsolete_or_duplicated not in migrated


@pytest.mark.parametrize("dependencies", (
    ["z3-solver==5.1.0.0"], [], ["z3-solver>=5.3.1"],
    ["z3-solver==5.3.1.0", "z3-solver==5.3.1.1"],
))
def test_solver_pin_rejects_stale_missing_ranged_or_duplicate_dependencies(dependencies) -> None:
    project = {"project": {"optional-dependencies": {"test": dependencies}}}
    with pytest.raises(StatusError, match="z3-solver pin must match"):
        _check_solver_pin(project, {"z3": "5.3.1"})


def test_codeql_actions_use_one_reviewed_revision() -> None:
    assert not codeql_version_failures(ROOT / ".github/workflows/codeql.yml")


@pytest.mark.parametrize("analyze_ref", ("b" * 40, "v4", ""))
def test_workflow_audit_rejects_mixed_or_unpinned_codeql(tmp_path, analyze_ref) -> None:
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    path = workflows / "codeql.yml"
    path.write_text(
        "jobs:\n  analyze:\n    steps:\n"
        f"      - uses: github/codeql-action/init@{'a' * 40}\n"
        f"      - uses: github/codeql-action/analyze@{analyze_ref}\n",
        encoding="utf-8",
    )
    assert codeql_version_failures(path)
    completed = subprocess.run(
        ("python", "-m", "tools.audit_workflow_structure", "--root", str(tmp_path)),
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert completed.returncode != 0
    assert "CodeQL actions" in completed.stderr


def test_dependabot_groups_codeql_and_defers_incompatible_compiler_api() -> None:
    import yaml

    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    updates = {item["package-ecosystem"]: item for item in config["updates"]}
    assert "github/codeql-action/*" in updates["github-actions"]["groups"]["codeql"]["patterns"]
    assert {"dependency-name": "typescript", "update-types": ["version-update:semver-major"]} in updates["npm"]["ignore"]


def test_bootstrap_isolates_external_release_tool_builds() -> None:
    completed = subprocess.run(
        ("make", "-n", "venv", "native-release-install"),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    # The bootstrap runner is deliberately opaque to make -n: inspecting it
    # must not execute provisioning or recursive Make. Check both pip and uv.
    bootstrap = (ROOT / "tools/bootstrap_local_env.sh").read_text()
    pip_commands = [
        line.strip()
        for line in (bootstrap + "\n" + completed.stdout).splitlines()
        if " -m pip install " in line
    ]
    # The bootstrap owns three package-index installs.  A fourth command comes
    # from the deliberately recursive native-release-install target, which
    # installs the already reviewed local wheel without resolving dependencies.
    assert len(pip_commands) == 4
    editable = next(line for line in pip_commands if " -e '.[test]'" in line)
    release_tools = next(
        line for line in pip_commands if '"$@"' in line
    )
    native_runtime = next(
        line for line in pip_commands if 'purpose "native-release-install"' in line
    )
    assert "--no-build-isolation" in editable
    assert "--no-build-isolation" not in release_tools
    assert "reuse==5.1.1" in completed.stdout
    uv_editable = next(line for line in bootstrap.splitlines() if "uv pip" in line and " -e '.[test]'" in line)
    assert "--no-build-isolation" in uv_editable
    assert "--force-reinstall --no-deps" in completed.stdout
    assert '"${wheels[0]}"' in completed.stdout
    assert "native-release-install" in native_runtime


def test_split_suite_floor_runs_without_site_packages(tmp_path: Path) -> None:
    status_root = _status_root(tmp_path / "status-root")
    deterministic = tmp_path / "deterministic.xml"
    performance = tmp_path / "performance.xml"
    deterministic.write_text(
        '<testsuite tests="2"><testcase name="deterministic-a"/>'
        '<testcase name="deterministic-b"/></testsuite>',
        encoding="utf-8",
    )
    _junit(performance, "performance")

    completed = subprocess.run(
        (
            "make",
            "-s",
            "ci-test-floor",
            f"CI_STATUS_ROOT={status_root}",
            f"CI_DETERMINISTIC_JUNIT={deterministic}",
            f"CI_PERFORMANCE_JUNIT={performance}",
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "release test reports valid"


def test_random_lane_preserves_command_option_prefixes() -> None:
    completed = subprocess.run(
        (
            "make",
            "-n",
            "ci-random-smoke",
            "CI_RANDOM_SUITE=expressions",
            "CI_RANDOM_MASTER_SEED=contract",
            "CI_RANDOM_TESTS=1",
        ),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert '"--suite" "expressions"' in completed.stdout
    assert '"--master-seed" "contract"' in completed.stdout
    assert '"--tests" "1"' in completed.stdout


@pytest.mark.parametrize("missing", ("deterministic", "performance"))
def test_split_suite_floor_requires_both_reports(
    tmp_path: Path, missing: str
) -> None:
    deterministic = "" if missing == "deterministic" else str(tmp_path / "d.xml")
    performance = "" if missing == "performance" else str(tmp_path / "p.xml")
    completed = subprocess.run(
        (
            "make",
            "-s",
            "ci-test-floor",
            f"CI_DETERMINISTIC_JUNIT={deterministic}",
            f"CI_PERFORMANCE_JUNIT={performance}",
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "requires CI_DETERMINISTIC_JUNIT and CI_PERFORMANCE_JUNIT" in completed.stderr


def test_workflow_contract_rejects_direct_hosted_commands(tmp_path: Path) -> None:
    path = tmp_path / "ci.yml"
    path.write_text(
        "jobs:\n"
        "  fast-core:\n"
        "    steps:\n"
        "      - run: make -s ci-bootstrap\n"
        "      - run: python -m pytest -q\n",
        encoding="utf-8",
    )

    failures = hosted_lane_contract_failures(path)

    assert any("must invoke Make target 'native-release-install'" in item for item in failures)
    assert any("must invoke Make target 'ci-fast-core'" in item for item in failures)
    assert any("must not bypass its Make lane" in item for item in failures)


def test_editor_workflow_contract_requires_native_runtime_install(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ci.yml"
    path.write_text(
        "jobs:\n"
        "  lexical-editor:\n"
        "    steps:\n"
        "      - run: make -s ci-bootstrap\n"
        "      - run: make -s ci-editor\n",
        encoding="utf-8",
    )

    failures = hosted_lane_contract_failures(path)

    assert any(
        "job 'lexical-editor' must invoke Make target 'native-release-install'"
        in item
        for item in failures
    )


def test_workflow_contract_requires_pdf_tools_for_status_lanes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ci.yml"
    path.write_text(
        "jobs:\n"
        "  fast-core:\n"
        "    steps:\n"
        "      - run: make -s ci-bootstrap\n"
        "      - run: make -s native-release-install\n"
        "      - run: make -s ci-fast-core\n",
        encoding="utf-8",
    )

    failures = hosted_lane_contract_failures(path)

    assert any(
        "job 'fast-core' must provision PDF inspection" in item
        for item in failures
    )

