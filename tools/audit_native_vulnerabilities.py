#!/usr/bin/env python3
"""Fail-closed advisory audit for the SBOM embedded in a native wheel."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sys

if __package__:
    from tools.audit_native_binary import NativeBinaryAuditError, read_native_sbom
else:
    from audit_native_binary import NativeBinaryAuditError, read_native_sbom


SCHEMA = 1
_PACKAGE_NAME = re.compile(r"[A-Za-z0-9_-]+")
_PACKAGE_VERSION = re.compile(r"[A-Za-z0-9.+_-]+")


class NativeVulnerabilityAuditError(ValueError):
    """The advisory scan is incomplete, malformed, or has unapproved findings."""


@dataclass(frozen=True, order=True)
class Package:
    name: str
    version: str


@dataclass(frozen=True, order=True)
class Finding:
    advisory_id: str
    package: Package


@dataclass(frozen=True)
class ExceptionRecord:
    advisory_id: str
    package: Package
    rationale: str
    reviewed_on: date
    expires_on: date

    @property
    def finding(self) -> Finding:
        return Finding(self.advisory_id, self.package)


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise NativeVulnerabilityAuditError(f"{context} must be a JSON object")
    return value


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise NativeVulnerabilityAuditError(f"{context} must be a JSON array")
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NativeVulnerabilityAuditError(f"{context} must be a non-empty string")
    return value


def _load_json(path: Path, context: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeVulnerabilityAuditError(f"cannot read {context}: {exc}") from exc
    return _object(value, context)


def _package(name: object, version: object, context: str) -> Package:
    name_text = _text(name, f"{context}.name")
    version_text = _text(version, f"{context}.version")
    if _PACKAGE_NAME.fullmatch(name_text) is None:
        raise NativeVulnerabilityAuditError(f"{context}.name is not a Cargo name")
    if _PACKAGE_VERSION.fullmatch(version_text) is None:
        raise NativeVulnerabilityAuditError(f"{context}.version is malformed")
    return Package(name_text, version_text)


def parse_cyclonedx_inventory(path: Path) -> frozenset[Package]:
    """Read the exact Cargo package inventory from a CycloneDX 1.5 document."""

    document = _load_json(path, "CycloneDX SBOM")
    if document.get("bomFormat") != "CycloneDX" or document.get("specVersion") != "1.5":
        raise NativeVulnerabilityAuditError(
            "native SBOM must be a CycloneDX 1.5 document"
        )
    components = _array(document.get("components"), "CycloneDX components")
    if not components:
        raise NativeVulnerabilityAuditError("CycloneDX components must not be empty")
    result: set[Package] = set()
    for index, raw_component in enumerate(components):
        context = f"CycloneDX components[{index}]"
        component = _object(raw_component, context)
        package = _package(component.get("name"), component.get("version"), context)
        if component.get("purl") != f"pkg:cargo/{package.name}@{package.version}":
            raise NativeVulnerabilityAuditError(
                f"{context}.purl does not identify the exact Cargo package"
            )
        if package in result:
            raise NativeVulnerabilityAuditError(
                f"duplicate CycloneDX component {package.name}@{package.version}"
            )
        result.add(package)
    return frozenset(result)


def parse_osv_report(
    path: Path, expected: frozenset[Package]
) -> frozenset[Finding]:
    """Require complete OSV-Scanner coverage of the exact SBOM inventory."""

    document = _load_json(path, "OSV-Scanner report")
    if "error" in document or "errors" in document:
        raise NativeVulnerabilityAuditError("OSV-Scanner report contains an error")
    results = _array(document.get("results"), "OSV-Scanner results")
    if len(results) != 1:
        raise NativeVulnerabilityAuditError(
            "OSV-Scanner report must contain exactly one SBOM result"
        )
    result = _object(results[0], "OSV-Scanner results[0]")
    source = _object(result.get("source"), "OSV-Scanner result source")
    if source.get("type") != "sbom" or not isinstance(source.get("path"), str):
        raise NativeVulnerabilityAuditError(
            "OSV-Scanner result does not identify an SBOM source"
        )
    packages = _array(result.get("packages"), "OSV-Scanner packages")
    evaluated: set[Package] = set()
    findings: set[Finding] = set()
    for index, raw_record in enumerate(packages):
        context = f"OSV-Scanner packages[{index}]"
        record = _object(raw_record, context)
        if "error" in record or "errors" in record:
            raise NativeVulnerabilityAuditError(
                f"{context} could not be evaluated completely"
            )
        package_data = _object(record.get("package"), f"{context}.package")
        if package_data.get("ecosystem") != "crates.io":
            raise NativeVulnerabilityAuditError(
                f"{context}.package has an unexpected ecosystem"
            )
        package = _package(
            package_data.get("name"), package_data.get("version"), f"{context}.package"
        )
        if package in evaluated:
            raise NativeVulnerabilityAuditError(
                f"OSV-Scanner reported {package.name}@{package.version} more than once"
            )
        evaluated.add(package)
        raw_vulnerabilities = record.get("vulnerabilities", [])
        vulnerabilities = _array(
            raw_vulnerabilities, f"{context}.vulnerabilities"
        )
        for vulnerability_index, raw_vulnerability in enumerate(vulnerabilities):
            vulnerability = _object(
                raw_vulnerability,
                f"{context}.vulnerabilities[{vulnerability_index}]",
            )
            advisory_id = _text(
                vulnerability.get("id"),
                f"{context}.vulnerabilities[{vulnerability_index}].id",
            )
            finding = Finding(advisory_id, package)
            if finding in findings:
                raise NativeVulnerabilityAuditError(
                    f"duplicate advisory {advisory_id} for "
                    f"{package.name}@{package.version}"
                )
            findings.add(finding)
    if evaluated != set(expected):
        missing = sorted(set(expected) - evaluated)
        unexpected = sorted(evaluated - set(expected))
        raise NativeVulnerabilityAuditError(
            "OSV-Scanner did not evaluate the exact SBOM inventory; "
            f"missing={missing}, unexpected={unexpected}"
        )
    return frozenset(findings)


def _parse_date(value: object, context: str) -> date:
    text = _text(value, context)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise NativeVulnerabilityAuditError(
            f"{context} must use YYYY-MM-DD"
        ) from exc


def parse_exceptions(path: Path, *, as_of: date) -> tuple[ExceptionRecord, ...]:
    """Read exact, reviewed, time-bounded vulnerability exceptions."""

    document = _load_json(path, "native vulnerability exceptions")
    if set(document) != {"schema", "exceptions"} or document.get("schema") != SCHEMA:
        raise NativeVulnerabilityAuditError(
            "native vulnerability exceptions have an unsupported schema"
        )
    records: list[ExceptionRecord] = []
    seen: set[Finding] = set()
    for index, raw_record in enumerate(
        _array(document.get("exceptions"), "native vulnerability exceptions")
    ):
        context = f"native vulnerability exceptions[{index}]"
        record = _object(raw_record, context)
        expected_keys = {
            "advisory_id",
            "package",
            "version",
            "rationale",
            "reviewed_on",
            "expires_on",
        }
        if set(record) != expected_keys:
            raise NativeVulnerabilityAuditError(
                f"{context} has unexpected or missing fields"
            )
        package = _package(record.get("package"), record.get("version"), context)
        reviewed_on = _parse_date(record.get("reviewed_on"), f"{context}.reviewed_on")
        expires_on = _parse_date(record.get("expires_on"), f"{context}.expires_on")
        if reviewed_on > as_of:
            raise NativeVulnerabilityAuditError(f"{context} has a future review date")
        if expires_on < as_of:
            raise NativeVulnerabilityAuditError(f"{context} is expired")
        item = ExceptionRecord(
            advisory_id=_text(record.get("advisory_id"), f"{context}.advisory_id"),
            package=package,
            rationale=_text(record.get("rationale"), f"{context}.rationale"),
            reviewed_on=reviewed_on,
            expires_on=expires_on,
        )
        if item.finding in seen:
            raise NativeVulnerabilityAuditError(f"duplicate {context}")
        seen.add(item.finding)
        records.append(item)
    return tuple(records)


def audit_report(
    *,
    sbom: Path,
    report: Path,
    exceptions: Path,
    output: Path,
    scanner_exit_code: int,
    scanner_version: str,
    as_of: date,
) -> None:
    """Validate scanner completeness/findings and emit deterministic evidence."""

    if scanner_exit_code != 0:
        raise NativeVulnerabilityAuditError(
            f"OSV-Scanner failed with exit code {scanner_exit_code}"
        )
    packages = parse_cyclonedx_inventory(sbom)
    findings = parse_osv_report(report, packages)
    exception_records = parse_exceptions(exceptions, as_of=as_of)
    approvals = {record.finding: record for record in exception_records}
    unused = sorted(set(approvals) - set(findings))
    if unused:
        raise NativeVulnerabilityAuditError(
            f"native vulnerability exceptions are stale or unused: {unused}"
        )
    unapproved = sorted(set(findings) - set(approvals))
    evidence = {
        "schema": SCHEMA,
        "as_of": as_of.isoformat(),
        "scanner": {"name": "OSV-Scanner", "version": scanner_version},
        "sbom_sha256": hashlib.sha256(sbom.read_bytes()).hexdigest(),
        "package_count": len(packages),
        "findings": [
            {
                "advisory_id": finding.advisory_id,
                "package": finding.package.name,
                "version": finding.package.version,
                "approved": finding in approvals,
            }
            for finding in sorted(findings)
        ],
        "approved_exceptions": [
            {
                "advisory_id": record.advisory_id,
                "package": record.package.name,
                "version": record.package.version,
                "rationale": record.rationale,
                "reviewed_on": record.reviewed_on.isoformat(),
                "expires_on": record.expires_on.isoformat(),
            }
            for record in sorted(
                exception_records,
                key=lambda item: (
                    item.advisory_id,
                    item.package.name,
                    item.package.version,
                ),
            )
        ],
        "status": "accepted" if not unapproved else "rejected",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if unapproved:
        raise NativeVulnerabilityAuditError(
            f"native SBOM has unapproved vulnerabilities: {unapproved}"
        )


def _extract(wheel: Path, output: Path) -> None:
    try:
        payload = read_native_sbom(wheel)
    except (NativeBinaryAuditError, OSError) as exc:
        raise NativeVulnerabilityAuditError(
            f"cannot extract native wheel SBOM: {exc}"
        ) from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    parse_cyclonedx_inventory(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    extract = subparsers.add_parser("extract")
    extract.add_argument("wheel", type=Path)
    extract.add_argument("--output", type=Path, required=True)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--sbom", type=Path, required=True)
    audit.add_argument("--report", type=Path, required=True)
    audit.add_argument("--exceptions", type=Path, required=True)
    audit.add_argument("--output", type=Path, required=True)
    audit.add_argument("--scanner-exit-code", type=int, required=True)
    audit.add_argument("--scanner-version", required=True)
    audit.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    arguments = parser.parse_args()
    try:
        if arguments.command == "extract":
            _extract(arguments.wheel, arguments.output)
        else:
            audit_report(
                sbom=arguments.sbom,
                report=arguments.report,
                exceptions=arguments.exceptions,
                output=arguments.output,
                scanner_exit_code=arguments.scanner_exit_code,
                scanner_version=arguments.scanner_version,
                as_of=arguments.as_of,
            )
    except (NativeVulnerabilityAuditError, OSError) as exc:
        print(f"native vulnerability audit: error: {exc}", file=sys.stderr)
        return 2
    print("native vulnerability audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
