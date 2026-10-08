# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

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
    spec = importlib.util.spec_from_file_location(
        "release_preflight_public_tree", ROOT / "tools/public_tree.py"
    )
    assert spec is not None and spec.loader is not None
    public_tree = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = public_tree
    spec.loader.exec_module(public_tree)
    public_tree.export_tree(ROOT, root)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Release Preflight Test")
    _git(root, "config", "user.email", "release-preflight@example.invalid")
    marker = root / ".release-base"
    marker.write_text("v0.1.0a19\n", encoding="utf-8")
    _git(root, "add", marker.name)
    _git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "previous release")
    _git(root, "tag", "-a", "v0.1.0a19", "-m", "previous release")
    _git(root, "add", "-A")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "candidate")
    _git(root, "branch", "-M", "main")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def test_current_candidate_binds_release_sources_native_wheel_and_git(
    release_repository: Path,
) -> None:
    report = preflight(
        release_repository,
        tag="v0.1.0a20",
        previous_tag="v0.1.0a19",
        mode="candidate",
        require_clean=False,
    )
    assert report["schema"] == 2
    assert report["version"] == "0.1.0a20"
    assert report["tag"] == "v0.1.0a20"
    assert report["previous_tag"] == "v0.1.0a19"
    assert report["regressions"]["included"] == [
        "ZL-046",
        "ZL-047",
        "ZL-048",
        "ZL-049",
        "ZL-050",
    ]
    assert "release/regressions.json" in report["identities"]
    assert report["git"]["previous_commit"] == _git(
        release_repository, "rev-list", "-n", "1", "v0.1.0a19"
    )
    assert report["native_wheels"] == [
        {
            "file": "zlang_native_sim-0.1.0a20-cp312-abi3-manylinux_2_28_x86_64.whl",
            "platform": "linux_x86_64",
            "sha256": "23d6b4bd436ad8d5d9a10031ceb43e45a2a5fd8db0eaee2215bd204fed1199a3",
            "version": "0.1.0a20",
        }
    ]


def test_hosted_candidate_requires_selected_protected_main(
    release_repository: Path,
) -> None:
    head = _git(release_repository, "rev-parse", "HEAD")
    _git(release_repository, "update-ref", "refs/remotes/origin/main", head)
    report = preflight(
        release_repository,
        tag="v0.1.0a20",
        previous_tag="v0.1.0a19",
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
            tag="v0.1.0a20",
            previous_tag="v0.1.0a19",
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
            tag="v0.1.0a20",
            previous_tag="v0.1.0a19",
            mode="candidate",
            require_clean=False,
            selected_ref="main",
            protected_main_ref="origin/main",
        )


@pytest.mark.parametrize(
    ("tag", "previous", "message"),
    (
        ("v0.1.0a20", "v0.1.0a18", "immediately follow"),
        ("v0.2.0a1", "v0.1.0a19", "different bases"),
        ("v0.1.0", "v0.1.0a19", "alpha tags"),
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
            tag="v0.1.0a20",
            previous_tag="v0.1.0a19",
            mode="tagged",
            require_clean=False,
        )


def test_candidate_mode_rejects_a_non_main_checkout(
    release_repository: Path,
) -> None:
    _git(release_repository, "switch", "-c", "release-candidate")
    try:
        with pytest.raises(ReleasePreflightError, match="checked-out branch 'main'"):
            preflight(
                release_repository,
                tag="v0.1.0a20",
                previous_tag="v0.1.0a19",
                mode="candidate",
                require_clean=False,
            )
    finally:
        _git(release_repository, "switch", "main")


def test_cli_writes_stable_sorted_json(
    tmp_path: Path, release_repository: Path,
) -> None:
    output = tmp_path / "preflight.json"
    assert main([
        "--root", str(release_repository),
        "--tag", "v0.1.0a20",
        "--previous-tag", "v0.1.0a19",
        "--output", str(output),
    ]) == 0
    payload = output.read_text(encoding="utf-8")
    assert payload.endswith("\n")
    assert json.loads(payload)["mode"] == "candidate"
    assert payload == json.dumps(json.loads(payload), indent=2, sort_keys=True) + "\n"
