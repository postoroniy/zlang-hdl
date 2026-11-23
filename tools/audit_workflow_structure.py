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
        "lexical-editor": ("ci-bootstrap", "native-release-install", "ci-editor"),
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
            "native-release-install",
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

# These lanes execute the fail-closed Community ancestry gate before testing.
_HISTORY_REQUIRED_TARGETS = {"ci-fast-core", "ci-full-regression", "ci-performance-regression"}

_PDF_TOOL_ACTION = "./.github/actions/setup-pdf-tools"
_PDF_TOOL_JOBS: dict[str, tuple[str, ...]] = {
    "ci.yml": ("fast-core", "full-regression", "performance-regression"),
    "daily-regression.yml": ("deterministic",),
    "eda.yml": ("real-tools",),
    "release.yml": ("validate", "eda"),
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


def _job_actions(job: object) -> tuple[str, ...]:
    if not isinstance(job, dict):
        return ()
    steps = job.get("steps")
    if not isinstance(steps, list):
        return ()
    return tuple(
        action
        for step in steps
        if isinstance(step, dict)
        and isinstance((action := step.get("uses")), str)
    )


def codeql_version_failures(path: Path) -> tuple[str, ...]:
    """CodeQL init/analyze/upload steps share versioned configuration per job."""
    try:
        jobs = _workflow_jobs(path)
    except ValueError as exc:
        return (str(exc),)
    failures: list[str] = []
    for name, job in jobs.items():
        actions = tuple(
            action for action in _job_actions(job)
            if action.startswith("github/codeql-action/")
        )
        refs = {action.partition("@")[2] for action in actions}
        if any(re.fullmatch(r"[0-9a-f]{40}", ref) is None for ref in refs):
            failures.append(f"{path}: job {name!r}: CodeQL actions require full commit pins")
        if len(refs) > 1:
            failures.append(f"{path}: job {name!r}: all CodeQL actions must use the same revision")
    return tuple(failures)


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
        job = jobs.get(job_name)
        commands = _job_run_commands(job)
        combined = "\n".join(commands)
        if not commands:
            failures.append(f"{path}: job {job_name!r} has no executable run steps")
            continue
        if _HISTORY_REQUIRED_TARGETS.intersection(targets):
            checkouts = [step for step in job.get("steps", ())
                         if isinstance(step, dict)
                         and str(step.get("uses", "")).startswith("actions/checkout@")]
            if (len(checkouts) != 1
                    or not isinstance(checkouts[0].get("with"), dict)
                    or str(checkouts[0]["with"].get("fetch-depth")) != "0"):
                failures.append(
                    f"{path}: job {job_name!r} requires complete Git history "
                    "(checkout fetch-depth: 0) for Community ancestry validation"
                )
        if "ci-full-regression" in targets:
            # Match the qualified full CI lane. Unset WORKERS inherits the
            # local Make default, which oversubscribes hosted EDA runners.
            for command in commands:
                for line in command.replace("\\\n", " ").splitlines():
                    if re.search(r"\bmake\s+(?:-s\s+)?ci-full-regression(?:\s|$)", line):
                        if re.findall(r"(?:^|\s)WORKERS=(\S+)", line) != ["4"]:
                            failures.append(
                                f"{path}: job {job_name!r} requires explicit WORKERS=4 "
                                "for each complete regression; LSP deadlines stay unchanged"
                            )
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
        if (
            job_name in _PDF_TOOL_JOBS.get(path.name, ())
            and _PDF_TOOL_ACTION not in _job_actions(job)
        ):
            failures.append(
                f"{path}: job {job_name!r} must provision PDF inspection "
                f"through {_PDF_TOOL_ACTION!r}"
            )
    return tuple(failures)



_PROTECTED_CONTEXT_WORKFLOWS = {"codeql.yml", "dependency-review.yml", "dco.yml"}
_PR_OR_PUSH_BASE = "${{ github.event.pull_request.base.sha || github.event.before }}"
_PR_OR_PUSH_HEAD = "${{ github.event.pull_request.head.sha || github.sha }}"


def _unconditional_steps(job: dict[str, object]) -> bool:
    """A required producer cannot skip or suppress a failed executable step."""
    steps = job.get("steps")
    return (
        job.get("continue-on-error", "false") == "false"
        and isinstance(steps, list)
        and bool(steps)
        and all(isinstance(step, dict) and "if" not in step
                and step.get("continue-on-error", "false") == "false" for step in steps)
    )


def _pinned_action_steps(job: dict[str, object], action: str) -> tuple[dict, ...]:
    return tuple(step for step in job.get("steps", ()) if isinstance(step, dict)
                 and re.fullmatch(re.escape(action) + r"@[0-9a-f]{40}",
                                  str(step.get("uses", ""))))


def protected_context_contract_failures(path: Path) -> tuple[str, ...]:
    """Keep the three protected-main contexts backed by real fail-closed jobs."""
    if path.name not in _PROTECTED_CONTEXT_WORKFLOWS:
        return ()
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        jobs = _workflow_jobs(path)
    except (ValueError, yaml.YAMLError) as exc:
        return (str(exc),)
    failures: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            failures.append(f"{path}: protected context: {message}")

    events = document.get("on", {})
    push = events.get("push", {}) if isinstance(events, dict) else {}
    require(isinstance(events, dict) and "pull_request" in events
            and events["pull_request"] in (None, "", {}),
            "unfiltered pull_request trigger is required")
    require(isinstance(push, dict) and push.get("branches") == ["main"]
            and set(push) == {"branches"}, "unfiltered push to main is required")

    if path.name == "codeql.yml":
        analyze = jobs.get("analyze", {})
        alias = jobs.get("codeql-status", {})
        if not isinstance(analyze, dict) or not isinstance(alias, dict):
            return (*failures, f"{path}: protected context: CodeQL jobs must be mappings")
        require("if" not in analyze and _unconditional_steps(analyze),
                "CodeQL analyze must execute without skips or failure suppression")
        for action in ("github/codeql-action/init", "github/codeql-action/analyze"):
            require(len(_pinned_action_steps(analyze, action)) == 1,
                    f"CodeQL analyze must execute one pinned {action} action")
        require(alias.get("name") == "CodeQL" and alias.get("needs") == ["analyze"]
                and alias.get("if") == "${{ always() }}", "CodeQL alias must always require analyze")
        require(alias.get("permissions", document.get("permissions")) == {"contents": "read"},
                "CodeQL alias permissions must be read-only")
        steps = alias.get("steps", [])
        require(_unconditional_steps(alias) and len(steps) == 1
                and steps[0].get("env") == {"ANALYZE_RESULT": "${{ needs.analyze.result }}"}
                and steps[0].get("run") == 'test "$ANALYZE_RESULT" = success',
                "CodeQL alias must reject failure, cancellation and skipped analysis")
        return tuple(failures)

    job = jobs.get("review" if path.name == "dependency-review.yml" else "dco", {})
    if not isinstance(job, dict):
        return (*failures, f"{path}: protected context: producer job must be a mapping")
    require("if" not in job and _unconditional_steps(job),
            "producer must execute without skips or failure suppression")
    permissions = {"contents": "read"}
    if path.name == "dco.yml":
        permissions.update({"checks": "read", "pull-requests": "read"})
    require(job.get("permissions", document.get("permissions")) == permissions,
            f"producer permissions must be exactly {permissions}")
    if path.name == "dependency-review.yml":
        actions = _pinned_action_steps(job, "actions/dependency-review-action")
        require(len(actions) == 1 and actions[0].get("with") == {
            "fail-on-severity": "moderate", "base-ref": _PR_OR_PUSH_BASE,
            "head-ref": _PR_OR_PUSH_HEAD,
        }, "dependency review must retain moderate severity and explicit PR/push base and head")
        return tuple(failures)

    require(job.get("name") == "DCO", "DCO job must publish the DCO context")
    require(document.get("env", {}).get("BASH_ENV") ==
            "${{ github.workspace }}/.github/workflows/local-venv-env.sh",
            "DCO Python must use the checkout-local venv hook")
    checkouts = _pinned_action_steps(job, "actions/checkout")
    require(len(checkouts) == 1 and checkouts[0].get("with") == {
        "persist-credentials": "false", "fetch-depth": "0", "ref": _PR_OR_PUSH_HEAD,
    }, "DCO must check out the exact contributed head with complete history")
    setups = _pinned_action_steps(job, "actions/setup-python")
    require(len(setups) == 1 and setups[0].get("with") == {"python-version": "3.12"},
            "DCO must use pinned Python 3.12 setup")
    steps = job.get("steps", [])
    runs = [step for step in steps if isinstance(step, dict) and "run" in step]
    expected_lines = [
        'test "$(git rev-parse HEAD)" = "$HEAD_SHA"',
        '[[ "$BASE_SHA" =~ ^[0-9a-f]{40}$ ]]',
        'test "$BASE_SHA" != 0000000000000000000000000000000000000000',
        'if [[ "$EVENT_NAME" == pull_request ]]; then',
        'base=$(git merge-base "$BASE_SHA" HEAD)',
        'else', 'base=$BASE_SHA', 'fi',
        'python -m tools.audit_review_commits --root . --base "$base" --github-repository "$REPOSITORY"',
    ]
    command = runs[0].get("run", "") if len(runs) == 1 else ""
    actual_lines = [" ".join(line.split()) for line in command.replace("\\\n", " ").splitlines()
                    if line.strip()]
    require(len(steps) == 3 and len(runs) == 1 and actual_lines == expected_lines
            and runs[0].get("env") == {
                "GH_TOKEN": "${{ github.token }}", "REPOSITORY": "${{ github.repository }}",
                "EVENT_NAME": "${{ github.event_name }}", "BASE_SHA": _PR_OR_PUSH_BASE,
                "HEAD_SHA": _PR_OR_PUSH_HEAD,
            }, "DCO must validate the head and base, select PR merge-base or push before, and run the real audit")
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
        failures += codeql_version_failures(path)
        failures += hosted_lane_contract_failures(path)
        failures += protected_context_contract_failures(path)
    if failures:
        parser.error("\n".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
