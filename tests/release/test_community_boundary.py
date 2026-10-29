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
