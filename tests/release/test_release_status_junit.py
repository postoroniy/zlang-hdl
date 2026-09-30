from __future__ import annotations

from pathlib import Path

import pytest

import tools.release_status as release_status


def _write_junit(
    path: Path,
    *,
    passed: int,
    skipped: int = 0,
    failures: int = 0,
    errors: int = 0,
) -> None:
    cases = [f'<testcase name="passed-{index}" />' for index in range(passed)]
    cases.extend(
        f'<testcase name="skipped-{index}"><skipped /></testcase>'
        for index in range(skipped)
    )
    cases.extend(
        f'<testcase name="failed-{index}"><failure /></testcase>'
        for index in range(failures)
    )
    cases.extend(
        f'<testcase name="error-{index}"><error /></testcase>'
        for index in range(errors)
    )
    path.write_text(
        '<testsuite name="release">' + "".join(cases) + "</testsuite>",
        encoding="utf-8",
    )


def test_junit_partitions_are_validated_against_their_own_minimums(
    tmp_path: Path,
) -> None:
    deterministic = tmp_path / "deterministic.xml"
    performance = tmp_path / "performance.xml"
    _write_junit(deterministic, passed=3)
    _write_junit(performance, passed=2)

    release_status._validate_junit_report(
        deterministic,
        suite="deterministic",
        minimum_passed=3,
        minimum_collected=3,
        maximum_skipped=0,
    )
    release_status._validate_junit_report(
        performance,
        suite="performance",
        minimum_passed=2,
        minimum_collected=2,
        maximum_skipped=0,
    )


def test_performance_report_cannot_substitute_for_the_deterministic_partition(
    tmp_path: Path,
) -> None:
    performance = tmp_path / "performance.xml"
    _write_junit(performance, passed=2)

    with pytest.raises(
        release_status.StatusError,
        match="only 2 tests collected in deterministic JUnit report",
    ):
        release_status._validate_junit_report(
            performance,
            suite="deterministic",
            minimum_passed=3,
            minimum_collected=3,
            maximum_skipped=0,
        )


@pytest.mark.parametrize(
    ("counts", "message"),
    (
        ({"passed": 1, "skipped": 1}, "1 tests skipped in performance"),
        ({"passed": 1, "failures": 1}, "1 failures and 0 errors"),
        ({"passed": 1, "errors": 1}, "0 failures and 1 errors"),
    ),
)
def test_performance_report_fails_closed(
    tmp_path: Path, counts: dict[str, int], message: str
) -> None:
    report = tmp_path / "performance.xml"
    _write_junit(report, **counts)

    with pytest.raises(release_status.StatusError, match=message):
        release_status._validate_junit_report(
            report,
            suite="performance",
            minimum_passed=1,
            minimum_collected=1,
            maximum_skipped=0,
        )
