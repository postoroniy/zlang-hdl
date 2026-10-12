# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
from __future__ import annotations

import hashlib
import json
from datetime import date
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
from tools.release_status import _example_counts


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
    """Give preflight an isolated one-commit bootstrap repository."""

    root = tmp_path_factory.mktemp("release-preflight") / "candidate"
    shutil.copytree(
        ROOT,
        root,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "build",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
        ),
    )
    changelog = root / "CHANGELOG.md"
    changelog_text = changelog.read_text(encoding="utf-8")
    unreleased_start = changelog_text.index("## Unreleased")
    release_start = changelog_text.index("## 0.1.0a23", unreleased_start)
    changelog_text = (
        changelog_text[:unreleased_start]
        + "## Unreleased\n\n"
        + changelog_text[release_start:]
    )
    changelog_text = changelog_text.replace(
        "## 0.1.0a23 — 2026-10-12",
        f"## 0.1.0a23 — {date.today().isoformat()}",
    )
    changelog.write_text(changelog_text, encoding="utf-8")
    status_path = root / "release/status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    grammar = root / "editors/vscode/zlang-hdl/syntaxes/zlang.tmLanguage.json"
    status["documentation"]["syntax_grammar_sha256"] = hashlib.sha256(
        grammar.read_bytes()
    ).hexdigest()
    for source in (
        "docs/language-reference.md",
        "docs/language-quick-reference.md",
    ):
        status["documentation"]["sources"][source] = hashlib.sha256(
            (root / source).read_bytes()
        ).hexdigest()
    status["validation"]["example_corpus"] = _example_counts(root)
    status_path.write_text(
        json.dumps(status, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Release Preflight Test")
    _git(root, "config", "user.email", "release-preflight@example.invalid")
    _git(root, "add", "-A")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "bootstrap candidate")
    _git(root, "branch", "-M", "main")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def test_current_candidate_binds_release_sources_native_wheel_and_git(
    release_repository: Path,
) -> None:
    report = preflight(
        release_repository,
        tag="v0.1.0a23",
        mode="candidate",
        require_clean=False,
    )
    assert report["schema"] == 4
    assert report["version"] == "0.1.0a23"
    assert report["tag"] == "v0.1.0a23"
    assert "previous_release" not in report
    assert report["regressions"]["included"] == [
        "REL-005",
        "ZL-049",
        "ZL-050",
    ]
    assert "release/regressions.json" in report["identities"]
    assert report["native_wheels"] == [
        {
            "file": "zlang_native_sim-0.1.0a23-cp312-abi3-manylinux_2_28_x86_64.whl",
            "platform": "linux_x86_64",
            "sha256": "e48a5ab189484600e16c41ce5c03bc2612c0cbe70b16571aed4774305e97af49",
            "version": "0.1.0a23",
        }
    ]


@pytest.mark.parametrize(
    ("tag", "previous", "message"),
    (
        ("v0.1.0a9", "v0.1.0a7", "immediately follow"),
        ("v0.2.0a1", "v0.1.0a8", "different bases"),
        ("v0.1.0", "v0.1.0a8", "alpha tags"),
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
            tag="v0.1.0a23",
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
                tag="v0.1.0a23",
                mode="candidate",
                require_clean=False,
            )
    finally:
        _git(release_repository, "switch", "main")


def test_cli_writes_stable_sorted_json(
    tmp_path: Path,
    release_repository: Path,
) -> None:
    output = tmp_path / "preflight.json"
    assert (
        main(
            [
                "--root",
                str(release_repository),
                "--tag",
                "v0.1.0a23",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    payload = output.read_text(encoding="utf-8")
    assert payload.endswith("\n")
    assert json.loads(payload)["mode"] == "candidate"
    assert payload == json.dumps(json.loads(payload), indent=2, sort_keys=True) + "\n"


def test_bootstrap_without_a_prior_tag_requires_exactly_one_commit(
    release_repository: Path,
) -> None:
    marker = release_repository / ".second-commit"
    marker.write_text("not a bootstrap\n", encoding="utf-8")
    _git(release_repository, "add", marker.name)
    _git(
        release_repository,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "second commit",
    )
    _git(release_repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    try:
        with pytest.raises(ReleasePreflightError, match="one-commit repository"):
            preflight(
                release_repository,
                tag="v0.1.0a23",
                mode="candidate",
                require_clean=False,
            )
    finally:
        _git(release_repository, "reset", "--hard", "HEAD^")
        _git(release_repository, "update-ref", "refs/remotes/origin/main", "HEAD")
