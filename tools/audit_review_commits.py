#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Require signed, DCO-complete commits on a public review branch."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys


class ReviewCommitAuditError(ValueError):
    """The public review branch ancestry or commit attestations are invalid."""


@dataclass(frozen=True)
class ReviewCommit:
    identity: str
    author_name: str
    author_email: str
    signature_status: str
    message: str


def validate_review_commit(commit: ReviewCommit) -> None:
    if commit.signature_status not in {"G", "U"}:
        raise ReviewCommitAuditError(
            f"commit {commit.identity} lacks a valid cryptographic signature"
        )
    expected = f"Signed-off-by: {commit.author_name} <{commit.author_email}>"
    if expected not in commit.message.splitlines():
        raise ReviewCommitAuditError(
            f"commit {commit.identity} lacks the exact DCO trailer {expected!r}"
        )


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ReviewCommitAuditError(
            f"git {' '.join(arguments)} failed: {detail or completed.returncode}"
        )
    return completed.stdout.strip()


def audit_review_commits(root: Path, *, base: str) -> tuple[str, ...]:
    root = root.resolve()
    _git(root, "rev-parse", "--verify", base)
    _git(root, "merge-base", "--is-ancestor", base, "HEAD")
    identities = tuple(
        line
        for line in _git(root, "rev-list", "--reverse", f"{base}..HEAD").splitlines()
        if line
    )
    if not identities:
        raise ReviewCommitAuditError(
            f"public review branch contains no commits after {base}"
        )
    for identity in identities:
        fields = _git(
            root,
            "show",
            "-s",
            "--format=%H%x00%an%x00%ae%x00%G?%x00%B",
            identity,
        ).split("\0", 4)
        if len(fields) != 5:
            raise ReviewCommitAuditError(f"cannot inspect commit {identity}")
        validate_review_commit(ReviewCommit(*fields))
    return identities


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="refs/remotes/origin/main")
    arguments = parser.parse_args(argv)
    try:
        identities = audit_review_commits(arguments.root, base=arguments.base)
    except (OSError, subprocess.TimeoutExpired, ReviewCommitAuditError) as exc:
        print(f"review commit audit: error: {exc}", file=sys.stderr)
        return 2
    print(f"review commit audit passed: {len(identities)} signed DCO commits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
