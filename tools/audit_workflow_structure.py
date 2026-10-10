# SPDX-License-Identifier: Apache-2.0
"""Reject GitHub workflow structures that bypass repository-owned CI lanes.

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

import yaml


_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s|$)")
_DIRECT_RELEASE_STATUS = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:python|python3)\s+tools/release_status\.py(?:\s|$)"
)
_DIRECT_LANE_COMMANDS = (
    re.compile(r"(?:^|\n)\s*(?:python|python3)\s+-m\s+pytest(?:\s|$)"),
    re.compile(r"(?:^|\n)\s*(?:python|python3)\s+-m\s+tools\.release_status(?:\s|$)"),
    re.compile(r"(?:^|\n)\s*(?:python|python3)\s+-m\s+pip\s+install(?:\s|$)"),
    re.compile(r"(?:^|\n)\s*(?:pip|pytest)(?:\s|$)"),
)

# GitHub owns hosts, credentials and artifact transport.  These job contracts
# ensure repository commands stay executable locally through the same Make
# entry points used by hosted lanes.
_JOB_TARGETS: dict[str, dict[str, tuple[str, ...]]] = {
    "ci.yml": {
        "fast-core": ("ci-bootstrap", "native-release-install", "ci-fast-core"),
        "lexical-editor": ("ci-bootstrap", "ci-editor"),
        "random-smoke": ("ci-bootstrap", "ci-random-smoke"),
        "full-regression": (
            "ci-bootstrap",
            "native-release-install",
            "ci-full-regression",
        ),
        "performance-regression": (
            "ci-bootstrap",
            "native-release-install",
            "ci-performance-regression",
        ),
        "hosted-edge-regression": ("ci-bootstrap", "ci-hosted-edge"),
        "fast": ("ci-test-floor",),
    },
    "daily-regression.yml": {
        "deterministic": (
            "ci-bootstrap",
            "native-release-install",
            "ci-full-regression",
            "ci-performance-regression",
            "ci-test-floor",
        ),
        "random": ("ci-bootstrap", "ci-random-smoke"),
    },
    "eda.yml": {
        "real-tools": (
            "ci-bootstrap",
            "native-release-install",
            "ci-full-regression",
            "ci-performance-regression",
            "ci-test-floor",
        ),
    },
    "release.yml": {
        "validate": (
            "ci-bootstrap",
            "release-preflight",
            "public-check",
            "package",
            "ci-editor",
        ),
        "eda": (
            "ci-bootstrap",
            "native-release-install",
            "ci-full-regression",
            "ci-performance-regression",
            "ci-test-floor",
        ),
    },
}


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


def _workflow_jobs(path: Path) -> dict[str, object]:
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid workflow YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{path}: workflow must be a mapping")
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        raise ValueError(f"{path}: workflow must contain a jobs mapping")
    return jobs


def _job_run_commands(job: object) -> tuple[str, ...]:
    if not isinstance(job, dict):
        return ()
    steps = job.get("steps")
    if not isinstance(steps, list):
        return ()
    return tuple(
        command
        for step in steps
        if isinstance(step, dict)
        and isinstance((command := step.get("run")), str)
    )


def hosted_lane_contract_failures(path: Path) -> tuple[str, ...]:
    contracts = _JOB_TARGETS.get(path.name)
    if contracts is None:
        return ()
    try:
        jobs = _workflow_jobs(path)
    except ValueError as exc:
        return (str(exc),)
    failures: list[str] = []
    for job_name, targets in contracts.items():
        commands = _job_run_commands(jobs.get(job_name))
        combined = "\n".join(commands)
        if not commands:
            failures.append(f"{path}: job {job_name!r} has no executable run steps")
            continue
        for target in targets:
            if re.search(rf"\bmake\s+(?:-s\s+)?{re.escape(target)}(?:\s|$)", combined) is None:
                failures.append(
                    f"{path}: job {job_name!r} must invoke Make target {target!r}"
                )
        for pattern in _DIRECT_LANE_COMMANDS:
            if pattern.search(combined):
                failures.append(
                    f"{path}: job {job_name!r} must not bypass its Make lane "
                    f"with direct pip/pytest/release-status commands"
                )
                break
    return tuple(failures)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args(argv)
    workflows = arguments.root / ".github" / "workflows"
    failures: tuple[str, ...] = ()
    for path in sorted(workflows.glob("*.y*ml")):
        duplicate_failures = duplicate_top_level_keys(path)
        failures += duplicate_failures
        if duplicate_failures:
            continue
        failures += unsafe_repo_tool_invocations(path)
        failures += hosted_lane_contract_failures(path)
    if failures:
        parser.error("\n".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
