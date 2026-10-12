"""The aggregation helper must not hide later inputs after a failure."""

from __future__ import annotations

import pytest

from tests.case_matrix import check_cases


def test_matrix_reports_each_failed_row_and_runs_all_inputs() -> None:
    seen: list[int] = []

    def check(value: int) -> None:
        seen.append(value)
        assert value != 1, "intentional mutation"
        assert value != 3, "second intentional mutation"

    with pytest.raises(BaseException, match="2 of 3 matrix cases failed") as error:
        check_cases(
            (("first", 1), ("middle", 2), ("last", 3)),
            check,
            matrix="case_matrix_probe",
        )
    assert seen == [1, 2, 3]
    assert "case first" in str(error.value)
    assert "case last" in str(error.value)


@pytest.mark.parametrize(
    "labels",
    (("first", "last"), ("first", "replacement", "last"), ("last", "middle", "first")),
)
def test_matrix_rejects_dropped_replaced_or_reordered_rows(
    labels: tuple[str, ...],
) -> None:
    checked: list[str] = []
    with pytest.raises(BaseException, match="matrix membership"):
        check_cases(
            ((label, label) for label in labels),
            checked.append,
            matrix="case_matrix_probe",
        )
    assert checked == list(labels)


def test_matrix_requires_a_reviewed_ledger_entry() -> None:
    with pytest.raises(AssertionError, match="unregistered test matrix"):
        check_cases((("first", 1),), lambda value: None, matrix="not-reviewed")
