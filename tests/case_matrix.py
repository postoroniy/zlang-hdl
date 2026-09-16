"""Run every row of a consolidated test matrix, even after one row fails."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from hashlib import sha256
import json
from typing import TypeVar
import traceback

import pytest


T = TypeVar("T")

# Frozen against the reviewed pre-consolidation parameter sets. A deliberate
# coverage change must update its row count and fingerprint in this one ledger.
_FROZEN_MATRICES: dict[str, tuple[int, str]] = {
    "case_matrix_probe": (
        3,
        "a25c389673fad20d93b9b50417d5520514bfff3d8ec489bd3bfe2d71c7356a12",
    ),
    "numeric": (25, "6c7d78691dcc8827eb307e74458ab0a20e89e0d1ade27d961c226b333af583c7"),
    "capabilities": (
        34,
        "925c9344d6bc47ef083793768ba3f86b657c51fa756336c0f3a8f29b6ce9b348",
    ),
    "intrinsic_families": (
        11,
        "82ef6924afb64b1ebcd4a3abbc8bb0702a0358b3b39cd023214c739675c2bd3f",
    ),
    "802_modules": (
        32,
        "08f7ba37eea9ce4d79fe57cf8ca1a4877886502901acaf6ce7ad6133e2019d16",
    ),
    "802_sources": (
        10,
        "3b2833eecd83bf478e0260723d89a052cb3ad45a462a9dd54d1f80a2b42d083c",
    ),
    "literal_unsigned": (
        6,
        "e2f4362db47201bf813745e9f047f56f03403544a7514d5d801759a82d9fe72f",
    ),
    "literal_signed": (
        7,
        "e539980c19291faf9c49d7b9633b7fadbb36eab025d660aac69f21e8a69c1b30",
    ),
    "literal_positive_radix": (
        4,
        "42f5bb570593873d2802f753a88451068053c4d5c0f787c7b5c59a0faa4aab2d",
    ),
    "literal_negative_radix": (
        4,
        "30808f72275f6c4ea3a3d3fcebf1c736f5ffee3025333adcb0ee18eeadac635c",
    ),
    "literal_unsigned_reject": (
        4,
        "e30f3db0ee39ed0edbf8b3a9c427b203cb2979d2cebcf88188ece358ec546349",
    ),
    "parser_escapes": (
        11,
        "62939aeb1f67986b61b8f23f07660facfd99369b74aca3301da4cd409ecb7f1a",
    ),
    "parser_printable": (
        4,
        "fa0b16241f4c88d57cf748b48d0325812661a812860762b954634fffbfc72d34",
    ),
    "parser_malformed_hex": (
        12,
        "f4fd18c10d3d0ceb8bb3f4c40a36c96d5a2a95d46538f9cda57a30d9cc92a8a8",
    ),
    "parser_malformed_nonascii": (
        6,
        "9f116912c9bf269cccd84ff3a85095860ba52f8f0215d4548d1b1f320a93ee3d",
    ),
    "enum_width": (
        5,
        "5c3de85bd794bbc52e9d66a04d2e940003bc43e4c55a9ea8fd52fe2dc9b07df3",
    ),
    "enum_invalid": (
        10,
        "09b30fcfccc332d871704a7fcd0e0dbf5e95d4445a8b1890cfe93a7f7176d9ca",
    ),
    "enum_ops": (3, "15ef0bc550a8efcba9fd668105b07b11b5079b8bf9758b4839761110f3f7fb0a"),
    "tuple_arity": (
        7,
        "2ca6c908bb3c7fddc2e1f819cc60a2343c1a095f78b61cde4ff9aa48acb6caa0",
    ),
}


def label_fingerprint(labels: Iterable[str]) -> str:
    """Content identity of an ordered, explicitly reviewed matrix membership."""
    payload = json.dumps(tuple(labels), ensure_ascii=False, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def check_cases(
    cases: Iterable[tuple[str, T]],
    check: Callable[[T], None],
    *,
    matrix: str,
) -> None:
    """Retain each frozen input and its diagnostic while reducing pytest items."""
    try:
        expected_count, expected_labels_sha256 = _FROZEN_MATRICES[matrix]
    except KeyError as error:
        raise AssertionError(f"unregistered test matrix: {matrix!r}") from error
    seen: set[str] = set()
    labels: list[str] = []
    failures: list[tuple[str, str]] = []
    for label, value in cases:
        if not label or label in seen:
            raise AssertionError(f"empty or duplicate matrix label: {label!r}")
        seen.add(label)
        labels.append(label)
        try:
            check(value)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            failures.append((label, traceback.format_exc()))
    if not seen:
        raise AssertionError("test matrix has no cases")
    actual_fingerprint = label_fingerprint(labels)
    if len(labels) != expected_count or actual_fingerprint != expected_labels_sha256:
        failures.append(
            (
                "matrix membership",
                "reviewed row labels changed; update the frozen fingerprint only "
                "after reviewing every added/removed row\n"
                f"expected_count={expected_count}\nactual_count={len(labels)}\n"
                f"expected={expected_labels_sha256}\nactual={actual_fingerprint}\n"
                f"labels={labels!r}",
            )
        )
    if failures:
        details = "\n\n".join(
            f"case {label}:\n{failure}" for label, failure in failures
        )
        pytest.fail(
            f"{len(failures)} of {len(seen)} matrix cases failed:\n{details}",
            pytrace=False,
        )
