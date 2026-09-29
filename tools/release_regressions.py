#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Validate the durable regression ledger for one Community release."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path, PurePosixPath
import re
import sys


SCHEMA = 1
_IDENTITY = re.compile(r"^[A-Z][A-Z0-9-]*-[0-9]+$")
_DISPOSITIONS = {
    "included",
    "deferred",
    "private_only",
    "excluded_experiment",
}


class RegressionLedgerError(ValueError):
    """The release regression ledger is incomplete or unsafe."""


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegressionLedgerError(f"{field} must be a non-empty string")
    return value


def _safe_file(root: Path, value: object, *, field: str) -> Path:
    text = _text(value, field=field)
    logical = PurePosixPath(text)
    if logical.is_absolute() or ".." in logical.parts or text != logical.as_posix():
        raise RegressionLedgerError(f"{field} must be a normalized relative path")
    path = root.joinpath(*logical.parts)
    try:
        if not path.is_file():
            raise RegressionLedgerError(f"{field} does not exist: {text}")
    except OSError as exc:
        raise RegressionLedgerError(f"cannot inspect {field} {text}: {exc}") from exc
    return path


def _ordered_strings(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise RegressionLedgerError(f"{field} must be a non-empty list")
    items = tuple(_text(item, field=field) for item in value)
    if list(items) != sorted(set(items)):
        raise RegressionLedgerError(f"{field} must be unique and sorted")
    return items


def _selector_exists(root: Path, selector: str, *, field: str) -> None:
    parts = selector.split("::")
    if len(parts) < 2 or any(not part for part in parts):
        raise RegressionLedgerError(f"{field} must be an exact pytest selector")
    source = _safe_file(root, parts[0], field=field)
    if source.suffix != ".py" or not parts[0].startswith("tests/"):
        raise RegressionLedgerError(f"{field} must select a Python test")
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise RegressionLedgerError(f"cannot inspect {field}: {exc}") from exc
    body: list[ast.stmt] = tree.body
    for name in parts[1:]:
        found = next(
            (
                node
                for node in body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name
            ),
            None,
        )
        if found is None:
            raise RegressionLedgerError(f"{field} does not resolve: {selector}")
        body = found.body if isinstance(found, ast.ClassDef) else []
    if not parts[-1].startswith("test_"):
        raise RegressionLedgerError(f"{field} must select one test function")


def validate_regression_ledger(
    root: Path,
    *,
    release: str,
    previous_tag: str,
) -> dict[str, object]:
    """Validate and summarize the candidate's public regression ledger."""

    root = root.resolve()
    path = root / "release/regressions.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegressionLedgerError(f"cannot read release regression ledger: {exc}") from exc
    if not isinstance(payload, dict):
        raise RegressionLedgerError("release regression ledger must be a JSON object")
    if set(payload) != {"schema", "release", "previous_tag", "entries"}:
        raise RegressionLedgerError("release regression ledger has unknown or missing fields")
    if payload["schema"] != SCHEMA:
        raise RegressionLedgerError(
            f"release regression ledger schema must be {SCHEMA}"
        )
    if payload["release"] != release:
        raise RegressionLedgerError("release regression ledger version does not match")
    if payload["previous_tag"] != previous_tag:
        raise RegressionLedgerError("release regression ledger baseline does not match")

    entries = payload["entries"]
    if not isinstance(entries, list) or not entries:
        raise RegressionLedgerError("release regression ledger must contain entries")
    identities: list[str] = []
    included: list[str] = []
    dispositions: dict[str, int] = {status: 0 for status in sorted(_DISPOSITIONS)}
    for index, entry in enumerate(entries):
        prefix = f"entries[{index}]"
        if not isinstance(entry, dict):
            raise RegressionLedgerError(f"{prefix} must be an object")
        identity = _text(entry.get("id"), field=f"{prefix}.id")
        if _IDENTITY.fullmatch(identity) is None:
            raise RegressionLedgerError(f"{prefix}.id is not a stable regression identity")
        status = _text(entry.get("status"), field=f"{prefix}.status")
        if status not in _DISPOSITIONS:
            raise RegressionLedgerError(f"{prefix}.status is unsupported")
        _text(entry.get("summary"), field=f"{prefix}.summary")
        identities.append(identity)
        dispositions[status] += 1

        if status == "included":
            expected = {"id", "status", "summary", "source_paths", "tests"}
            if set(entry) != expected:
                raise RegressionLedgerError(
                    f"{prefix} included entry has unknown or missing fields"
                )
            source_paths = _ordered_strings(
                entry["source_paths"], field=f"{prefix}.source_paths"
            )
            for source_path in source_paths:
                _safe_file(root, source_path, field=f"{prefix}.source_paths")
            selectors = _ordered_strings(entry["tests"], field=f"{prefix}.tests")
            for selector in selectors:
                _selector_exists(root, selector, field=f"{prefix}.tests")
            included.append(identity)
        else:
            expected = {"id", "status", "summary", "reason", "follow_up"}
            if set(entry) != expected:
                raise RegressionLedgerError(
                    f"{prefix} non-included entry has unknown or missing fields"
                )
            _text(entry["reason"], field=f"{prefix}.reason")
            _text(entry["follow_up"], field=f"{prefix}.follow_up")

    if identities != sorted(set(identities)):
        raise RegressionLedgerError("regression identities must be unique and sorted")
    return {
        "schema": SCHEMA,
        "entries": len(entries),
        "included": included,
        "dispositions": dispositions,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--release", required=True)
    parser.add_argument("--previous-tag", required=True)
    arguments = parser.parse_args(argv)
    try:
        report = validate_regression_ledger(
            arguments.root,
            release=arguments.release,
            previous_tag=arguments.previous_tag,
        )
    except RegressionLedgerError as exc:
        print(f"release-regressions: error: {exc}", file=sys.stderr)
        return 2
    print(
        f"release regressions valid: {report['entries']} entries, "
        f"{len(report['included'])} included"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
