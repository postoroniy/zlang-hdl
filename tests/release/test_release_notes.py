# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tools.release_notes import release_notes, validate_release_changelog


ROOT = Path(__file__).resolve().parents[2]


def test_current_release_notes_are_curated_from_exact_changelog_section() -> None:
    notes = release_notes(
        (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), "v0.1.0a21"
    )
    assert "registered outputs" in notes
    assert "transaction-stream BMC" in notes
    assert "bounded, deterministic exact-value" in notes
    assert "ABI-v10" not in notes
    assert "0.1.0a10 —" not in notes


def test_release_notes_reject_missing_duplicate_and_empty_sections() -> None:
    with pytest.raises(ValueError, match="invalid release tag"):
        release_notes("## 1.2.3\nnotes\n", "latest")
    with pytest.raises(ValueError, match="found 0"):
        release_notes("# Changes\n", "v1.2.3")
    with pytest.raises(ValueError, match="found 2"):
        release_notes("## 1.2.3\na\n## 1.2.3\nb\n", "v1.2.3")
    with pytest.raises(ValueError, match="is empty"):
        release_notes("## 1.2.3\n\n## 1.2.2\nold\n", "v1.2.3")


def test_final_changelog_requires_empty_unreleased_and_exact_candidate_date() -> None:
    valid = "## Unreleased\n\n## 1.2.3 — 2026-10-10\n\n- shipped\n"
    assert validate_release_changelog(
        valid,
        "v1.2.3",
        expected_date=date(2026, 10, 10),
    ) == "- shipped\n"

    with pytest.raises(ValueError, match="Unreleased still contains"):
        validate_release_changelog(
            valid.replace("## Unreleased\n", "## Unreleased\n\n- not moved\n"),
            "v1.2.3",
            expected_date=date(2026, 10, 10),
        )
    with pytest.raises(ValueError, match="does not match exact candidate"):
        validate_release_changelog(
            valid,
            "v1.2.3",
            expected_date=date(2026, 10, 11),
        )
    with pytest.raises(ValueError, match="must use"):
        validate_release_changelog(
            valid.replace(" — 2026-10-10", ""),
            "v1.2.3",
            expected_date=date(2026, 10, 10),
        )
