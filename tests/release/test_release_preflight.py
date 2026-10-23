# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
from __future__ import annotations

import hashlib
import json
import importlib.util
import os
from pathlib import Path
import re
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
from tools.release_status import _example_counts
from tools.documentation_manifest import load_manuals


ROOT = Path(__file__).resolve().parents[2]


def _git(
    root: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )
    return completed.stdout.strip()


def _release_commit_environment(root: Path) -> dict[str, str]:
    """Bind fixture commits to the candidate date and Brisbane offset."""

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(
        r"^## [0-9]+\.[0-9]+\.[0-9]+a[0-9]+ — ([0-9]{4}-[0-9]{2}-[0-9]{2})$",
        changelog,
        flags=re.MULTILINE,
    )
    assert match is not None
    timestamp = f"{match.group(1)}T00:30:00+10:00"
    return {
        **os.environ,
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_DATE": timestamp,
    }


@pytest.fixture(scope="module")
def release_repository(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Give preflight a real history without requiring history in release archives."""

    root = tmp_path_factory.mktemp("release-preflight") / "candidate"
    projector_path = ROOT / "tools/public_tree.py"
    if projector_path.is_file():
        spec = importlib.util.spec_from_file_location(
            "release_preflight_public_tree", projector_path
        )
        assert spec is not None and spec.loader is not None
        public_tree = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = public_tree
        spec.loader.exec_module(public_tree)
        public_tree.export_tree(ROOT, root)
    else:
        # In the history-isolated public checkout ROOT already is the complete
        # reviewed release tree. Projector policy remains release-host-only.
        shutil.copytree(
            ROOT,
            root,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", "__pycache__", "build", "node_modules"
            ),
        )
    changelog = root / "CHANGELOG.md"
    changelog_text = changelog.read_text(encoding="utf-8")
    unreleased_start = changelog_text.index("## Unreleased")
    release_start = changelog_text.index("## 0.1.0a24", unreleased_start)
    changelog_text = (
        changelog_text[:unreleased_start]
        + "## Unreleased\n\n"
        + changelog_text[release_start:]
    )
    changelog.write_text(changelog_text, encoding="utf-8")
    status_path = root / "release/status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    grammar = root / "editors/vscode/zlang-hdl/syntaxes/zlang.tmLanguage.json"
    for record in status["documentation"]["manuals"].values():
        record["syntax_grammar_sha256"] = hashlib.sha256(grammar.read_bytes()).hexdigest()
    # The release-status owner defines the exact PDF source projection.  Keep
    # this synthetic repository aligned with that owner instead of maintaining
    # a second list that silently becomes stale when the reference is split.
    for manual in load_manuals(root):
        record = status["documentation"]["manuals"][manual.identity]
        record["sources"] = {
            path.as_posix(): hashlib.sha256((root / path).read_bytes()).hexdigest()
            for path in (manual.entrypoint, *manual.sources)
        }
    status["validation"]["example_corpus"] = _example_counts(root)
    status_path.write_text(
        json.dumps(status, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Release Preflight Test")
    _git(root, "config", "user.email", "release-preflight@example.invalid")
    _git(root, "add", "-A")
    _git(
        root,
        "-c", "commit.gpgsign=false", "commit", "-qm", "bootstrap candidate",
        env=_release_commit_environment(root),
    )
    _git(root, "branch", "-M", "main")
    _git(
        root,
        "tag", "-a", "v0.1.0a23", "-m", "fixture previous public release",
        env=_release_commit_environment(root),
    )
    _git(
        root,
        "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm",
        "a24 candidate",
        env=_release_commit_environment(root),
    )
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def test_current_candidate_binds_release_sources_native_wheel_and_git(
    release_repository: Path,
) -> None:
    report = preflight(
        release_repository,
        tag="v0.1.0a24",
        previous_tag="v0.1.0a23",
        mode="candidate",
        require_clean=False,
    )
    assert report["schema"] == 4
    assert report["version"] == "0.1.0a24"
    assert report["tag"] == "v0.1.0a24"
    assert report["previous_tag"] == "v0.1.0a23"
    assert report["regressions"]["included"] == [
        "CI-001",
        "CI-002",
        "DOC-001",
        "FORMAL-001",
        "FORMAL-002",
        "FORMAL-003",
        "FORMAL-004",
        "FORMAL-005",
        "LSP-001",
        "LSP-002",
        "SIM-001",
        "SIM-002",
        "SIM-003",
        "SIM-004",
        "SIM-005",
        "VSIX-001",
        "VSIX-002",
        "VSIX-003",
        "VSIX-004",
        "ZL-052",
        "ZL-053",
        "ZL-054",
        "ZL-055",
        "ZL-056",
        "ZL-057",
        "ZL-058",
        "ZL-059",
        "ZL-060",
        "ZL-061",
        "ZL-062",
        "ZL-063",
        "ZL-064",
        "ZL-065",
        "ZL-066",
        "ZL-067",
        "ZL-068",
    ]
    assert report["dependency_floor"]["schema"] == 1
    assert report["dependency_floor"]["previous_ref"] == "v0.1.0a23"
    assert report["dependency_floor"]["compared"] > 0
    assert report["dependency_floor"]["downgrades"] == []
    assert report["regressions"]["entries"] == 36
    assert report["regressions"]["dispositions"] == {
        "deferred": 0,
        "excluded_experiment": 0,
        "included": 36,
        "private_only": 0,
    }
    assert report["identities"]["release/regressions.json"] == hashlib.sha256(
        (release_repository / "release/regressions.json").read_bytes()
    ).hexdigest()
    assert report["git"]["previous_commit"]
    assert report["native_wheels"] == [
        {
            "file": "zlang_native_sim-0.1.0a24-cp312-abi3-manylinux_2_28_x86_64.whl",
            "platform": "linux_x86_64",
            "sha256": "6e17c01f69fe39e2081fb7c4dcd73cc13c63e7dcd23d2803107b019a2df5c571",
            "version": "0.1.0a24",
        }
    ]


@pytest.mark.parametrize("timezone", ("UTC", "Australia/Brisbane"))
def test_candidate_date_uses_the_timezone_recorded_in_the_commit(
    release_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    timezone: str,
) -> None:
    monkeypatch.setenv("TZ", timezone)
    report = preflight(
        release_repository,
        tag="v0.1.0a24",
        previous_tag="v0.1.0a23",
        mode="candidate",
        require_clean=False,
    )
    assert report["version"] == "0.1.0a24"


@pytest.mark.parametrize("identity", ("CI-002", "DOC-001"))
def test_candidate_rejects_missing_documentation_or_security_regression(
    release_repository: Path, identity: str,
) -> None:
    ledger = release_repository / "release/regressions.json"
    original = ledger.read_bytes()
    payload = json.loads(original)
    entry = next(entry for entry in payload["entries"] if entry["id"] == identity)
    entry["tests"][0] += "_missing"
    try:
        ledger.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ReleasePreflightError, match="does not resolve"):
            preflight(
                release_repository,
                tag="v0.1.0a24",
                previous_tag="v0.1.0a23",
                mode="candidate",
                require_clean=False,
            )
    finally:
        ledger.write_bytes(original)


@pytest.mark.parametrize(
    ("tag", "previous", "message"),
    (
        ("v0.1.0a24", "v0.1.0a21", "immediately follow"),
        ("v0.2.0a1", "v0.1.0a22", "different bases"),
        ("v0.1.0", "v0.1.0a22", "alpha tag"),
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
            tag="v0.1.0a24",
            previous_tag="v0.1.0a23",
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
                tag="v0.1.0a24",
                previous_tag="v0.1.0a23",
                mode="candidate",
                require_clean=False,
            )
    finally:
        _git(release_repository, "switch", "main")


def test_previous_tag_is_required_for_nonbootstrap_candidate(
    release_repository: Path,
) -> None:
    original = _git(release_repository, "rev-parse", "HEAD")
    marker = release_repository / ".second-commit"
    marker.write_text("not a bootstrap\n", encoding="utf-8")
    _git(release_repository, "add", marker.name)
    _git(
        release_repository,
        "-c", "commit.gpgsign=false", "commit", "-qm", "second commit",
        env=_release_commit_environment(release_repository),
    )
    _git(release_repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    try:
        with pytest.raises(ReleasePreflightError, match="baseline does not match"):
            preflight(
                release_repository,
                tag="v0.1.0a24",
                previous_tag=None,
                mode="candidate",
                require_clean=False,
            )
    finally:
        _git(release_repository, "reset", "--hard", original)
        _git(release_repository, "update-ref", "refs/remotes/origin/main", original)


def test_cli_writes_stable_sorted_json(
    tmp_path: Path, release_repository: Path,
) -> None:
    output = tmp_path / "preflight.json"
    assert main([
        "--root", str(release_repository),
        "--tag", "v0.1.0a24",
        "--previous-tag", "v0.1.0a23",
        "--output", str(output),
    ]) == 0
    payload = output.read_text(encoding="utf-8")
    assert payload.endswith("\n")
    assert json.loads(payload)["mode"] == "candidate"
    assert payload == json.dumps(json.loads(payload), indent=2, sort_keys=True) + "\n"
