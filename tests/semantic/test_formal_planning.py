from __future__ import annotations

from dataclasses import replace
import json

import pytest

from zlang.ir import (
    ComparisonWindow,
    FormalBackendArtifactRef,
    FormalExecutableRoute,
    FormalExecutionPlan,
    FormalGoalPlan,
    FormalPlanGoalKind,
    FormalPlanningError,
    FormalRouteKind,
    FormalSkipCode,
    FormalSkipReason,
)
from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
    clock_domain_contract_identity,
)
from zlang.source import SourceOrigin, SourceSpan


def _origin() -> SourceOrigin:
    return SourceOrigin(
        SourceSpan(8, 5, 10, 6),
        "assert count_within",
        "examples/counter.zhl",
        "1" * 64,
    )


def _artifact(
    backend: str = "systemverilog", suffix: str = "sv"
) -> FormalBackendArtifactRef:
    return FormalBackendArtifactRef(
        backend,
        f"artifact:{suffix}",
        f"bindings:{suffix}",
    )


def _safety_goal() -> FormalGoalPlan:
    return FormalGoalPlan(
        goal_identity="goal:count-within",
        property_identity="m35.register.count.bounds",
        kind=FormalPlanGoalKind.SAFETY,
        clock_domain="clk",
        reset_domain="rst",
        assumption_ids=("require:legal-input",),
        required_observations=("register:count",),
        selected_ir_identity="selected:counter",
        comparison_window=ComparisonWindow.same_cycle(),
        minimum_bmc_depth=2,
        route=FormalExecutableRoute(
            FormalRouteKind.PROPERTY_HARNESS,
            (_artifact(),),
        ),
        source_origin=_origin(),
    )


def _m36_goal() -> FormalGoalPlan:
    window = ComparisonWindow.reset_fill(3)
    return FormalGoalPlan(
        goal_identity="goal:selected-candidate",
        property_identity="m36.equiv.pipeline.example",
        kind=FormalPlanGoalKind.M36_EQUIVALENCE,
        clock_domain="clk",
        reset_domain="rst",
        assumption_ids=(),
        required_observations=("input:a", "output:y"),
        selected_ir_identity="selected:counter",
        comparison_window=window,
        minimum_bmc_depth=window.minimum_bmc_depth,
        route=FormalExecutableRoute(
            FormalRouteKind.SEMANTIC_EQUIVALENCE,
            (_artifact("direct_systemverilog", "sv"),),
            reference_identity="reference:canonical",
        ),
    )


def test_execution_plan_round_trips_deterministically() -> None:
    # Input order is not semantic; reports use stable goal-identity order.
    plan = FormalExecutionPlan(
        "selected:counter",
        "verification:counter",
        (_safety_goal(), _m36_goal()),
    )
    reversed_plan = FormalExecutionPlan(
        "selected:counter",
        "verification:counter",
        (_m36_goal(), _safety_goal()),
    )

    assert plan == reversed_plan
    assert plan.to_json() == reversed_plan.to_json()
    assert FormalExecutionPlan.from_json(plan.to_json()) == plan
    assert FormalExecutionPlan.from_data(plan.to_data()) == plan
    assert plan.plan_identity.startswith("formal-execution-plan:")
    assert _safety_goal().backend_artifact_identities == ("artifact:sv",)


def test_execution_plan_derives_deterministic_applicability_summary() -> None:
    skipped = replace(
        _safety_goal(),
        goal_identity="goal:skipped",
        route=None,
        skip_reason=FormalSkipReason(
            FormalSkipCode.OBSERVATION_UNAVAILABLE,
            "the required observation is not published",
        ),
    )
    plan = FormalExecutionPlan(
        "selected:counter",
        "verification:counter",
        (_m36_goal(), skipped),
    )

    assert plan.applicability_summary() == {
        "total": 2,
        "executable": 1,
        "skipped": 1,
        "goal_kinds": {"m36_equivalence": 1, "safety": 1},
        "routes": {"semantic_equivalence": 1},
        "backends": {"direct_systemverilog": 1},
        "skip_reasons": {"observation_unavailable": 1},
    }
    assert FormalExecutionPlan.from_json(plan.to_json()).applicability_summary() == (
        plan.applicability_summary()
    )


def test_skipped_goal_is_structured_and_has_no_artifact() -> None:
    goal = replace(
        _safety_goal(),
        route=None,
        skip_reason=FormalSkipReason(
            FormalSkipCode.ASSUMPTION_UNAVAILABLE,
            "the scoped environment requirement has no backend binding",
            ("require:legal-input",),
            "systemverilog",
        ),
    )
    restored = FormalGoalPlan.from_data(goal.to_data())

    assert restored == goal
    assert restored.backend_artifact_identities == ()
    assert restored.skip_reason is not None
    assert restored.skip_reason.code is FormalSkipCode.ASSUMPTION_UNAVAILABLE


def test_goal_plan_retains_exact_physical_domain_and_separates_identity() -> None:
    asynchronous = ClockDomain(
        "clk",
        "rst_n",
        ClockEdge.FALLING,
        ResetMode.ASYNCHRONOUS,
        ResetPolarity.ACTIVE_LOW,
        reset_release_mode=ResetReleaseMode.SYNCHRONIZED,
        reset_release_cycles=2,
    )
    async_goal = replace(
        _safety_goal(),
        clock_domain="clk",
        reset_domain="rst_n",
        clock_domain_contract=asynchronous,
        physical_domain_identity=clock_domain_contract_identity(asynchronous),
    )
    restored = FormalGoalPlan.from_data(async_goal.to_data())

    assert restored == async_goal
    assert restored.clock_domain_contract == asynchronous
    assert restored.physical_domain_identity == clock_domain_contract_identity(
        asynchronous
    )
    assert restored.plan_identity != _safety_goal().plan_identity

    corrupted = async_goal.to_data()
    corrupted["physical_domain_identity"] = "0" * 64
    with pytest.raises(FormalPlanningError, match="does not match"):
        FormalGoalPlan.from_data(corrupted)

    with pytest.raises(FormalPlanningError, match="clock/reset names"):
        replace(async_goal, reset_domain="other")




@pytest.mark.parametrize(
    ("change", "message"),
    (
        ({"route": None}, "exactly one route or skip"),
        (
            {
                "route": FormalExecutableRoute(
                    FormalRouteKind.COVER_HARNESS, (_artifact(),)
                )
            },
            "cannot use route",
        ),
        ({"clock_domain": None}, "reset domain requires a clock"),
        ({"minimum_bmc_depth": 0}, "does not reach"),
        ({"assumption_ids": ("a", "a")}, "assumption IDs must be unique"),
        (
            {"required_observations": ("register:count", "register:count")},
            "required observations must be unique",
        ),
    ),
)
def test_goal_invariants_reject_partial_or_inconsistent_plans(
    change: dict[str, object], message: str
) -> None:
    with pytest.raises(FormalPlanningError, match=message):
        replace(_safety_goal(), **change)


def test_strict_codec_rejects_corruption() -> None:
    plan = FormalExecutionPlan(
        "selected:counter", "verification:counter", (_safety_goal(),)
    )

    extra = plan.to_data()
    extra["unknown"] = 1
    with pytest.raises(FormalPlanningError, match="unexpected unknown"):
        FormalExecutionPlan.from_data(extra)

    wrong_identity = plan.to_data()
    wrong_identity["plan_identity"] = "formal-execution-plan:wrong"
    with pytest.raises(FormalPlanningError, match="identity does not match"):
        FormalExecutionPlan.from_data(wrong_identity)

    shallow = plan.to_data()
    shallow_goal = shallow["goals"][0]  # type: ignore[index]
    shallow_goal["minimum_bmc_depth"] = 0  # type: ignore[index]
    # Update only the nested claimed identity: validation must still reject the
    # comparison-window violation before accepting either digest.
    with pytest.raises(FormalPlanningError, match="does not reach"):
        FormalExecutionPlan.from_data(shallow)

    bad_window = plan.to_data()
    bad_goal = bad_window["goals"][0]  # type: ignore[index]
    window = bad_goal["comparison_window"]  # type: ignore[index]
    window["minimum_bmc_depth"] = 9  # type: ignore[index]
    with pytest.raises(FormalPlanningError, match="does not match"):
        FormalExecutionPlan.from_data(bad_window)

    bad_origin = plan.to_data()
    origin = bad_origin["goals"][0]["source_origin"]  # type: ignore[index]
    origin["span"]["extra"] = 1  # type: ignore[index]
    with pytest.raises(FormalPlanningError, match="unexpected extra"):
        FormalExecutionPlan.from_data(bad_origin)

    with pytest.raises(FormalPlanningError, match="not valid JSON"):
        FormalExecutionPlan.from_json("{")
    with pytest.raises(FormalPlanningError, match="must be an object"):
        FormalExecutionPlan.from_json(json.dumps([]))


def test_execution_plan_rejects_duplicate_goal_but_allows_shared_property() -> None:
    first = _safety_goal()
    duplicate_goal = replace(_m36_goal(), goal_identity=first.goal_identity)
    with pytest.raises(FormalPlanningError, match="goal identities"):
        FormalExecutionPlan("selected:counter", "verification:x", (first, duplicate_goal))

    # One typed property may legitimately have separately routed execution
    # goals (for example, bounded and proof-oriented direct-SV M36 jobs).
    shared_property = replace(
        _m36_goal(), property_identity=first.property_identity
    )
    plan = FormalExecutionPlan(
        "selected:counter", "verification:x", (first, shared_property)
    )
    assert tuple(item.property_identity for item in plan.goals).count(
        first.property_identity
    ) == 2


def test_execution_plan_rejects_goal_from_a_different_selected_ir() -> None:
    with pytest.raises(FormalPlanningError, match="selected-IR identity differs"):
        FormalExecutionPlan(
            "selected:other",
            "verification:counter",
            (_safety_goal(),),
        )


def test_plan_identity_excludes_source_origin_but_json_preserves_it() -> None:
    first = _safety_goal()
    relocated = replace(
        first,
        source_origin=SourceOrigin(
            SourceSpan(80, 2, 82, 3),
            "assert count_within",
            "generated/counter.zhl",
            "2" * 64,
        ),
    )

    assert first.plan_identity == relocated.plan_identity
    assert first.to_data()["source_origin"] != relocated.to_data()["source_origin"]
