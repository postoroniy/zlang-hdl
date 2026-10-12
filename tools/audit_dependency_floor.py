#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Reject external dependency/tool downgrades against a previous release ref."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib

from packaging.requirements import Requirement
from packaging.version import InvalidVersion, Version


_ACTION = re.compile(
    r"uses:\s+(?P<name>[^@\s]+)@[0-9a-f]{40}\s+#\s+v(?P<version>[0-9]+(?:\.[0-9]+)*)"
)
_MAKE_VERSION = re.compile(
    r"^(?P<name>[A-Z][A-Z0-9_]*_VERSION)\s*\?=\s*(?P<version>\S+)\s*$",
    re.MULTILINE,
)


class DependencyFloorError(ValueError):
    """One release dependency moved below the previous public release."""


def _git_text(root: Path, ref: str, relative: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), "show", f"{ref}:{relative}"),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise DependencyFloorError(
            f"cannot read {relative!r} from {ref!r}: {detail or completed.returncode}"
        )
    return completed.stdout


def _text(root: Path, relative: str, ref: str | None) -> str:
    if ref is not None:
        return _git_text(root, ref, relative)
    try:
        return (root / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DependencyFloorError(f"cannot read {relative!r}: {exc}") from exc


def _python_versions(text: str) -> dict[str, str]:
    document = tomllib.loads(text)
    project = document.get("project", {})
    groups = [project.get("dependencies", ())]
    groups.extend(project.get("optional-dependencies", {}).values())
    versions: dict[str, str] = {}
    for group in groups:
        for value in group:
            requirement = Requirement(value)
            exact = [item.version for item in requirement.specifier if item.operator == "=="]
            if len(exact) == 1:
                versions[f"python:{requirement.name.lower()}"] = exact[0]
    return versions


def _npm_versions(text: str) -> dict[str, str]:
    document = json.loads(text)
    versions: dict[str, str] = {}
    for path, record in document.get("packages", {}).items():
        if not path.startswith("node_modules/") or not isinstance(record, dict):
            continue
        version = record.get("version")
        if isinstance(version, str):
            versions[f"npm:{path.removeprefix('node_modules/')}"] = version
    return versions


def _action_versions(root: Path, ref: str | None) -> dict[str, str]:
    if ref is None:
        paths = sorted((root / ".github/workflows").glob("*.y*ml"))
        documents = [(path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
                     for path in paths]
    else:
        completed = subprocess.run(
            ("git", "-C", str(root), "ls-tree", "-r", "--name-only", ref,
             ".github/workflows"),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise DependencyFloorError(f"cannot enumerate workflows from {ref!r}")
        documents = [
            (relative, _git_text(root, ref, relative))
            for relative in completed.stdout.splitlines()
            if relative.endswith((".yml", ".yaml"))
        ]
    versions: dict[str, str] = {}
    for relative, text in documents:
        for match in _ACTION.finditer(text):
            key = f"action:{match.group('name')}"
            version = match.group("version")
            previous = versions.setdefault(key, version)
            if previous != version:
                raise DependencyFloorError(
                    f"{relative} uses inconsistent versions of {match.group('name')}"
                )
    return versions


def dependency_versions(root: Path, *, ref: str | None = None) -> dict[str, str]:
    """Return external version floors from the release-owned pin surfaces."""

    root = root.resolve()
    versions = _python_versions(_text(root, "pyproject.toml", ref))
    versions.update(_npm_versions(_text(
        root, "editors/vscode/zlang-hdl/package-lock.json", ref
    )))
    versions.update(_action_versions(root, ref))
    versions.update({
        f"make:{match.group('name')}": match.group("version")
        for match in _MAKE_VERSION.finditer(_text(root, "Makefile", ref))
    })
    status = json.loads(_text(root, "release/status.json", ref))
    versions.update({
        f"eda:{name}": value
        for name, value in status.get("eda_toolchain", {}).items()
        if isinstance(value, str)
    })
    return dict(sorted(versions.items()))


def validate_dependency_floor(root: Path, *, previous_ref: str) -> dict[str, object]:
    previous = dependency_versions(root, ref=previous_ref)
    current = dependency_versions(root)
    common = sorted(previous.keys() & current.keys())
    downgrades: list[str] = []
    compared = 0
    for key in common:
        try:
            before = Version(previous[key])
            after = Version(current[key])
        except InvalidVersion:
            continue
        compared += 1
        if after < before:
            downgrades.append(f"{key}: {before} -> {after}")
    if downgrades:
        raise DependencyFloorError(
            "external dependency downgrade(s) relative to "
            f"{previous_ref}: " + "; ".join(downgrades)
        )
    return {
        "schema": 1,
        "previous_ref": previous_ref,
        "compared": compared,
        "downgrades": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--previous-ref", required=True)
    arguments = parser.parse_args(argv)
    try:
        report = validate_dependency_floor(
            arguments.root, previous_ref=arguments.previous_ref
        )
    except (DependencyFloorError, OSError, ValueError, json.JSONDecodeError,
            tomllib.TOMLDecodeError) as exc:
        print(f"dependency-floor: error: {exc}", file=sys.stderr)
        return 2
    print(
        "dependency floor valid: "
        f"{report['compared']} external versions compared with {arguments.previous_ref}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
