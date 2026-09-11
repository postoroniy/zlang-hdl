from __future__ import annotations

from pathlib import Path

import pytest

from zlang.costs import SourcePolicy
from zlang.exploration import TransformFamily
from zlang.formal_exploration import FormalPolicy
from zlang.implementation_request import (
    ArchitectureRequest,
    BackendKind,
    BackendRequest,
    ConstraintRelation,
    ExactTiming,
    ImplementationConstraint,
    ImplementationContribution,
    ImplementationObjective,
    ImplementationRequestError,
    ObjectiveDirection,
    PolicyOrigin,
    RequirementMode,
    SemanticRegionIdentity,
    TransformPolicy,
    apply_exact_timing_contract,
    merge_implementation_contributions,
    parse_selected_profile,
)
from zlang.ir.expressions import CostMetric
from zlang.ir.timing import ModuleTimingContract
from zlang.project import ProjectManifest
from zlang.targets import ArchitectureSelectionMode


def _manifest(profiles: str) -> ProjectManifest:
    return ProjectManifest.parse(
        '''schema = 1

[project]
name = "acme.design"
version = "1.0.0"
source-root = "src"

[dependencies]
''' + profiles,
        path=Path("/portable/project/zlang.toml"),
    )


def _full_profile(name: str = "release") -> str:
    return f'''
[profiles.{name}]
backend = "systemverilog"
backend-mode = "required"
target = "std.target.xilinx.xc7z030"
allowed-transforms = ["pipeline", "dsp", "reduction"]
avoided-transforms = ["reassociate"]
objective = "maximize_fmax"
architecture = "std.arch.xilinx.dsp48e1"
architecture-mode = "preferred"
evidence-policy = "measured_required"
formal-policy = "required_bmc"

[profiles.{name}.constraints]
latency = {{ maximum = 4 }}
ii = 1
fmax = 100.0
dsp = 8
'''


def test_objective_capability_sets_are_shared_by_source_and_profiles() -> None:
    with pytest.raises(ValueError, match="minimizes only"):
        ImplementationObjective(ObjectiveDirection.MINIMIZE, CostMetric.FMAX_EST)
    with pytest.raises(ValueError, match="only maximizes Fmax"):
        ImplementationObjective(ObjectiveDirection.MAXIMIZE, CostMetric.LUT)


def test_selected_profile_parses_every_frozen_policy_field() -> None:
    contribution = parse_selected_profile(_manifest(_full_profile()), "release")
    assert contribution.backend == BackendRequest(
        BackendKind.SYSTEMVERILOG, RequirementMode.REQUIRED
    )
    assert contribution.target == "std.target.xilinx.xc7z030"
    assert contribution.transforms == TransformPolicy(
        (TransformFamily.DSP, TransformFamily.PIPELINE, TransformFamily.REDUCTION),
        (TransformFamily.REASSOCIATE,),
    )
    assert contribution.objective == ImplementationObjective(
        ObjectiveDirection.MAXIMIZE, CostMetric.FMAX_EST
    )
    assert contribution.architecture == ArchitectureRequest(
        "std.arch.xilinx.dsp48e1", ArchitectureSelectionMode.PREFERRED
    )
    assert contribution.evidence_policy is SourcePolicy.MEASURED_REQUIRED
    assert contribution.formal_policy is FormalPolicy.REQUIRED_BMC
    assert contribution.constraints == (
        ImplementationConstraint(CostMetric.DSP, ConstraintRelation.MAXIMUM, 8),
        ImplementationConstraint(CostMetric.FMAX_EST, ConstraintRelation.MINIMUM, 100.0),
        ImplementationConstraint(CostMetric.INITIATION_INTERVAL, ConstraintRelation.EXACT, 1),
        ImplementationConstraint(CostMetric.LATENCY, ConstraintRelation.MAXIMUM, 4),
    )




def test_unknown_profile_is_structured() -> None:
    with pytest.raises(ImplementationRequestError) as caught:
        parse_selected_profile(_manifest(""), "missing")
    assert caught.value.code == "ZL-IMPL-001"
    assert "unknown implementation profile 'missing'" == str(caught.value)
    assert caught.value.fixes


@pytest.mark.parametrize(
    ("body", "message"),
    (
        ('backend-mode = "required"', "profile backend-mode requires backend"),
        ('backend = "vhdl"', "unknown backend 'vhdl'"),
        ('allowed-transforms = "pipeline"', "allowed-transforms must be an array"),
        (
            'allowed-transforms = ["pipeline"]\navoided-transforms = ["pipeline"]',
            "both allowed and avoided",
        ),
        ('objective = "maximize_lut"', "only maximizes Fmax"),
        ('architecture-mode = "required"', "requires an architecture"),
        ('formal-policy = "maybe"', "unknown formal policy 'maybe'"),
        ('evidence-policy = "guess"', "unknown evidence policy 'guess'"),
    ),
)
def test_profile_rejects_malformed_selected_policy(body: str, message: str) -> None:
    with pytest.raises((ImplementationRequestError, ValueError), match=message):
        parse_selected_profile(_manifest(f"\n[profiles.bad]\n{body}\n"), "bad")


def test_constraint_scalar_defaults_and_explicit_range_normalize() -> None:
    contribution = parse_selected_profile(_manifest('''
[profiles.ranged]
[profiles.ranged.constraints]
latency = 4
ii = { exact = 1 }
fmax = { minimum = 100, maximum = 500 }
'''), "ranged")
    constraints = contribution.constraints or ()
    assert ImplementationConstraint(
        CostMetric.LATENCY, ConstraintRelation.MAXIMUM, 4
    ) in constraints
    assert ImplementationConstraint(
        CostMetric.INITIATION_INTERVAL, ConstraintRelation.EXACT, 1
    ) in constraints
    assert ImplementationConstraint(
        CostMetric.FMAX_EST, ConstraintRelation.MINIMUM, 100
    ) in constraints
    assert ImplementationConstraint(
        CostMetric.FMAX_EST, ConstraintRelation.MAXIMUM, 500
    ) in constraints


@pytest.mark.parametrize(
    ("constraint", "message"),
    (
        ("ii = 0", "II constraint must be a positive integer"),
        ("latency = -1", "latency constraint must be a non-negative integer"),
        ("fmax = 0", "Fmax constraint must be positive"),
        ("latency = { exact = 4, maximum = 5 }", "exact cannot be combined"),
        ("mystery = 1", "unknown implementation constraint 'mystery'"),
    ),
)
def test_profile_rejects_invalid_constraint(constraint: str, message: str) -> None:
    with pytest.raises((ImplementationRequestError, ValueError), match=message):
        parse_selected_profile(_manifest(
            f"\n[profiles.bad.constraints]\n{constraint}\n"
        ), "bad")


def test_identical_contributions_merge_and_provenance_is_not_identity() -> None:
    policy = ImplementationContribution(
        PolicyOrigin("profile one"),
        backend=BackendRequest(BackendKind.SYSTEMVERILOG, RequirementMode.REQUIRED),
        constraints=(ImplementationConstraint(
            CostMetric.LATENCY, ConstraintRelation.MAXIMUM, 4
        ),),
        formal_policy=FormalPolicy.REQUIRED_BMC,
    )
    duplicate = ImplementationContribution(
        PolicyOrigin("CLI"),
        backend=BackendRequest(BackendKind.SYSTEMVERILOG, RequirementMode.REQUIRED),
        constraints=(ImplementationConstraint(
            CostMetric.LATENCY, ConstraintRelation.MAXIMUM, 4
        ),),
        formal_policy=FormalPolicy.REQUIRED_BMC,
    )
    merged = merge_implementation_contributions(policy, duplicate)
    alone = merge_implementation_contributions(policy)
    assert merged.identity == alone.identity
    assert merged.to_identity_data() == alone.to_identity_data()
    assert merged.to_data() != alone.to_data()


def test_disjoint_constraint_metrics_merge() -> None:
    first = ImplementationContribution(
        PolicyOrigin("source"),
        constraints=(ImplementationConstraint(
            CostMetric.LATENCY, ConstraintRelation.MAXIMUM, 4
        ),),
    )
    second = ImplementationContribution(
        PolicyOrigin("profile"),
        constraints=(ImplementationConstraint(
            CostMetric.DSP, ConstraintRelation.MAXIMUM, 8
        ),),
    )
    merged = merge_implementation_contributions(first, second)
    assert {item.metric for item in merged.constraints} == {
        CostMetric.LATENCY, CostMetric.DSP,
    }




def test_conflicting_constraints_name_metric_and_both_origins() -> None:
    left = ImplementationContribution(
        PolicyOrigin("source explore"),
        constraints=(ImplementationConstraint(
            CostMetric.LATENCY, ConstraintRelation.MAXIMUM, 4
        ),),
    )
    right = ImplementationContribution(
        PolicyOrigin("profile fast"),
        constraints=(ImplementationConstraint(
            CostMetric.LATENCY, ConstraintRelation.MAXIMUM, 5
        ),),
    )
    with pytest.raises(ImplementationRequestError) as caught:
        merge_implementation_contributions(left, right)
    assert "constraint.latency" in str(caught.value)
    assert "source explore" in caught.value.notes[0]
    assert "profile fast" in caught.value.notes[1]


def test_omitted_contribution_fields_retain_legacy_defaults() -> None:
    request = merge_implementation_contributions(
        ImplementationContribution(PolicyOrigin("empty CLI"))
    )
    assert request.backend is None
    assert request.target is None
    assert request.transforms == TransformPolicy()
    assert request.constraints == ()
    assert request.objective == ImplementationObjective()
    assert request.architecture == ArchitectureRequest()
    assert request.evidence_policy is SourcePolicy.MEASURED_PREFERRED
    assert request.formal_policy is FormalPolicy.OFF


def test_exact_timing_contract_is_carried_and_cannot_be_changed() -> None:
    request = merge_implementation_contributions(ImplementationContribution(
        PolicyOrigin("profile"),
        constraints=(
            ImplementationConstraint(CostMetric.LATENCY, ConstraintRelation.MAXIMUM, 4),
            ImplementationConstraint(CostMetric.INITIATION_INTERVAL, ConstraintRelation.EXACT, 1),
        ),
    ))
    timed = apply_exact_timing_contract(request, ModuleTimingContract(4, 1))
    assert timed.exact_timing == ExactTiming(4, 1)
    assert timed.identity != request.identity
    assert apply_exact_timing_contract(timed, ExactTiming(4, 1)) == timed
    with pytest.raises(ImplementationRequestError, match="conflicting implementation policy"):
        apply_exact_timing_contract(timed, ExactTiming(3, 1))


@pytest.mark.parametrize(
    "constraint",
    (
        ImplementationConstraint(CostMetric.LATENCY, ConstraintRelation.MAXIMUM, 3),
        ImplementationConstraint(CostMetric.LATENCY, ConstraintRelation.EXACT, 5),
        ImplementationConstraint(CostMetric.INITIATION_INTERVAL, ConstraintRelation.EXACT, 2),
    ),
)
def test_exact_timing_rejects_incompatible_request_constraint(
    constraint: ImplementationConstraint,
) -> None:
    request = merge_implementation_contributions(ImplementationContribution(
        PolicyOrigin("profile"), constraints=(constraint,),
    ))
    with pytest.raises(ImplementationRequestError, match="conflicts with exact module timing"):
        apply_exact_timing_contract(request, ExactTiming(4, 1))


def test_semantic_region_identity_uses_canonical_identity_not_spelling() -> None:
    concise = SemanticRegionIdentity(
        "acme.Fir", "Sample=fixed<16,14>;N=8", "y",
        "canonical-expression-digest", "fixed<16,14>",
    )
    verbose = SemanticRegionIdentity(
        "acme.Fir", "Sample=fixed<16,14>;N=8", "y",
        "canonical-expression-digest", "fixed<16,14>",
    )
    other_output = SemanticRegionIdentity(
        "acme.Fir", "Sample=fixed<16,14>;N=8", "debug",
        "canonical-expression-digest", "fixed<16,14>",
    )
    assert concise.identity == verbose.identity
    assert concise.identity != other_output.identity


def test_request_identity_is_order_independent_and_deterministic() -> None:
    constraints = (
        ImplementationConstraint(CostMetric.DSP, ConstraintRelation.MAXIMUM, 8),
        ImplementationConstraint(CostMetric.LATENCY, ConstraintRelation.MAXIMUM, 4),
    )
    left = merge_implementation_contributions(ImplementationContribution(
        PolicyOrigin("left"), constraints=constraints,
        transforms=TransformPolicy((TransformFamily.PIPELINE, TransformFamily.DSP)),
    ))
    right = merge_implementation_contributions(ImplementationContribution(
        PolicyOrigin("right"), constraints=tuple(reversed(constraints)),
        transforms=TransformPolicy((TransformFamily.DSP, TransformFamily.PIPELINE)),
    ))
    assert left.identity == right.identity
    assert len(left.identity) == 64
    assert left.to_identity_data() == right.to_identity_data()


def test_unified_constraint_adapter_preserves_exact_relation() -> None:
    request = merge_implementation_contributions(ImplementationContribution(
        PolicyOrigin("profile"),
        constraints=(ImplementationConstraint(
            CostMetric.INITIATION_INTERVAL, ConstraintRelation.EXACT, 1
        ),),
    ))
    assert request.unified_constraints() == (
        # Existing M28 represents exact as identical lower/upper bounds.
        request.constraints[0].as_unified(),
    )
    assert request.unified_constraints()[0].minimum == 1
    assert request.unified_constraints()[0].maximum == 1
