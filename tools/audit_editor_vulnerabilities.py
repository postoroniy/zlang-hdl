#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fail closed on incomplete or vulnerable locked VS Code dependencies."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys


_SEVERITIES = ("info", "low", "moderate", "high", "critical")


class EditorVulnerabilityAuditError(ValueError):
    """The npm audit or lockfile inventory is incomplete or unacceptable."""


@dataclass(frozen=True)
class EditorAuditSummary:
    dependency_count: int
    development_dependency_count: int


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise EditorVulnerabilityAuditError(f"{context} must be a JSON object")
    return value


def _load_json(path: Path, context: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EditorVulnerabilityAuditError(f"cannot read {context}: {exc}") from exc
    return _object(value, context)


def _count(record: dict[str, object], key: str, context: str) -> int:
    value = record.get(key)
    if type(value) is not int or value < 0:
        raise EditorVulnerabilityAuditError(
            f"{context}.{key} must be a non-negative integer"
        )
    return value


def audit_editor_dependencies(
    *,
    lockfile: Path,
    report: Path,
    scanner_exit_code: int,
) -> EditorAuditSummary:
    """Validate one npm v2 report against the exact lockfile inventory."""

    document = _load_json(report, "npm audit report")
    if "error" in document:
        raise EditorVulnerabilityAuditError("npm audit report contains an error")
    if document.get("auditReportVersion") != 2:
        raise EditorVulnerabilityAuditError("npm audit report must use schema v2")

    vulnerabilities = _object(
        document.get("vulnerabilities"), "npm audit vulnerabilities"
    )
    metadata = _object(document.get("metadata"), "npm audit metadata")
    severity_counts = _object(
        metadata.get("vulnerabilities"), "npm audit severity counts"
    )
    counts = {
        severity: _count(severity_counts, severity, "npm audit severity counts")
        for severity in (*_SEVERITIES, "total")
    }
    if counts["total"] != sum(counts[severity] for severity in _SEVERITIES):
        raise EditorVulnerabilityAuditError(
            "npm audit severity total does not match the severity counts"
        )
    has_findings = bool(vulnerabilities) or counts["total"] != 0

    if scanner_exit_code not in {0, 1}:
        raise EditorVulnerabilityAuditError(
            f"npm audit failed with exit code {scanner_exit_code}"
        )
    if (scanner_exit_code == 1) != has_findings:
        finding_text = "findings" if has_findings else "no findings"
        raise EditorVulnerabilityAuditError(
            "npm audit exit code and report are inconsistent: "
            f"exit code {scanner_exit_code} with {finding_text}"
        )
    if has_findings:
        raise EditorVulnerabilityAuditError(
            f"locked editor dependencies have {counts['total']} vulnerability findings"
        )

    dependencies = _object(
        metadata.get("dependencies"), "npm audit dependency counts"
    )
    development_count = _count(
        dependencies, "dev", "npm audit dependency counts"
    )
    dependency_count = _count(
        dependencies, "total", "npm audit dependency counts"
    )
    if development_count <= 0 or dependency_count <= 0:
        raise EditorVulnerabilityAuditError(
            "npm audit did not include the locked editor build tooling"
        )

    lock = _load_json(lockfile, "editor package lock")
    packages = _object(lock.get("packages"), "editor package-lock packages")
    if lock.get("lockfileVersion") != 3 or "" not in packages:
        raise EditorVulnerabilityAuditError(
            "editor package lock must use schema v3 and contain its root package"
        )
    locked_dependency_count = len(packages) - 1
    if dependency_count != locked_dependency_count:
        raise EditorVulnerabilityAuditError(
            "npm audit does not cover the complete lockfile inventory: "
            f"audited={dependency_count}, locked={locked_dependency_count}"
        )
    return EditorAuditSummary(
        dependency_count=dependency_count,
        development_dependency_count=development_count,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--scanner-exit-code", type=int, required=True)
    arguments = parser.parse_args(argv)
    try:
        summary = audit_editor_dependencies(
            lockfile=arguments.lock,
            report=arguments.report,
            scanner_exit_code=arguments.scanner_exit_code,
        )
    except EditorVulnerabilityAuditError as exc:
        print(f"editor vulnerability audit: error: {exc}", file=sys.stderr)
        return 2
    print(
        "editor vulnerability audit passed: "
        f"{summary.dependency_count} locked dependencies, including "
        f"{summary.development_dependency_count} development dependencies"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
