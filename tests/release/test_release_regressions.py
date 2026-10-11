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
    payload = json.loads((ROOT / "release/regressions.json").read_text(encoding="utf-8"))
    relatives: set[str] = set()
    for entry in payload["entries"]:
        if entry["status"] != "included":
            continue
        relatives.update(entry["source_paths"])
        relatives.update(selector.split("::", 1)[0] for selector in entry["tests"])
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
        release="0.1.0a21",
        previous_tag="v0.1.0a20",
    )
    assert report == {
        "schema": 1,
        "entries": 8,
        "included": [
            "EDITOR-001", "REL-001", "REL-002", "REL-003",
            "ZL-045", "ZL-046", "ZL-047", "ZL-048",
        ],
        "dispositions": {
            "deferred": 0,
            "excluded_experiment": 0,
            "included": 8,
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
            release="0.1.0a21",
            previous_tag="v0.1.0a20",
        )


def test_missing_release_source_fails_closed(tmp_path: Path) -> None:
    root = _copy_ledger_tree(tmp_path)
    payload = _payload(root)
    payload["entries"][0]["source_paths"][-1] = "zlang/missing.py"
    _write(root, payload)
    with pytest.raises(RegressionLedgerError, match="does not exist"):
        validate_regression_ledger(
            root,
            release="0.1.0a21",
            previous_tag="v0.1.0a20",
        )


def test_release_and_previous_tag_are_exact(tmp_path: Path) -> None:
    root = _copy_ledger_tree(tmp_path)
    with pytest.raises(RegressionLedgerError, match="version does not match"):
        validate_regression_ledger(
            root,
            release="0.1.0a22",
            previous_tag="v0.1.0a20",
        )
    with pytest.raises(RegressionLedgerError, match="baseline does not match"):
        validate_regression_ledger(
            root,
            release="0.1.0a21",
            previous_tag="v0.1.0a19",
        )


def test_nonincluded_fix_requires_reason_and_durable_follow_up(tmp_path: Path) -> None:
    root = _copy_ledger_tree(tmp_path)
    payload = _payload(root)
    payload["entries"][0] = {
        "id": "EDITOR-001",
        "status": "deferred",
        "summary": "Deferred example",
        "reason": "Requires a separately reviewed semantic change.",
        "follow_up": "ZL-045",
    }
    _write(root, payload)
    report = validate_regression_ledger(
        root,
        release="0.1.0a21",
        previous_tag="v0.1.0a20",
    )
    assert report["included"] == [
        "REL-001", "REL-002", "REL-003",
        "ZL-045", "ZL-046", "ZL-047", "ZL-048",
    ]

    del payload["entries"][0]["follow_up"]
    _write(root, payload)
    with pytest.raises(RegressionLedgerError, match="unknown or missing fields"):
        validate_regression_ledger(
            root,
            release="0.1.0a21",
            previous_tag="v0.1.0a20",
        )


def test_duplicate_or_unsorted_identities_fail_closed(tmp_path: Path) -> None:
    root = _copy_ledger_tree(tmp_path)
    payload = _payload(root)
    payload["entries"] = list(reversed(payload["entries"]))
    _write(root, payload)
    with pytest.raises(RegressionLedgerError, match="unique and sorted"):
        validate_regression_ledger(
            root,
            release="0.1.0a21",
            previous_tag="v0.1.0a20",
        )


def test_cli_reports_the_validated_inclusion_count(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([
        "--root", str(ROOT),
        "--release", "0.1.0a21",
        "--previous-tag", "v0.1.0a20",
    ]) == 0
    assert capsys.readouterr().out == (
        "release regressions valid: 8 entries, 8 included\n"
    )
