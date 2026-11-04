# SPDX-License-Identifier: Apache-2.0
"""Publication denial tests independent of filenames and final-tree cleanliness."""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from tools.community_boundary import (
    BoundaryError,
    body_fingerprints,
    check_member,
    load_policy,
    validate_artifact,
    validate_history,
    validate_tree,
)

ROOT = Path(__file__).resolve().parents[2]


def policy():
    return load_policy(ROOT / "release/community-boundary.json")


@pytest.mark.parametrize(
    "source",
    [
        "import zlang.dft_pipeline",
        "from zlang import dft_pipeline",
        "def deferred():\n import zlang.hardware_debug_catalog",
        'import importlib\ndef deferred():\n return importlib.import_module("zlang."+"dft_pipeline")',
        'def deferred():\n return __import__("zlang.dft_scan")',
    ],
)
def test_private_imports_fail_even_when_lazy(source):
    with pytest.raises(BoundaryError, match="private dependency"):
        check_member("zlang/renamed.py", source.encode(), policy())


def test_private_implementation_under_renamed_file(tmp_path):
    # A synthetic nontrivial body isolates fingerprint semantics without
    # embedding a real private algorithm in the Community regression suite.
    source = (
        "def original(x):\n"
        + "".join(
            "    x = (x + " + str(i) + ") ^ " + str(i * 7) + "\n" for i in range(20)
        )
        + "    return x\n"
    )
    p = policy()
    p["python_body_sha256"] = sorted(body_fingerprints(source))
    with pytest.raises(BoundaryError, match="fingerprint"):
        check_member(
            "zlang/innocent.py", source.replace("original", "renamed").encode(), p
        )


@pytest.mark.parametrize(
    "name,source",
    [
        ("zlang/cli.py", 'def main(argv):\n if argv[0] == "trace": return 0\n'),
        (
            "zlang/implementation_request.py",
            'allowed = {"backend", "hardware-debug"}\n',
        ),
        ("docs/renamed.md", "# Helpers\nimport std.debug.trace\n"),
        ("zlang/formal.py", "def run_verilog_formal(*, multiclock=False): return 0\n"),
    ],
)
def test_unavailable_surface_and_documentation_fail(name, source):
    with pytest.raises(BoundaryError):
        check_member(name, source.encode(), policy())


@pytest.mark.parametrize(
    "member",
    [
        "zlang/dft_pipeline.py",
        "package.data/data/stdlib/debug/trace.zhl",
        "extension/zlang/hardware_debug_host.py",
    ],
)
def test_artifact_contents_are_checked(tmp_path, member):
    artifact = tmp_path / "package.whl"
    with zipfile.ZipFile(artifact, "w") as z:
        z.writestr(member, b"# implementation")
    with pytest.raises(BoundaryError):
        validate_artifact(artifact, policy())


def test_missing_or_incompatible_policy_fails_closed(tmp_path):
    with pytest.raises(BoundaryError):
        validate_tree(tmp_path)
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"schema": 99}))
    with pytest.raises(BoundaryError):
        load_policy(p)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Boundary Fixture")
    git(tmp_path, "config", "user.email", "boundary@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "README").write_text("public baseline")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "public fixture baseline")
    p = policy()
    p["public_base"] = git(tmp_path, "rev-parse", "HEAD")
    return p


def test_private_commit_in_history_rejects_even_after_deletion(tmp_path):
    p = repo(tmp_path)
    file = tmp_path / "zlang/dft_insert\n.py"
    file.parent.mkdir()
    file.write_text("VALUE=1")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "unsafe fixture")
    file.unlink()
    git(tmp_path, "add", "-u")
    git(tmp_path, "commit", "-qm", "remove fixture")
    with pytest.raises(BoundaryError):
        validate_history(tmp_path, p)


def test_private_object_not_reachable_is_not_public_history(tmp_path):
    p = repo(tmp_path)
    base = p["public_base"]
    git(tmp_path, "checkout", "-qb", "private-fixture")
    (tmp_path / "ordinary").write_text("private fixture")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "private fixture")
    bad = git(tmp_path, "rev-parse", "HEAD")
    p["denied_commits"] = [bad]
    with pytest.raises(BoundaryError, match="private-only commit"):
        validate_history(tmp_path, p)
    git(tmp_path, "checkout", "--detach", base)
    assert validate_history(tmp_path, p) == 0


def test_executable_and_compiled_python_are_not_silently_accepted(tmp_path):
    for name, data in [("lib/new.so", b"\x7fELFbad"), ("zlang/cache.pyc", b"compiled")]:
        with pytest.raises(BoundaryError):
            check_member(name, data, policy())
    p = policy()
    p["reviewed_binary_sha256"] = [sha256(b"\x7fELFgood").hexdigest()]
    check_member("lib/existing.so", b"\x7fELFgood", p)


def test_production_source_symlink_is_rejected(tmp_path):
    (tmp_path / "release").mkdir()
    (tmp_path / "release/community-boundary.json").write_text(json.dumps(policy()))
    (tmp_path / "zlang").mkdir()
    target = tmp_path / "ordinary.txt"
    target.write_text("VALUE=1")
    (tmp_path / "zlang/alias.py").symlink_to(target)
    with pytest.raises(BoundaryError, match="symlink"):
        validate_tree(tmp_path)


@pytest.mark.parametrize("depth", (1, 2))
def test_shallow_history_cannot_hide_an_older_denied_commit(tmp_path, depth):
    source = tmp_path / "source"
    source.mkdir()
    p = repo(source)
    denied = p["public_base"]
    (source / "README").write_text("reviewed baseline")
    git(source, "commit", "-qam", "reviewed fixture baseline")
    p["public_base"] = git(source, "rev-parse", "HEAD")
    (source / "README").write_text("public candidate")
    git(source, "commit", "-qam", "public fixture candidate")
    p["denied_commits"] = [denied]
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", str(depth), source.as_uri(), str(shallow))
    assert git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    with pytest.raises(BoundaryError, match="complete Git history"):
        validate_history(shallow, p)
    git(shallow, "fetch", "-q", "--unshallow", "origin")
    with pytest.raises(BoundaryError, match="private-only commit"):
        validate_history(shallow, p)
    p["denied_commits"] = []
    assert validate_history(shallow, p) == 1


def _github_merge(tmp_path, monkeypatch, *, nested=False, removed_private=False):
    p = repo(tmp_path)
    git(tmp_path, "branch", "-M", "main")
    git(tmp_path, "checkout", "-qb", "feature")
    (tmp_path / "feature.txt").write_text("feature\n")
    git(tmp_path, "add", "feature.txt")
    git(tmp_path, "commit", "-qm", "feature source")
    if removed_private:
        private = tmp_path / "zlang/dft_pipeline.py"
        private.parent.mkdir()
        private.write_text("VALUE=1\n")
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "-qm", "unavailable owner")
        private.unlink()
        git(tmp_path, "commit", "-qam", "remove owner")
    if nested:
        git(tmp_path, "checkout", "-qb", "side", p["public_base"])
        (tmp_path / "side.txt").write_text("side\n")
        git(tmp_path, "add", "side.txt")
        git(tmp_path, "commit", "-qm", "side source")
        git(tmp_path, "checkout", "feature")
        git(tmp_path, "merge", "--no-ff", "-qm", "real source merge", "side")
    head = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "main")
    (tmp_path / "base.txt").write_text("base\n")
    git(tmp_path, "add", "base.txt")
    git(tmp_path, "commit", "-qm", "base source")
    base = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "merge", "--no-ff", "-qm", "integration product", "feature")
    merge = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "remote", "add", "origin", "https://github.com/public/example.git")
    event = {"number": 7, "repository": {"full_name": "public/example"},
             "pull_request": {"base": {"sha": base, "ref": "main",
                                        "repo": {"full_name": "public/example"}},
                              "head": {"sha": head}}}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    for key, value in {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request",
                       "GITHUB_WORKSPACE": str(tmp_path), "GITHUB_EVENT_PATH": str(event_path),
                       "GITHUB_REPOSITORY": "public/example", "GITHUB_REF": "refs/pull/7/merge",
                       "GITHUB_SHA": merge}.items():
        monkeypatch.setenv(key, value)
    return p, event, event_path


def test_exact_github_integration_checks_both_histories(tmp_path, monkeypatch):
    p, _, _ = _github_merge(tmp_path, monkeypatch)
    assert validate_history(tmp_path, p) == 3
    # Publication retains the ordinary ban on source-history merges.
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    with pytest.raises(BoundaryError, match="unreviewed merge"):
        validate_history(tmp_path, p)


@pytest.mark.parametrize("mutation", (
    "missing_event", "number", "repository", "base_repository", "head", "swapped",
    "sha", "ref", "origin", "wrong_tree",
))
def test_github_integration_context_fails_closed(tmp_path, monkeypatch, mutation):
    p, event, path = _github_merge(tmp_path, monkeypatch)
    request = event["pull_request"]
    if mutation == "missing_event":
        path.unlink()
    elif mutation == "number":
        event["number"] = 8
    elif mutation == "repository":
        event["repository"]["full_name"] = "different/repository"
    elif mutation == "base_repository":
        request["base"]["repo"]["full_name"] = "different/repository"
    elif mutation == "head":
        request["head"]["sha"] = p["public_base"]
    elif mutation == "swapped":
        request["base"]["sha"], request["head"]["sha"] = request["head"]["sha"], request["base"]["sha"]
    elif mutation == "sha":
        monkeypatch.setenv("GITHUB_SHA", p["public_base"])
    elif mutation == "ref":
        monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    elif mutation == "origin":
        git(tmp_path, "remote", "set-url", "origin", "https://github.com/different/repository.git")
    else:
        (tmp_path / "integration-only.txt").write_text("unreviewed integration change\n")
        git(tmp_path, "add", "integration-only.txt")
        tree = git(tmp_path, "write-tree")
        altered = git(tmp_path, "commit-tree", tree, "-p", request["base"]["sha"],
                      "-p", request["head"]["sha"], "-m", "altered integration")
        git(tmp_path, "update-ref", "HEAD", altered)
        monkeypatch.setenv("GITHUB_SHA", altered)
    if mutation != "missing_event":
        path.write_text(json.dumps(event))
    with pytest.raises(BoundaryError):
        validate_history(tmp_path, p)


@pytest.mark.parametrize("mutation", ("denied_base", "denied_head", "nested_merge", "removed_private"))
def test_github_integration_preserves_source_ancestry_and_content_denials(tmp_path, monkeypatch, mutation):
    p, event, _ = _github_merge(tmp_path, monkeypatch, nested=mutation == "nested_merge",
                              removed_private=mutation == "removed_private")
    if mutation == "denied_base":
        p["denied_commits"] = [event["pull_request"]["base"]["sha"]]
    elif mutation == "denied_head":
        p["denied_commits"] = [event["pull_request"]["head"]["sha"]]
    with pytest.raises(BoundaryError):
        validate_history(tmp_path, p)
