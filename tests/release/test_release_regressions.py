# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from tools.release_regressions import (
    RegressionLedgerError,
    main,
    validate_regression_ledger,
)


ROOT = Path(__file__).resolve().parents[2]


def _copy_ledger_tree(tmp_path: Path) -> Path:
    root = tmp_path / "candidate"
    shutil.copytree(ROOT / "release", root / "release")
    payload = _payload(root)
    relatives = {
        relative
        for entry in payload["entries"]
        if entry["status"] == "included"
        for relative in (
            *entry["source_paths"],
            *(selector.split("::", 1)[0] for selector in entry["tests"]),
        )
    }
    for relative in sorted(relatives):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    return root


def _payload(root: Path) -> dict[str, object]:
    return json.loads((root / "release/regressions.json").read_text(encoding="utf-8"))


def _write(root: Path, payload: dict[str, object]) -> None:
    (root / "release/regressions.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_current_release_binds_included_fixes_to_permanent_tests() -> None:
    report = validate_regression_ledger(
        ROOT,
        release="0.1.0a19",
        previous_tag="v0.1.0a18",
    )
    assert report == {
        "schema": 1,
        "entries": 6,
        "included": [
            "ZL-039", "ZL-040", "ZL-041", "ZL-042", "ZL-043", "ZL-044"
        ],
        "dispositions": {
            "deferred": 0,
            "excluded_experiment": 0,
            "included": 6,
            "private_only": 0,
        },
    }


def test_missing_or_renamed_regression_test_fails_closed(tmp_path: Path) -> None:
    root = _copy_ledger_tree(tmp_path)
    payload = _payload(root)
    payload["entries"][0]["tests"][0] += "_missing"
    _write(root, payload)
    with pytest.raises(RegressionLedgerError, match="does not resolve"):
        validate_regression_ledger(
            root,
            release="0.1.0a19",
            previous_tag="v0.1.0a18",
        )


def test_missing_release_source_fails_closed(tmp_path: Path) -> None:
    root = _copy_ledger_tree(tmp_path)
    payload = _payload(root)
    payload["entries"][0]["source_paths"][0] = "zlang/missing.py"
    _write(root, payload)
    with pytest.raises(RegressionLedgerError, match="does not exist"):
        validate_regression_ledger(
            root,
            release="0.1.0a19",
            previous_tag="v0.1.0a18",
        )


def test_release_and_previous_tag_are_exact(tmp_path: Path) -> None:
    root = _copy_ledger_tree(tmp_path)
    with pytest.raises(RegressionLedgerError, match="version does not match"):
        validate_regression_ledger(
            root,
            release="0.1.0a20",
            previous_tag="v0.1.0a18",
        )
    with pytest.raises(RegressionLedgerError, match="baseline does not match"):
        validate_regression_ledger(
            root,
            release="0.1.0a19",
            previous_tag="v0.1.0a17",
        )


def test_nonincluded_fix_requires_reason_and_durable_follow_up(tmp_path: Path) -> None:
    root = _copy_ledger_tree(tmp_path)
    payload = _payload(root)
    payload["entries"][0] = {
        "id": "ZL-039",
        "status": "deferred",
        "summary": "Deferred example",
        "reason": "Requires a separately reviewed semantic change.",
        "follow_up": "ZL-041",
    }
    _write(root, payload)
    report = validate_regression_ledger(
        root,
        release="0.1.0a19",
        previous_tag="v0.1.0a18",
    )
    assert report["included"] == [
        "ZL-040", "ZL-041", "ZL-042", "ZL-043", "ZL-044"
    ]

    del payload["entries"][0]["follow_up"]
    _write(root, payload)
    with pytest.raises(RegressionLedgerError, match="unknown or missing fields"):
        validate_regression_ledger(
            root,
            release="0.1.0a19",
            previous_tag="v0.1.0a18",
        )


def test_duplicate_or_unsorted_identities_fail_closed(tmp_path: Path) -> None:
    root = _copy_ledger_tree(tmp_path)
    payload = _payload(root)
    payload["entries"] = list(reversed(payload["entries"]))
    _write(root, payload)
    with pytest.raises(RegressionLedgerError, match="unique and sorted"):
        validate_regression_ledger(
            root,
            release="0.1.0a19",
            previous_tag="v0.1.0a18",
        )


def test_cli_reports_the_validated_inclusion_count(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([
        "--root", str(ROOT),
        "--release", "0.1.0a19",
        "--previous-tag", "v0.1.0a18",
    ]) == 0
    assert capsys.readouterr().out == (
        "release regressions valid: 6 entries, 6 included\n"
    )
