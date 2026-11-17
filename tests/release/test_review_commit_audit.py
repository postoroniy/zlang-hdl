# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy
from io import BytesIO
import json
from urllib.error import HTTPError, URLError

import pytest

import tools.audit_review_commits as audit

from tools.audit_review_commits import (
    ReviewCommit,
    ReviewCommitAuditError,
    validate_review_commit,
)


def _commit(*, signature: str = "G", trailer: str | None = None) -> ReviewCommit:
    author_name = "Release Maintainer"
    author_email = "release@example.invalid"
    signoff = trailer or f"Signed-off-by: {author_name} <{author_email}>"
    return ReviewCommit(
        identity="a" * 40,
        author_name=author_name,
        author_email=author_email,
        signature_status=signature,
        message=f"Prepare candidate\n\n{signoff}\n",
    )


@pytest.mark.parametrize("signature", ("G", "U"))
def test_review_commit_accepts_good_signature_and_exact_dco(signature: str) -> None:
    validate_review_commit(_commit(signature=signature))


@pytest.mark.parametrize("signature", ("N", "B", "E", "X", "Y", "R"))
def test_review_commit_rejects_missing_or_invalid_signature(signature: str) -> None:
    with pytest.raises(ReviewCommitAuditError, match="cryptographic signature"):
        validate_review_commit(_commit(signature=signature))


def test_review_commit_rejects_nonmatching_dco_identity() -> None:
    with pytest.raises(ReviewCommitAuditError, match="exact DCO trailer"):
        validate_review_commit(
            _commit(trailer="Signed-off-by: Someone Else <else@example.invalid>")
        )


def _github_payload(identity: str = "a" * 40) -> dict:
    commit = _commit()
    return {
        "sha": identity,
        "author": {"name": commit.author_name, "email": commit.author_email},
        "message": commit.message,
        "verification": {"verified": True, "reason": "valid"},
    }


def _github_responses(monkeypatch, payloads):
    pending = iter(payloads)
    calls = []

    class Response(BytesIO):
        status = 200

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request, timeout))
            payload = next(pending)
            if isinstance(payload, Exception):
                raise payload
            return Response(payload if isinstance(payload, bytes) else json.dumps(payload).encode())

    monkeypatch.setattr(audit, "build_opener", lambda handler: Opener())
    return calls


def _git_history(monkeypatch, identities, *, signature="G", ancestor=True):
    calls = []

    def git(root, *arguments):
        calls.append(arguments)
        if arguments[:2] == ("merge-base", "--is-ancestor") and not ancestor:
            raise ReviewCommitAuditError("base is not an ancestor")
        if arguments[0] == "rev-list":
            return "\n".join(identities)
        if arguments[0] == "show":
            commit = _commit(signature=signature)
            return "\0".join((commit.identity, commit.author_name, commit.author_email,
                              commit.signature_status, commit.message))
        return "a" * 40

    monkeypatch.setattr(audit, "_git", git)
    return calls


def test_github_mode_checks_every_exact_commit_and_dco(monkeypatch, tmp_path) -> None:
    identities = ("a" * 40, "b" * 40, "c" * 40)
    git_calls = _git_history(monkeypatch, identities)
    api_calls = _github_responses(monkeypatch, [_github_payload(sha) for sha in identities])
    monkeypatch.setenv("GH_TOKEN", "fixture-only-token")
    monkeypatch.setenv("GITHUB_TOKEN", "other-fixture-token")
    assert audit.audit_review_commits(
        tmp_path, base="origin/main", github_repository="owner/repo",
    ) == identities
    assert ("merge-base", "--is-ancestor", "origin/main", "HEAD") in git_calls
    assert ("rev-list", "--reverse", "origin/main..HEAD") in git_calls
    assert not any(call[0] == "show" for call in git_calls)
    assert [call[0].full_url for call in api_calls] == [
        f"https://api.github.com/repos/owner/repo/git/commits/{sha}" for sha in identities
    ]
    assert all(timeout == 30 for _, timeout in api_calls)
    assert all(request.get_header("Authorization") == "Bearer fixture-only-token"
               for request, _ in api_calls)

    # A valid HEAD cannot conceal an unsigned intermediate commit in the range.
    unsigned = _github_payload(identities[1])
    unsigned["verification"]["verified"] = False
    unsigned["verification"]["reason"] = "unsigned"
    _git_history(monkeypatch, identities)
    api_calls = _github_responses(monkeypatch, [
        _github_payload(identities[0]), unsigned, _github_payload(identities[2]),
    ])
    with pytest.raises(ReviewCommitAuditError, match="cryptographic signature"):
        audit.audit_review_commits(tmp_path, base="origin/main", github_repository="owner/repo")
    assert [call[0].full_url for call in api_calls] == [
        f"https://api.github.com/repos/owner/repo/git/commits/{sha}" for sha in identities[:2]
    ]


@pytest.mark.parametrize("mutation", (
    "stale-sha", "unverified", "string-verified", "invalid-reason", "missing-verification",
    "wrong-author", "missing-author", "invalid-message",
))
def test_github_mode_rejects_untrusted_metadata_and_nonmatching_dco(
    monkeypatch, tmp_path, mutation,
) -> None:
    payload = copy.deepcopy(_github_payload())
    if mutation == "stale-sha":
        payload["sha"] = "b" * 40
    elif mutation == "unverified":
        payload["verification"]["verified"] = False
    elif mutation == "string-verified":
        payload["verification"]["verified"] = "true"
    elif mutation == "invalid-reason":
        payload["verification"]["reason"] = "unknown_key"
    elif mutation == "missing-verification":
        payload.pop("verification")
    elif mutation == "wrong-author":
        payload["author"]["email"] = "other@example.invalid"
    elif mutation == "missing-author":
        payload.pop("author")
    else:
        payload["message"] = ["untrusted message"]
    _git_history(monkeypatch, ("a" * 40,))
    _github_responses(monkeypatch, [payload])
    with pytest.raises(ReviewCommitAuditError):
        audit.audit_review_commits(tmp_path, base="base", github_repository="owner/repo")


@pytest.mark.parametrize("repository", (
    "owner/repo/extra", "owner/repo?access_token=secret", "../repo", "owner/..", "",
))
def test_github_repository_validation_precedes_any_network(monkeypatch, repository) -> None:
    def network_forbidden(*args, **kwargs):
        raise AssertionError("invalid repository reached network")

    monkeypatch.setattr(audit, "build_opener", network_forbidden)
    with pytest.raises(ReviewCommitAuditError, match="owner/repo"):
        audit._github_review_commit(repository, "a" * 40)


@pytest.mark.parametrize("failure", (
    HTTPError("https://api.github.com/fixture", 403, "secret-token", None, None),
    URLError("secret-token"), TimeoutError("secret-token"), b"not JSON secret-token",
))
def test_github_lookup_errors_fail_closed_without_logging_credentials(
    monkeypatch, tmp_path, capsys, failure,
) -> None:
    _git_history(monkeypatch, ("a" * 40,))
    _github_responses(monkeypatch, [failure])
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    assert audit.main([
        "--root", str(tmp_path), "--base", "base", "--github-repository", "owner/repo",
    ]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "GitHub commit lookup failed" in output.err
    assert "secret-token" not in output.err


def test_github_authentication_fallback_and_no_redirects(monkeypatch) -> None:
    calls = _github_responses(monkeypatch, [_github_payload()])
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-fallback-token")
    audit._github_review_commit("owner/repo", "a" * 40)
    assert calls[0][0].get_header("Authorization") == "Bearer fixture-fallback-token"
    assert audit._NoGitHubRedirects().redirect_request(
        None, None, 302, "redirect", {}, "https://other.invalid/",
    ) is None


@pytest.mark.parametrize("identities,ancestor", (((), True), (("a" * 40,), False)))
def test_github_mode_retains_nonempty_range_and_ancestry_guards(
    monkeypatch, tmp_path, identities, ancestor,
) -> None:
    _git_history(monkeypatch, identities, ancestor=ancestor)

    def network_forbidden(*args, **kwargs):
        raise AssertionError("invalid history reached network")

    monkeypatch.setattr(audit, "build_opener", network_forbidden)
    with pytest.raises(ReviewCommitAuditError):
        audit.audit_review_commits(tmp_path, base="base", github_repository="owner/repo")


@pytest.mark.parametrize("signature", ("G", "E"))
def test_default_mode_still_uses_local_gpg_and_never_fetches_github(
    monkeypatch, tmp_path, signature,
) -> None:
    _git_history(monkeypatch, ("a" * 40,), signature=signature)

    def network_forbidden(*args, **kwargs):
        raise AssertionError("local mode reached network")

    monkeypatch.setattr(audit, "build_opener", network_forbidden)
    if signature == "G":
        assert audit.audit_review_commits(tmp_path, base="base") == ("a" * 40,)
    else:
        with pytest.raises(ReviewCommitAuditError, match="cryptographic signature"):
            audit.audit_review_commits(tmp_path, base="base")


def test_github_mode_fails_closed_when_local_git_is_unavailable(
    monkeypatch, tmp_path, capsys,
) -> None:
    def missing_git(*args, **kwargs):
        raise FileNotFoundError("git is unavailable")

    def network_forbidden(*args, **kwargs):
        raise AssertionError("Git failure reached network")

    monkeypatch.setattr(audit.subprocess, "run", missing_git)
    monkeypatch.setattr(audit, "build_opener", network_forbidden)
    assert audit.main([
        "--root", str(tmp_path), "--base", "base", "--github-repository", "owner/repo",
    ]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "git is unavailable" in output.err
