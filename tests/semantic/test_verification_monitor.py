from __future__ import annotations

import pytest

from zlang.ir.expressions import (
    Binary,
    BinaryOperator,
    Constant,
    InputRef,
    RegisterRef,
)
from zlang.ir.types import BitType, UIntType
from zlang.ir.verification import (
    VerificationGoal,
    VerificationGoalKind,
    VerificationRequirement,
    VerificationScope,
)
from zlang.simulate import (
    SimulationError,
    VerificationAssertionError,
    VerificationMonitor,
)
from zlang.source import SourceOrigin, SourceSpan


BIT = BitType()
U3 = UIntType(3)


def _origin(line: int, construct: str) -> SourceOrigin:
    return SourceOrigin(
        SourceSpan(line, 3, line, 24),
        construct,
        source_unit="monitor.zhl",
    )


def _predicate(operator: BinaryOperator, value: int) -> Binary:
    return Binary(
        operator,
        RegisterRef("count", U3),
        Constant(value, U3),
        U3,
        BIT,
    )


def _scope(*, include_ensure: bool = False) -> VerificationScope:
    scope_id = "module:Counter:scope:fifo_behavior"
    goals = [
        VerificationGoal(
            semantic_id=f"{scope_id}:assert:count_within",
            scope_id=scope_id,
            kind=VerificationGoalKind.ASSERT,
            name="count_within",
            expression=_predicate(BinaryOperator.LESS_EQUAL, 4),
            source_origin=_origin(8, "verification assert"),
        ),
        VerificationGoal(
            semantic_id=f"{scope_id}:cover:full",
            scope_id=scope_id,
            kind=VerificationGoalKind.COVER,
            name="full",
            expression=_predicate(BinaryOperator.EQUAL, 4),
            source_origin=_origin(11, "verification cover"),
        ),
    ]
    if include_ensure:
        goals.append(
            VerificationGoal(
                semantic_id=f"{scope_id}:ensure:output_shape",
                scope_id=scope_id,
                kind=VerificationGoalKind.ENSURE,
                name="output_shape",
                expression=InputRef("output_ok", BIT),
                source_origin=_origin(14, "verification ensure"),
            )
        )
    return VerificationScope(
        semantic_id=scope_id,
        name="fifo_behavior",
        clock="clk",
        reset="rst",
        requirements=(
            VerificationRequirement(
                semantic_id=f"{scope_id}:require:legal_input",
                name="legal_input",
                expression=InputRef("legal", BIT),
                source_origin=_origin(5, "verification requirement"),
            ),
        ),
        goals=tuple(goals),
        source_origin=_origin(4, "verification contract"),
    )


def test_reset_suppresses_requirements_assertions_and_covers() -> None:
    monitor = VerificationMonitor((_scope(),))

    result = monitor.sample({"legal": 0, "count": 7}, cycle=0, reset_active=True)

    assert result.reset_suppressed
    assert result.active_scope_ids == ()
    assert result.requirement_violations == ()
    assert result.cover_witnesses == ()
    assert monitor.requirement_violations == ()
    assert monitor.cover_witnesses == ()


def test_requirement_violation_is_recorded_and_gates_scope_goals() -> None:
    monitor = VerificationMonitor((_scope(),))

    result = monitor.sample({"legal": 0, "count": 7}, cycle=1)

    assert result.active_scope_ids == ()
    assert len(result.requirement_violations) == 1
    violation = result.requirement_violations[0]
    assert violation.scope_name == "fifo_behavior"
    assert violation.requirement_name == "legal_input"
    assert violation.cycle == 1
    assert monitor.requirement_violations == result.requirement_violations
    assert monitor.cover_witnesses == ()


def test_cover_records_only_the_first_witness_cycle() -> None:
    monitor = VerificationMonitor((_scope(),))

    first = monitor.sample({"legal": 1, "count": 4}, cycle=2)
    repeated = monitor.sample({"legal": 1, "count": 4}, cycle=3)

    assert first.active_scope_ids == ("module:Counter:scope:fifo_behavior",)
    assert len(first.cover_witnesses) == 1
    assert first.cover_witnesses[0].goal_name == "full"
    assert first.cover_witnesses[0].cycle == 2
    assert repeated.cover_witnesses == ()
    assert monitor.cover_witnesses == first.cover_witnesses


@pytest.mark.parametrize(
    ("values", "kind", "goal_name", "line"),
    [
        ({"legal": 1, "count": 5, "output_ok": 1}, "assert", "count_within", 8),
        ({"legal": 1, "count": 1, "output_ok": 0}, "ensure", "output_shape", 14),
    ],
)
def test_safety_failure_is_source_attributed(
    values: dict[str, int],
    kind: str,
    goal_name: str,
    line: int,
) -> None:
    monitor = VerificationMonitor((_scope(include_ensure=True),))

    with pytest.raises(VerificationAssertionError) as caught:
        monitor.sample(values, cycle=6)

    error = caught.value
    assert error.goal_kind.value == kind
    assert error.goal_name == goal_name
    assert error.cycle == 6
    assert (
        str(error)
        == f"verification {kind} 'fifo_behavior.{goal_name}' failed at cycle 6 "
        f"at monitor.zhl:{line}:3-{line}:24:verification {kind}"
    )


def test_missing_sampled_value_reports_goal_and_source() -> None:
    monitor = VerificationMonitor((_scope(),))

    with pytest.raises(SimulationError, match="missing value 'count'") as caught:
        monitor.sample({"legal": 1}, cycle=2)

    assert "goal 'fifo_behavior.count_within'" in str(caught.value)
    assert "monitor.zhl:8:3-8:24:verification assert" in str(caught.value)


def test_non_bit_sample_is_rejected_defensively() -> None:
    scope_id = "module:Bad:scope:bad"
    scope = VerificationScope(
        semantic_id=scope_id,
        name="bad",
        clock="clk",
        reset="rst",
        goals=(
            VerificationGoal(
                semantic_id=f"{scope_id}:assert:value",
                scope_id=scope_id,
                kind=VerificationGoalKind.ASSERT,
                name="value",
                expression=InputRef("value", BIT),
            ),
        ),
    )
    monitor = VerificationMonitor((scope,))

    with pytest.raises(SimulationError, match="produced non-bit value 2"):
        monitor.sample({"value": 2}, cycle=0)


def test_monitor_rejects_duplicate_scope_or_goal_identities() -> None:
    scope = _scope()
    with pytest.raises(ValueError, match="scope semantic IDs must be unique"):
        VerificationMonitor((scope, scope))

    duplicate_goal_scope = VerificationScope(
        semantic_id="module:Other:scope:copy",
        name="copy",
        clock="clk",
        reset="rst",
        goals=(
            VerificationGoal(
                semantic_id=scope.goals[0].semantic_id,
                scope_id="module:Other:scope:copy",
                kind=VerificationGoalKind.COVER,
                name="copied",
                expression=Constant(1, BIT),
            ),
        ),
    )
    with pytest.raises(ValueError, match="goal semantic IDs must be unique"):
        VerificationMonitor((scope, duplicate_goal_scope))


def test_monitor_rejects_duplicate_requirement_and_cross_clause_identities() -> None:
    scope = _scope()
    duplicate_requirement_scope = VerificationScope(
        semantic_id="module:Other:scope:requirements",
        name="requirements",
        clock="clk",
        reset="rst",
        requirements=(VerificationRequirement(
            semantic_id=scope.requirements[0].semantic_id,
            name="copied_requirement",
            expression=Constant(1, BIT),
        ),),
        goals=(VerificationGoal(
            semantic_id="module:Other:scope:requirements:cover:seen",
            scope_id="module:Other:scope:requirements",
            kind=VerificationGoalKind.COVER,
            name="seen",
            expression=Constant(1, BIT),
        ),),
    )
    with pytest.raises(ValueError, match="requirement semantic IDs must be unique"):
        VerificationMonitor((scope, duplicate_requirement_scope))

    colliding_clause_scope = VerificationScope(
        semantic_id="module:Other:scope:collision",
        name="collision",
        clock="clk",
        reset="rst",
        goals=(VerificationGoal(
            semantic_id=scope.requirements[0].semantic_id,
            scope_id="module:Other:scope:collision",
            kind=VerificationGoalKind.COVER,
            name="collision",
            expression=Constant(1, BIT),
        ),),
    )
    with pytest.raises(ValueError, match="clause semantic IDs must be globally unique"):
        VerificationMonitor((scope, colliding_clause_scope))


def test_multi_domain_module_scopes_are_sampled_by_explicit_clock() -> None:
    left = VerificationScope(
        semantic_id="module:Multi:scope:a",
        name="$module",
        clock="a",
        reset="ra",
        goals=(VerificationGoal(
            semantic_id="module:Multi:scope:a:cover:left",
            scope_id="module:Multi:scope:a",
            kind=VerificationGoalKind.COVER,
            name="left",
            expression=InputRef("left", BIT),
        ),),
    )
    right = VerificationScope(
        semantic_id="module:Multi:scope:b",
        name="$module",
        clock="b",
        reset="rb",
        goals=(VerificationGoal(
            semantic_id="module:Multi:scope:b:cover:right",
            scope_id="module:Multi:scope:b",
            kind=VerificationGoalKind.COVER,
            name="right",
            expression=InputRef("right", BIT),
        ),),
    )
    monitor = VerificationMonitor((left, right))

    with pytest.raises(ValueError, match="requires an explicit clock"):
        monitor.sample({"left": 1, "right": 1}, cycle=0)
    sampled = monitor.sample({"left": 1}, cycle=0, clock="a")
    assert sampled.active_scope_ids == (left.semantic_id,)
    assert [item.goal_name for item in sampled.cover_witnesses] == ["left"]
    assert [item.goal_name for item in monitor.cover_witnesses] == ["left"]
