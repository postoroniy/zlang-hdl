#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Validate one prospective or tagged Community release identity.

This command is deliberately non-publishing.  It composes the existing
release-status, changelog, and native-binary validators, binds their accepted
bytes to one Git tree, and writes a deterministic review manifest.
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib

from tools.audit_native_binary import (
    NativeBinaryAuditError,
    audit_native_release_set,
)
from tools.audit_dependency_floor import (
    DependencyFloorError,
    validate_dependency_floor,
)
from tools.release_notes import validate_release_changelog
from tools.release_regressions import (
    RegressionLedgerError,
    validate_regression_ledger,
)
from tools.release_status import StatusError, validate as validate_release_status


SCHEMA = 4
_ALPHA_TAG = re.compile(r"^v(?P<base>[0-9]+\.[0-9]+\.[0-9]+)a(?P<number>[0-9]+)$")


class ReleasePreflightError(ValueError):
    """The candidate cannot be bound to one safe release identity."""


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReleasePreflightError(f"cannot read {path}: {exc}") from exc


def _git(root: Path, *arguments: str, check: bool = True) -> str:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleasePreflightError(f"cannot inspect Git identity: {exc}") from exc
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ReleasePreflightError(
            f"git {' '.join(arguments)} failed: {detail or completed.returncode}"
        )
    return completed.stdout.strip()


def _git_succeeds(root: Path, *arguments: str) -> bool:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleasePreflightError(f"cannot inspect Git identity: {exc}") from exc
    return completed.returncode == 0


def _require_exact_candidate_main(root: Path, candidate_commit: str) -> None:
    """Reject a candidate unless it is the checked-out protected main tip.

    A manual candidate validates the prospective release *before* a tag exists.
    Checking only ancestry would allow an arbitrary feature branch to be treated
    as releasable.  The hosted workflow fetches this same remote-tracking ref;
    local candidates must establish it explicitly too.
    """

    current_branch = _git(root, "branch", "--show-current")
    if current_branch != "main":
        raise ReleasePreflightError(
            "candidate release requires checked-out branch 'main', "
            f"not {current_branch or 'a detached HEAD'}"
        )
    origin_main = _git(root, "rev-parse", "--verify", "refs/remotes/origin/main")
    if origin_main != candidate_commit:
        raise ReleasePreflightError(
            "candidate release requires HEAD to equal refs/remotes/origin/main"
        )


def _alpha_sequence(tag: str, previous_tag: str) -> None:
    current = _ALPHA_TAG.fullmatch(tag)
    previous = _ALPHA_TAG.fullmatch(previous_tag)
    if current is None or previous is None:
        raise ReleasePreflightError("release and previous tags must be alpha tags")
    if current.group("base") != previous.group("base"):
        raise ReleasePreflightError("release and previous tags have different bases")
    if int(current.group("number")) != int(previous.group("number")) + 1:
        raise ReleasePreflightError(
            f"release tag {tag!r} must immediately follow {previous_tag!r}"
        )


def _previous_release_tag(root: Path) -> str | None:
    """Return the nearest prior tag, or no baseline for a one-commit repository."""

    previous = _git(root, "describe", "--tags", "--abbrev=0", "HEAD^", check=False)
    if previous:
        return previous
    if _git(root, "rev-list", "--count", "HEAD") != "1":
        raise ReleasePreflightError(
            "release history has no prior tag; only a one-commit repository may "
            "establish a baseline release"
        )
    return None


def _status(root: Path) -> dict[str, object]:
    try:
        value = json.loads((root / "release/status.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleasePreflightError(f"cannot read release status: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleasePreflightError("release status must be a JSON object")
    return value


def preflight(
    root: Path,
    *,
    tag: str,
    mode: str,
    require_clean: bool,
) -> dict[str, object]:
    """Return the deterministic manifest for one accepted release candidate."""

    root = root.resolve()
    if mode not in {"candidate", "tagged"}:
        raise ReleasePreflightError("mode must be 'candidate' or 'tagged'")
    if _ALPHA_TAG.fullmatch(tag) is None:
        raise ReleasePreflightError("release tag must be an alpha tag")
    previous_tag = _previous_release_tag(root)
    if previous_tag is not None:
        _alpha_sequence(tag, previous_tag)
    candidate_commit = _git(root, "rev-parse", "HEAD")
    candidate_date = date.fromisoformat(
        _git(root, "show", "-s", "--format=%cs", candidate_commit)
    )
    try:
        validate_release_status(root, tag=tag)
    except StatusError as exc:
        raise ReleasePreflightError(str(exc)) from exc

    status = _status(root)
    release = status.get("release")
    if not isinstance(release, dict) or release.get("version") != tag.removeprefix("v"):
        raise ReleasePreflightError("release status version does not match the tag")
    version = tag.removeprefix("v")
    try:
        regressions = validate_regression_ledger(
            root,
            release=version,
        )
    except RegressionLedgerError as exc:
        raise ReleasePreflightError(str(exc)) from exc

    try:
        notes = validate_release_changelog(
            (root / "CHANGELOG.md").read_text(encoding="utf-8"),
            tag,
            expected_date=candidate_date,
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ReleasePreflightError(f"release notes are invalid: {exc}") from exc

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    native = pyproject.get("project", {}).get("optional-dependencies", {}).get("native")
    expected_native = (
        f"zlang-native-sim=={version}; platform_system == 'Linux' "
        "and platform_machine == 'x86_64'"
    )
    if native != [expected_native]:
        raise ReleasePreflightError("optional native dependency does not match release")

    wheel_paths = tuple(sorted((root / "release/native-wheels").glob("*.whl")))
    try:
        native_infos = audit_native_release_set(wheel_paths, expected_version=version)
    except (NativeBinaryAuditError, OSError) as exc:
        raise ReleasePreflightError(f"native release set is invalid: {exc}") from exc

    candidate_tree = _git(root, "rev-parse", "HEAD^{tree}")
    previous_release: dict[str, object] | None = None
    if previous_tag is not None:
        previous_commit = _git(root, "rev-list", "-n", "1", previous_tag)
        if not previous_commit:
            raise ReleasePreflightError(f"previous tag {previous_tag!r} is unavailable")
        if not _git_succeeds(root, "merge-base", "--is-ancestor", previous_tag, "HEAD"):
            raise ReleasePreflightError(
                f"candidate is not descended from {previous_tag}"
            )
        try:
            dependency_floor = validate_dependency_floor(
                root, previous_ref=previous_tag
            )
        except DependencyFloorError as exc:
            raise ReleasePreflightError(str(exc)) from exc
        previous_release = {
            "tag": previous_tag,
            "commit": previous_commit,
            "dependency_floor": dependency_floor,
        }

    tag_type = _git(root, "cat-file", "-t", f"refs/tags/{tag}", check=False)
    if mode == "candidate":
        if tag_type:
            raise ReleasePreflightError(f"prospective tag {tag!r} already exists")
        _require_exact_candidate_main(root, candidate_commit)
    else:
        if tag_type != "tag":
            raise ReleasePreflightError("tagged release requires an annotated tag")
        if _git(root, "rev-list", "-n", "1", tag) != candidate_commit:
            raise ReleasePreflightError("release tag does not point at HEAD")

    if require_clean and _git(
        root, "status", "--porcelain=v1", "--untracked-files=all"
    ):
        raise ReleasePreflightError("release preflight requires a clean checkout")

    identities = {
        path: _sha256(root / path)
        for path in (
            "CHANGELOG.md",
            "docs/ZLang-HDL-Language-Reference.pdf",
            "editors/vscode/zlang-hdl/package-lock.json",
            "editors/vscode/zlang-hdl/package.json",
            "pyproject.toml",
            "release/regressions.json",
            "release/status.json",
            "zlang/_version.py",
        )
    }
    report: dict[str, object] = {
        "schema": SCHEMA,
        "mode": mode,
        "version": version,
        "tag": tag,
        "git": {
            "commit": candidate_commit,
            "tree": candidate_tree,
        },
        "identities": identities,
        "regressions": regressions,
        "release_notes_sha256": hashlib.sha256(notes.encode("utf-8")).hexdigest(),
        "native_wheels": [
            {
                "file": info.path.name,
                "platform": info.platform,
                "sha256": _sha256(info.path),
                "version": info.version,
            }
            for info in native_infos
        ],
    }
    if previous_release is not None:
        report["previous_release"] = previous_release
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--mode", choices=("candidate", "tagged"), default="candidate")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        report = preflight(
            arguments.root,
            tag=arguments.tag,
            mode=arguments.mode,
            require_clean=arguments.require_clean,
        )
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (ReleasePreflightError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"release-preflight: error: {exc}", file=sys.stderr)
        return 2
    print(f"release preflight valid: {arguments.tag} ({arguments.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
