# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tools.release_preflight import (
    ReleasePreflightError,
    _alpha_sequence,
    main,
    preflight,
)


ROOT = Path(__file__).resolve().parents[2]


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture(scope="module")
def release_repository(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Give preflight a real history without requiring history in release archives."""

    root = tmp_path_factory.mktemp("release-preflight") / "candidate"
    shutil.copytree(
        ROOT,
        root,
        ignore=shutil.ignore_patterns(
            ".git", ".pytest_cache", ".vscode-test", "__pycache__",
            "_zlang_native_sim", "build", "dist", "node_modules", "target",
        ),
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Release Preflight Test")
    _git(root, "config", "user.email", "release-preflight@example.invalid")
    marker = root / ".release-base"
    marker.write_text("v0.1.0a18\n", encoding="utf-8")
    _git(root, "add", marker.name)
    _git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "previous release")
    _git(root, "tag", "-a", "v0.1.0a18", "-m", "previous release")
    _git(root, "add", "-A")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "candidate")
    return root


def test_current_candidate_binds_release_sources_native_wheel_and_git(
    release_repository: Path,
) -> None:
    report = preflight(
        release_repository,
        tag="v0.1.0a19",
        previous_tag="v0.1.0a18",
        mode="candidate",
        require_clean=False,
    )
    assert report["schema"] == 2
    assert report["version"] == "0.1.0a19"
    assert report["tag"] == "v0.1.0a19"
    assert report["previous_tag"] == "v0.1.0a18"
    assert report["regressions"]["included"] == [
        "ZL-039",
        "ZL-040",
        "ZL-041",
        "ZL-042",
        "ZL-043",
        "ZL-044",
        "ZL-045",
    ]
    assert "release/regressions.json" in report["identities"]
    assert report["git"]["previous_commit"] == _git(
        release_repository, "rev-list", "-n", "1", "v0.1.0a18"
    )
    assert report["native_wheels"] == [
        {
            "file": "zlang_native_sim-0.1.0a19-cp312-abi3-manylinux_2_28_x86_64.whl",
            "platform": "linux_x86_64",
            "sha256": "c62f1ace4068109e0d67fa92891d2bf5808a9ce1a6fed943232e6a05eccf19d8",
            "version": "0.1.0a19",
        }
    ]


def test_hosted_candidate_requires_selected_protected_main(
    release_repository: Path,
) -> None:
    head = _git(release_repository, "rev-parse", "HEAD")
    _git(release_repository, "update-ref", "refs/remotes/origin/main", head)
    report = preflight(
        release_repository,
        tag="v0.1.0a19",
        previous_tag="v0.1.0a18",
        mode="candidate",
        require_clean=False,
        selected_ref="main",
        protected_main_ref="origin/main",
    )
    assert report["git"]["commit"] == head


def test_hosted_candidate_rejects_non_main_selected_ref(
    release_repository: Path,
) -> None:
    head = _git(release_repository, "rev-parse", "HEAD")
    _git(release_repository, "update-ref", "refs/remotes/origin/main", head)
    with pytest.raises(ReleasePreflightError, match="select the 'main' branch"):
        preflight(
            release_repository,
            tag="v0.1.0a19",
            previous_tag="v0.1.0a18",
            mode="candidate",
            require_clean=False,
            selected_ref="feature",
            protected_main_ref="origin/main",
        )


def test_hosted_candidate_rejects_head_not_at_protected_main(
    tmp_path: Path, release_repository: Path,
) -> None:
    root = tmp_path / "non-main-candidate"
    shutil.copytree(release_repository, root)
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD^")
    with pytest.raises(ReleasePreflightError, match="does not match protected"):
        preflight(
            root,
            tag="v0.1.0a19",
            previous_tag="v0.1.0a18",
            mode="candidate",
            require_clean=False,
            selected_ref="main",
            protected_main_ref="origin/main",
        )


@pytest.mark.parametrize(
    ("tag", "previous", "message"),
    (
        ("v0.1.0a19", "v0.1.0a17", "immediately follow"),
        ("v0.2.0a1", "v0.1.0a18", "different bases"),
        ("v0.1.0", "v0.1.0a18", "alpha tags"),
    ),
)
def test_alpha_sequence_is_explicit_and_monotonic(
    tag: str, previous: str, message: str
) -> None:
    with pytest.raises(ReleasePreflightError, match=message):
        _alpha_sequence(tag, previous)


def test_tagged_mode_rejects_an_absent_or_lightweight_release_tag(
    release_repository: Path,
) -> None:
    with pytest.raises(ReleasePreflightError, match="annotated tag"):
        preflight(
            release_repository,
            tag="v0.1.0a19",
            previous_tag="v0.1.0a18",
            mode="tagged",
            require_clean=False,
        )


def test_cli_writes_stable_sorted_json(
    tmp_path: Path, release_repository: Path,
) -> None:
    output = tmp_path / "preflight.json"
    assert main([
        "--root", str(release_repository),
        "--tag", "v0.1.0a19",
        "--previous-tag", "v0.1.0a18",
        "--output", str(output),
    ]) == 0
    payload = output.read_text(encoding="utf-8")
    assert payload.endswith("\n")
    assert json.loads(payload)["mode"] == "candidate"
    assert payload == json.dumps(json.loads(payload), indent=2, sort_keys=True) + "\n"
