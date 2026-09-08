#!/usr/bin/env python3
"""Fail closed when auditing the exact frozen release inventory, including pip."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
# Only canonical, concrete versions emitted by pip freeze; never specifiers.
VERSION = (
    r"(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?"
    r"(?:\.post[0-9]+)?(?:\.dev[0-9]+)?"
    r"(?:\+[a-z0-9]+(?:\.[a-z0-9]+)*)?"
)
PIN = re.compile(rf"({NAME})==({VERSION})")
AUDIT_TIMEOUT_SECONDS = 300


class InventoryError(RuntimeError):
    pass


def _name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def read_inventory(path: Path, project_version: str) -> dict[str, str]:
    """Return every frozen dependency except the one exact current local project."""
    if re.fullmatch(VERSION, project_version) is None:
        raise InventoryError("project version must be a concrete canonical version")
    inventory: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            continue
        match = PIN.fullmatch(line)
        if match is None:
            raise InventoryError(f"inventory line {number} must be exactly name==version")
        name, version = _name(match[1]), match[2]
        if name in inventory:
            raise InventoryError(f"duplicate inventory package: {name}")
        inventory[name] = version
    if inventory.pop("zlang-hdl", None) != project_version:
        raise InventoryError("inventory must contain exactly the current zlang-hdl version")
    if "pip" not in inventory:
        raise InventoryError("inventory must include the pip installer")
    return inventory


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise InventoryError("audit report contains duplicate JSON keys")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise InventoryError(f"audit report contains invalid JSON constant: {value}")


def check_report(path: Path, expected: dict[str, str]) -> int:
    """Validate strict pip-audit JSON coverage, returning the finding count."""
    try:
        report = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise InventoryError("audit report is missing, unreadable, or invalid JSON") from exc
    if (
        not isinstance(report, dict)
        or set(report) != {"dependencies", "fixes"}
        or not isinstance(report["dependencies"], list)
        or report["fixes"] != []
    ):
        raise InventoryError("audit report has an unsupported or malformed result structure")
    seen: dict[str, str] = {}
    findings = 0
    for dependency in report["dependencies"]:
        if not isinstance(dependency, dict) or set(dependency) != {"name", "version", "vulns"}:
            raise InventoryError("audit dependency is malformed, skipped, or has unknown fields")
        name, version, vulns = (dependency[key] for key in ("name", "version", "vulns"))
        if (
            not isinstance(name, str)
            or re.fullmatch(NAME, name) is None
            or not isinstance(version, str)
            or not isinstance(vulns, list)
        ):
            raise InventoryError("audit dependency has invalid name, version, or vulnerabilities")
        name = _name(name)
        if name in seen:
            raise InventoryError(f"duplicate audit dependency: {name}")
        if expected.get(name) != version:
            raise InventoryError(f"unexpected audit dependency or version: {name}")
        seen[name] = version
        findings += len(vulns)
    if seen != expected:
        raise InventoryError("audit report does not cover every frozen dependency")
    return findings


def audit(requirements: Path, project_version: str, report: Path) -> int:
    """Keep raw evidence; success requires complete coverage and zero findings."""
    inventory = read_inventory(requirements, project_version)
    audit_input = report.with_suffix(".requirements.txt")
    if len({path.resolve() for path in (requirements, report, audit_input)}) != 3:
        raise InventoryError("requirements, report, and derived audit input must be distinct")
    if any(path.exists() or path.is_symlink() for path in (report, audit_input)):
        raise InventoryError("audit outputs must be fresh; refusing existing report or audit input")
    report.parent.mkdir(parents=True, exist_ok=True)
    with audit_input.open("x", encoding="utf-8") as stream:
        stream.writelines(f"{name}=={version}\n" for name, version in sorted(inventory.items()))
    # Reserve a fresh output: an exit-zero tool that writes nothing cannot reuse evidence.
    with report.open("x", encoding="utf-8"):
        pass
    command = [
        sys.executable, "-m", "pip_audit", "--strict", "--no-deps", "--disable-pip",
        "--vulnerability-service", "pypi", "--progress-spinner", "off",
        "-r", str(audit_input), "--format", "json", "--output", str(report),
    ]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PIP_AUDIT_")}
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, env=environment,
            timeout=AUDIT_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise InventoryError(f"pip-audit timed out after {AUDIT_TIMEOUT_SECONDS} seconds") from exc
    except OSError as exc:
        raise InventoryError("pip-audit could not be executed") from exc
    findings = check_report(report, inventory)
    if findings:
        raise InventoryError(f"pip-audit reported {findings} vulnerability findings; release blocked")
    if completed.returncode != 0:
        raise InventoryError(f"pip-audit failed with exit status {completed.returncode}; release blocked")
    return len(inventory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--project-version", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        count = audit(args.requirements, args.project_version, args.report)
    except (InventoryError, OSError, UnicodeError) as exc:
        print(f"release-inventory: error: {exc}", file=sys.stderr)
        return 1
    print(f"release inventory audit passed: {count} exact dependencies including pip; no findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
