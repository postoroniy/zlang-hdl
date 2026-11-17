#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Require signed, DCO-complete commits on a public review branch."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


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


class _NoGitHubRedirects(HTTPRedirectHandler):
    """Keep the optional authentication header on the official API host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _github_review_commit(repository: str, identity: str) -> ReviewCommit:
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository)
            or repository.split("/")[1] in {".", ".."}):
        raise ReviewCommitAuditError("GitHub repository must be owner/repo")
    if not re.fullmatch(r"[0-9a-f]{40}", identity):
        raise ReviewCommitAuditError("GitHub verification requires an exact commit SHA")
    url = f"https://api.github.com/repos/{repository}/git/commits/{identity}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "zlang-review-commit-audit",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = Request(url, headers=headers)
        with build_opener(_NoGitHubRedirects()).open(request, timeout=30) as response:
            if response.status != 200:
                raise ReviewCommitAuditError(
                    f"GitHub commit lookup failed for {identity}: HTTP {response.status}"
                )
            payload = response.read(4 * 1024 * 1024 + 1)
        if len(payload) > 4 * 1024 * 1024:
            raise ReviewCommitAuditError(f"GitHub commit metadata is oversized: {identity}")
        data = json.loads(payload)
    except ReviewCommitAuditError:
        raise
    except HTTPError as error:
        raise ReviewCommitAuditError(
            f"GitHub commit lookup failed for {identity}: HTTP {error.code}"
        ) from None
    except (OSError, URLError, ValueError) as error:
        # Network, JSON and request errors may contain credentials or response
        # content. Report only their class and the already-validated commit SHA.
        raise ReviewCommitAuditError(
            f"GitHub commit lookup failed for {identity}: {type(error).__name__}"
        ) from None
    if not isinstance(data, dict) or data.get("sha") != identity:
        raise ReviewCommitAuditError(f"GitHub commit identity mismatch: {identity}")
    verification = data.get("verification")
    if (not isinstance(verification, dict) or verification.get("verified") is not True
            or verification.get("reason") != "valid"):
        raise ReviewCommitAuditError(
            f"commit {identity} lacks a valid GitHub-verified cryptographic signature"
        )
    author = data.get("author")
    if (not isinstance(author, dict)
            or any(not isinstance(author.get(key), str) or not author[key]
                   or any(character in author[key] for character in "\r\n\0")
                   for key in ("name", "email"))
            or not isinstance(data.get("message"), str)):
        raise ReviewCommitAuditError(f"GitHub commit metadata is malformed: {identity}")
    return ReviewCommit(identity, author["name"], author["email"], "G", data["message"])


def audit_review_commits(
    root: Path, *, base: str, github_repository: str | None = None,
) -> tuple[str, ...]:
    root = root.resolve()
    # Generic signed-commit fixtures need no Community product policy; actual
    # release preflight requires it, and Community review checks reachable history.
    if (root / "release/community-boundary.json").is_file():
        try:
            from tools.community_boundary import load_policy, validate_history, BoundaryError
        except ModuleNotFoundError:
            from community_boundary import load_policy, validate_history, BoundaryError
        try:
            validate_history(root, load_policy(root / "release/community-boundary.json"))
        except BoundaryError as error:
            raise ReviewCommitAuditError(str(error)) from error
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
        if github_repository is not None:
            validate_review_commit(_github_review_commit(github_repository, identity))
            continue
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
    parser.add_argument(
        "--github-repository", metavar="OWNER/REPO",
        help="use exact GitHub-verified commit signatures instead of local GPG verification",
    )
    arguments = parser.parse_args(argv)
    try:
        identities = audit_review_commits(
            arguments.root, base=arguments.base, github_repository=arguments.github_repository,
        )
    except (OSError, subprocess.TimeoutExpired, ReviewCommitAuditError) as exc:
        print(f"review commit audit: error: {exc}", file=sys.stderr)
        return 2
    print(f"review commit audit passed: {len(identities)} signed DCO commits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
