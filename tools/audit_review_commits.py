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


_DEPENDABOT = "dependabot[bot]"
_DEPENDABOT_ID = 49699333
_DEPENDABOT_EMAIL = "49699333+dependabot[bot]@users.noreply.github.com"
_DEPENDABOT_TRAILER = "Signed-off-by: dependabot[bot] <support@github.com>"


def _signed_trailers(commit: ReviewCommit) -> set[str]:
    # Dependabot bodies may contain a Markdown divider before their trailers.
    parsed = subprocess.run(
        ("git", "interpret-trailers", "--parse", "--no-divider"),
        input=commit.message, capture_output=True, text=True, timeout=30, check=False,
    )
    if parsed.returncode != 0:
        raise ReviewCommitAuditError(f"cannot parse DCO trailers for {commit.identity}")
    return set(parsed.stdout.splitlines()).intersection(commit.message.splitlines())


def _signoff(commit: ReviewCommit) -> str:
    return f"Signed-off-by: {commit.author_name} <{commit.author_email}>"


def _has_dependabot_alias(commit: ReviewCommit, trailers: set[str]) -> bool:
    return (commit.author_name == _DEPENDABOT and commit.author_email == _DEPENDABOT_EMAIL
            and _DEPENDABOT_TRAILER in trailers)


def validate_review_commit(commit: ReviewCommit, *, github_repository: str | None = None) -> None:
    if commit.signature_status not in {"G", "U"}:
        raise ReviewCommitAuditError(
            f"commit {commit.identity} lacks a valid cryptographic signature"
        )
    expected = _signoff(commit)
    trailers = _signed_trailers(commit)
    if expected != _DEPENDABOT_TRAILER and expected in trailers:
        return
    if github_repository is not None and _has_dependabot_alias(commit, trailers):
        if not _verified_dependabot_push(github_repository, commit.identity):
            _verify_dependabot_squash(github_repository, commit.identity, trailers)
        return
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


def _github_json(url: str, identity: str, *, body: dict | None = None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "zlang-review-commit-audit",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=json.dumps(body).encode() if body is not None else None,
                          headers=headers)
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
    if not isinstance(data, dict):
        raise ReviewCommitAuditError(f"GitHub commit metadata is malformed: {identity}")
    return data


def _github_review_commit(repository: str, identity: str) -> ReviewCommit:
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository)
            or repository.split("/")[1] in {".", ".."}):
        raise ReviewCommitAuditError("GitHub repository must be owner/repo")
    if not re.fullmatch(r"[0-9a-f]{40}", identity):
        raise ReviewCommitAuditError("GitHub verification requires an exact commit SHA")
    data = _github_json(f"https://api.github.com/repos/{repository}/git/commits/{identity}", identity)
    if data.get("sha") != identity:
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


_PUSH_FIELDS = """
author { user { login databaseId } }
signature { isValid state wasSignedByGitHub signer { login databaseId } }
checkSuites(first:100) { nodes { push {
  nextSha repository { nameWithOwner } pusher { __typename login ... on Bot { databaseId } }
} } }
"""


def _github_commit_fields(repository: str, identity: str, fields: str) -> dict:
    owner, name = repository.split("/")
    query = ("query($owner:String!, $name:String!, $sha:GitObjectID!) {"
             "repository(owner:$owner, name:$name) { nameWithOwner object(oid:$sha) { ... on Commit {"
             "oid " + fields + "} } } }")
    payload = _github_json("https://api.github.com/graphql", identity, body={
        "query": query, "variables": {"owner": owner, "name": name, "sha": identity},
    })
    try:
        repo = payload["data"]["repository"]
        commit = repo["object"]
        if not payload.get("errors") and repo["nameWithOwner"] == repository and commit["oid"] == identity:
            return commit
    except (KeyError, TypeError):
        pass
    raise ReviewCommitAuditError(f"cannot verify Dependabot commit metadata for {identity}")


def _verified_dependabot_push(repository: str, identity: str) -> bool:
    commit = _github_commit_fields(repository, identity, _PUSH_FIELDS)
    error = ReviewCommitAuditError(f"cannot verify Dependabot signed alias and push for {identity}")
    try:
        signature = commit["signature"]
        if (commit["author"]["user"] != {"login": _DEPENDABOT, "databaseId": _DEPENDABOT_ID}
                or signature["isValid"] is not True or signature["state"] != "VALID"
                or signature["wasSignedByGitHub"] is not True
                or signature["signer"] != {"login": "web-flow", "databaseId": 19864447}):
            raise error
        # A Git author string or a workflow's associated PR does not identify the pusher.
        expected_push = {
            "nextSha": identity, "repository": {"nameWithOwner": repository},
            "pusher": {"__typename": "Bot", "login": "dependabot", "databaseId": _DEPENDABOT_ID},
        }
        nodes = commit["checkSuites"]["nodes"]
        if isinstance(nodes, list):
            return any(isinstance(node, dict) and node.get("push") == expected_push for node in nodes)
    except (KeyError, TypeError):
        raise error from None
    raise error


_SQUASH_FIELDS = """
tree { oid } parents(first:2) { nodes { oid tree { oid } } }
associatedPullRequests(first:10) { nodes {
  merged mergeCommit { oid } baseRefName headRefOid
  baseRepository { nameWithOwner } headRepository { nameWithOwner }
  author { __typename login ... on Bot { databaseId } }
  commits(first:100) { totalCount nodes { commit { oid tree { oid } parents(first:2) { nodes { oid tree { oid } } } } } }
} }
"""


def _tree_delta(repository: str, before: str, after: str) -> dict:
    """Exact old/new Git objects and modes per changed path, including binary files."""
    def entries(oid):
        error = ReviewCommitAuditError(f"cannot verify complete Git tree {oid}")
        if not re.fullmatch(r"[0-9a-f]{40}", oid):
            raise error
        data = _github_json(f"https://api.github.com/repos/{repository}/git/trees/{oid}?recursive=1", oid)
        if data.get("sha") != oid or data.get("truncated") is not False or not isinstance(data.get("tree"), list):
            raise error
        files = {}
        paths = set()
        kinds = {"040000": "tree", "100644": "blob", "100755": "blob", "120000": "blob", "160000": "commit"}
        for entry in data["tree"]:
            path, mode, kind, sha = (entry[key] for key in ("path", "mode", "type", "sha"))
            if (not isinstance(path, str) or not path or path in paths or mode not in kinds or kinds[mode] != kind
                    or not re.fullmatch(r"[0-9a-f]{40}", sha)):
                raise error
            paths.add(path)
            # Nonempty directory IDs reflect all descendants, including unrelated base changes.
            if kind != "tree" or sha == "4b825dc642cb6eb9a060e54bf8d69288fbee4904":
                files[path] = (mode, sha)
        return files

    old, new = entries(before), entries(after)
    return {path: (old.get(path), new.get(path)) for path in old.keys() | new.keys()
            if old.get(path) != new.get(path)}


def _squashed_originals(repository: str, identity: str) -> list[str]:
    merged = _github_commit_fields(repository, identity, _SQUASH_FIELDS)
    error = ReviewCommitAuditError(f"cannot verify Dependabot squash tree and PR for {identity}")
    try:
        [pr] = [pr for pr in merged["associatedPullRequests"]["nodes"]
                if pr["merged"] is True and pr["mergeCommit"] == {"oid": identity}
                and pr["baseRefName"] == "main"
                and pr["headRepository"] == pr["baseRepository"] == {"nameWithOwner": repository}
                and pr["author"] == {"__typename": "Bot", "login": "dependabot", "databaseId": _DEPENDABOT_ID}]
        [merge_parent] = merged["parents"]["nodes"]
        originals = [node["commit"] for node in pr["commits"]["nodes"]]
        if (not originals or len(originals) != pr["commits"]["totalCount"]
                or originals[-1]["oid"] != pr["headRefOid"]):
            raise error
        [parent] = originals[0]["parents"]["nodes"]
        source_trees = parent["tree"]["oid"], originals[-1]["tree"]["oid"]
        merge_trees = merge_parent["tree"]["oid"], merged["tree"]["oid"]
        if not all(re.fullmatch(r"[0-9a-f]{40}", oid) for oid in
                   (*source_trees, *merge_trees, parent["oid"], merge_parent["oid"])):
            raise error
        same_parent = parent["oid"] == merge_parent["oid"]
        shas = []
        for original in originals:
            sha = original["oid"]
            if (original["parents"]["nodes"] != [parent] or sha == identity or sha in shas
                    or not re.fullmatch(r"[0-9a-f]{40}", sha)):
                raise error
            shas.append(sha)
            parent = {"oid": sha, "tree": original["tree"]}
        if source_trees != merge_trees and (
            same_parent or _tree_delta(repository, *source_trees) != _tree_delta(repository, *merge_trees)
        ):
            raise error
        return shas
    except ReviewCommitAuditError:
        raise
    except (KeyError, TypeError, ValueError):
        raise error from None


def _verify_dependabot_squash(repository: str, identity: str, trailers: set[str]) -> None:
    seen_bot = False
    for sha in _squashed_originals(repository, identity):
        original = _github_review_commit(repository, sha)
        if _has_dependabot_alias(original, _signed_trailers(original)):
            if not _verified_dependabot_push(repository, sha):
                raise ReviewCommitAuditError(f"cannot verify Dependabot original push for {sha}")
            seen_bot = True
        else:
            validate_review_commit(original)
            if _signoff(original) not in trailers:
                raise ReviewCommitAuditError(f"squash lacks original author DCO trailer for {sha}")
    if not seen_bot:
        raise ReviewCommitAuditError(f"squash lacks a Dependabot-signed original for {identity}")


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
            validate_review_commit(_github_review_commit(github_repository, identity),
                                   github_repository=github_repository)
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
        commit = ReviewCommit(*fields)
        # Unpublished maintainer commits can still be checked by local GPG.
        bot_repository = os.environ.get("ZLANG_DEPENDABOT_REPOSITORY")
        if (bot_repository and commit.author_name == _DEPENDABOT
                and commit.author_email == _DEPENDABOT_EMAIL and _DEPENDABOT_TRAILER in commit.message.splitlines()):
            validate_review_commit(_github_review_commit(bot_repository, identity), github_repository=bot_repository)
        else:
            validate_review_commit(commit)
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
