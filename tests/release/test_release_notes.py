# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
from __future__ import annotations

from pathlib import Path

import pytest

from tools.release_notes import release_notes


ROOT = Path(__file__).resolve().parents[2]


def test_current_release_notes_are_curated_from_exact_changelog_section() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    notes = release_notes(changelog, "v0.1.0a20")
    unreleased = changelog.split("## Unreleased", 1)[1].split("## 0.1.0a20", 1)[0]

    assert not unreleased.strip()
    assert "## 0.1.0a20 — 2026-10-08" in changelog
    assert "Runtime packed-bit indexing" in notes
    assert "target candidate-provider boundary" in notes
    assert "one shared multiplier" in notes
    assert "capacity one, latency four and II four" in notes
    assert "ABI v11" in notes
    assert "general HLS scheduler" in notes
    assert "ABI-v10" not in notes
    assert "0.1.0a19 —" not in notes


def test_release_notes_reject_missing_duplicate_and_empty_sections() -> None:
    with pytest.raises(ValueError, match="invalid release tag"):
        release_notes("## 1.2.3\nnotes\n", "latest")
    with pytest.raises(ValueError, match="found 0"):
        release_notes("# Changes\n", "v1.2.3")
    with pytest.raises(ValueError, match="found 2"):
        release_notes("## 1.2.3\na\n## 1.2.3\nb\n", "v1.2.3")
    with pytest.raises(ValueError, match="is empty"):
        release_notes("## 1.2.3\n\n## 1.2.2\nold\n", "v1.2.3")
