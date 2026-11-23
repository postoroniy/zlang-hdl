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


def _dependabot_metadata():
    identity = "a" * 40
    author = {"login": "dependabot[bot]", "databaseId": 49699333}
    trailer = "Signed-off-by: dependabot[bot] <support@github.com>"
    rest = _github_payload(identity)
    rest.update(
        author={"name": author["login"], "email": "49699333+dependabot[bot]@users.noreply.github.com"},
        message=f"Bump dependency\n\n---\nChangelog and other text\n\n{trailer}\n",
    )
    push = {
        "nextSha": identity, "repository": {"nameWithOwner": "owner/repo"},
        "pusher": {"__typename": "Bot", "login": "dependabot", "databaseId": 49699333},
    }
    commit = {
        "oid": identity, "author": {"user": author},
        "signature": {"isValid": True, "wasSignedByGitHub": True, "state": "VALID",
                      "signer": {"login": "web-flow", "databaseId": 19864447}},
        "checkSuites": {"nodes": [{"push": None}, {"push": push}]},
    }
    return rest, {"data": {"repository": {"nameWithOwner": "owner/repo", "object": commit}}}


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


def test_dependabot_alias_binds_the_author_signature_push_and_trailer(monkeypatch, tmp_path):
    rest, graph = _dependabot_metadata()
    sha = rest["sha"]
    _git_history(monkeypatch, (sha,))
    calls = _github_responses(monkeypatch, [rest, graph])
    assert audit.audit_review_commits(tmp_path, base="base", github_repository="owner/repo") == (sha,)
    assert [req.full_url for req, _ in calls] == [
        f"https://api.github.com/repos/owner/repo/git/commits/{sha}", "https://api.github.com/graphql",
    ]
    request, timeout = calls[1]
    assert request.get_method() == "POST" and timeout == 30
    assert json.loads(request.data)["variables"] == {"owner": "owner", "name": "repo", "sha": sha}

    for path, value in (
        (("nameWithOwner",), "owner/fork"),
        (("object", "oid"), "b" * 40),
        (("object", "author", "user", "login"), "maintainer"),
        (("object", "author", "user", "databaseId"), 11),
        (("object", "signature", "isValid"), "true"),
        (("object", "signature", "state"), "UNKNOWN_KEY"),
        (("object", "signature", "wasSignedByGitHub"), False),
        (("object", "signature", "signer"), None),
        (("object", "signature", "signer", "databaseId"), 12),
        (("object", "checkSuites", "nodes"), []),
        (("object", "checkSuites", "nodes", 1, "push", "nextSha"), "b" * 40),
        (("object", "checkSuites", "nodes", 1, "push", "repository", "nameWithOwner"), "elsewhere/repo"),
        (("object", "checkSuites", "nodes", 1, "push", "pusher", "__typename"), "User"),
        (("object", "checkSuites", "nodes", 1, "push", "pusher", "login"), "other-bot"),
        (("object", "checkSuites", "nodes", 1, "push", "pusher", "databaseId"), 13),
    ):
        damaged = copy.deepcopy(graph)
        target = damaged["data"]["repository"]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        _github_responses(monkeypatch, [rest, damaged, {"data": None}])
        with pytest.raises(ReviewCommitAuditError, match="verify Dependabot"):
            audit.audit_review_commits(tmp_path, base="base", github_repository="owner/repo")
    for response in ({"data": None}, {**graph, "errors": [{"message": "partial"}]},
                     HTTPError("https://api.github.com/graphql", 403, "denied", None, None)):
        _github_responses(monkeypatch, [rest, response])
        with pytest.raises(ReviewCommitAuditError):
            audit.audit_review_commits(tmp_path, base="base", github_repository="owner/repo")


def test_dependabot_alias_is_specific_to_hosted_bot_and_final_trailers(monkeypatch, tmp_path):
    rest, _ = _dependabot_metadata()
    sha = rest["sha"]
    _git_history(monkeypatch, (sha,))
    for update in (
        {"author": {"name": "Human", "email": rest["author"]["email"]}},
        {"author": {**rest["author"], "email": "support@github.com"}},
        {"author": {**rest["author"], "email": "other[bot]@users.noreply.github.com"}},
        {"message": rest["message"] + "\nThis is a quoted example.\n"},
        {"message": rest["message"].replace("support@", "other@")},
        {"message": "Unsigned contribution"},
        {"verification": {"verified": False, "reason": "unsigned"}},
    ):
        calls = _github_responses(monkeypatch, [{**rest, **update}])
        with pytest.raises(ReviewCommitAuditError):
            audit.audit_review_commits(tmp_path, base="base", github_repository="owner/repo")
        assert len(calls) == 1
    local = ReviewCommit(sha, rest["author"]["name"], rest["author"]["email"], "G", rest["message"])
    calls = _github_responses(monkeypatch, [])
    with pytest.raises(ReviewCommitAuditError, match="exact DCO"):
        validate_review_commit(local)
    assert not calls


def test_dependabot_squash_replays_each_endorsement_and_the_complete_tree(monkeypatch, tmp_path):
    merged, push_evidence = _dependabot_metadata()
    sha = merged["sha"]
    repo = {"nameWithOwner": "owner/repo"}
    base, bot, followup, tree = ({"oid": c * 40} for c in "bcdf")
    base["tree"] = {"oid": "8" * 40}
    bot["tree"] = {"oid": "e" * 40}
    bot_commit, original_push = _dependabot_metadata()
    bot_commit["sha"] = bot["oid"]
    original = original_push["data"]["repository"]["object"]
    original["oid"] = bot["oid"]
    original["checkSuites"]["nodes"][1]["push"]["nextSha"] = bot["oid"]
    push_evidence["data"]["repository"]["object"]["checkSuites"]["nodes"] = [{"push": None}]
    human = _github_payload(followup["oid"])
    human_trailer = "Signed-off-by: Release Maintainer <release@example.invalid>"
    merged["message"] += human_trailer + "\n"
    pr = {
        "merged": True, "mergeCommit": {"oid": sha}, "baseRefName": "main", "headRefOid": followup["oid"],
        "baseRepository": dict(repo), "headRepository": dict(repo),
        "author": {"__typename": "Bot", "login": "dependabot", "databaseId": 49699333},
        "commits": {"totalCount": 2, "nodes": [
            {"commit": {**bot, "parents": {"nodes": [base]}, "tree": {"oid": "e" * 40}}},
            {"commit": {**followup, "parents": {"nodes": [bot]}, "tree": tree}},
        ]},
    }
    squash = {"data": {"repository": {**repo, "object": {
        "oid": sha, "parents": {"nodes": [base]}, "tree": tree,
        "associatedPullRequests": {"nodes": [pr]},
    }}}}
    _git_history(monkeypatch, (sha,))

    def run(*payloads):
        _github_responses(monkeypatch, payloads)
        return audit.audit_review_commits(tmp_path, base="base", github_repository="owner/repo")

    assert run(merged, push_evidence, squash, bot_commit, original_push, human) == (sha,)
    for path, value in (
        (("merged",), False), (("mergeCommit", "oid"), "9" * 40), (("baseRefName",), "another"),
        (("headRefOid",), bot["oid"]), (("headRepository", "nameWithOwner"), "fork/repo"),
        (("baseRepository", "nameWithOwner"), "fork/repo"), (("author", "databaseId"), 42),
        (("commits", "totalCount"), 3), (("commits", "nodes", 1, "commit", "tree"), {"oid": "9" * 40}),
        (("commits", "nodes", 0, "commit", "parents", "nodes"), [{"oid": "9" * 40}]),
        (("commits", "nodes", 1, "commit", "parents", "nodes"), [base, bot]),
    ):
        altered = copy.deepcopy(squash)
        target = altered["data"]["repository"]["object"]["associatedPullRequests"]["nodes"][0]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ReviewCommitAuditError, match="squash"):
            run(merged, push_evidence, altered)
    with pytest.raises(ReviewCommitAuditError, match="lacks original author"):
        run({**merged, "message": bot_commit["message"]}, push_evidence, squash, bot_commit, original_push, human)
    with pytest.raises(ReviewCommitAuditError, match="cryptographic signature"):
        run(merged, push_evidence, squash, bot_commit, original_push,
            {**human, "verification": {"verified": False, "reason": "unsigned"}})
    no_bot_push = copy.deepcopy(original_push)
    no_bot_push["data"]["repository"]["object"]["checkSuites"]["nodes"] = []
    with pytest.raises(ReviewCommitAuditError, match="original push"):
        run(merged, push_evidence, squash, bot_commit, no_bot_push)
    with pytest.raises(ReviewCommitAuditError, match="lacks a Dependabot-signed original"):
        run(merged, push_evidence, squash, _github_payload(bot["oid"]), human)

    advanced = copy.deepcopy(squash)
    landing = advanced["data"]["repository"]["object"]
    landing["parents"]["nodes"] = [{"oid": "7" * 40, "tree": {"oid": "6" * 40}}]
    landing["tree"] = {"oid": "5" * 40}
    file = {"path": "lock", "type": "blob", "mode": "100644", "sha": "4" * 40}
    extra = {**file, "path": "unendorsed"}
    responses = [{"sha": oid * 40, "truncated": False, "tree": entries}
                 for oid, entries in (("8", []), ("f", [file]), ("6", []), ("5", [file, extra]))]
    with pytest.raises(ReviewCommitAuditError, match="squash tree"):
        run(merged, push_evidence, advanced, *responses)


def test_dependabot_squash_delta_compares_exact_paths_modes_and_objects(monkeypatch):
    def entry(path, sha, mode="100644", kind="blob"):
        return {"path": path, "sha": sha * 40, "mode": mode, "type": kind}

    before = [entry("bin", "1", "040000", "tree"), entry("bin/tool", "2")]
    after = [entry("bin", "3", "040000", "tree"), entry("bin/tool", "4", "100755")]
    new_base = [entry("bin", "5", "040000", "tree"), before[1], entry("bin/other", "6")]
    landing = [entry("bin", "7", "040000", "tree"), after[1], new_base[2]]

    def delta(old, new):
        _github_responses(monkeypatch, [
            {"sha": "a" * 40, "truncated": False, "tree": old},
            {"sha": "b" * 40, "truncated": False, "tree": new},
        ])
        return audit._tree_delta("owner/repo", "a" * 40, "b" * 40)

    expected = delta(before, after)
    assert expected == {"bin/tool": (("100644", "2" * 40), ("100755", "4" * 40))}
    assert delta(new_base, landing) == expected
    assert delta(new_base, [*landing, entry("extra", "9", "120000")]) != expected
    assert delta(new_base, [*landing, entry("sub", "9", "160000", "commit")]) != expected
    assert delta(new_base, [landing[0], {**landing[1], "mode": "100644"}, landing[2]]) != expected
    assert delta([new_base[0], entry("bin/tool", "9"), new_base[2]], landing) != expected
    for payload in ({"sha": "a" * 40, "truncated": True, "tree": []},
                    {"sha": "b" * 40, "truncated": False, "tree": []},
                    {"sha": "a" * 40, "truncated": False, "tree": before * 2}):
        _github_responses(monkeypatch, [payload])
        with pytest.raises(ReviewCommitAuditError, match="complete Git tree"):
            audit._tree_delta("owner/repo", "a" * 40, "b" * 40)


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


def test_opt_in_local_lookup_verifies_bot_and_unpublished_maintainer(monkeypatch, tmp_path):
    rest, graph = _dependabot_metadata()
    bot, human = rest["sha"], "b" * 40
    _git_history(monkeypatch, (bot, human))
    default_git = audit._git

    def git(root, *arguments):
        if arguments[0] == "show" and arguments[-1] == bot:
            return "\0".join((bot, rest["author"]["name"], rest["author"]["email"], "E", rest["message"]))
        if arguments[0] == "show":
            commit = _commit()
            return "\0".join((human, commit.author_name, commit.author_email, "G", commit.message))
        return default_git(root, *arguments)

    monkeypatch.setattr(audit, "_git", git)
    monkeypatch.setenv("ZLANG_DEPENDABOT_REPOSITORY", "owner/repo")
    calls = _github_responses(monkeypatch, [rest, graph])
    assert audit.audit_review_commits(tmp_path, base="base") == (bot, human)
    assert len(calls) == 2
    unsigned = {**rest, "verification": {"verified": False, "reason": "unsigned"}}
    _github_responses(monkeypatch, [unsigned])
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
