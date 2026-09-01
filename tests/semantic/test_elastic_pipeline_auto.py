"""Frozen semantics for the bounded globally-stalled RV pipeline slice."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from zlang.backend.module_features import ModuleFeatureKind, module_feature_inventory
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compilation_session import CompilationSession
from zlang.cross_backend import validate_module_route
from zlang.equivalence import publish_bindings
from zlang.formal import build_formal_design
from zlang.formal_exploration import FormalPolicy
from zlang.implementation_plans import (
    BackendImplementationPlanningError,
    BackendPlanStatus,
    plan_backend_implementations,
)
from zlang.ir.cross_backend import CrossBackendError
from zlang.ir.cdc import ClockEdge
from zlang.ir.elastic import ElasticStallPolicy
from zlang.ir.equivalence import BindingSide, EquivalenceError
from zlang.ir.formal import FormalStatus
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import PortDirection
from zlang.ir.types import UIntType
from zlang.opt import lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples" / "elastic_pipeline_auto.zl").read_text()


def _module():
    return analyze(parse(SOURCE))


def test_transform_is_one_typed_canonical_region_with_exact_inventory() -> None:
    syntax = parse(SOURCE)
    assert syntax.connections[0].transform is not None
    module = analyze(syntax)
    assert module.assignments == ()
    assert module.pipeline_explorations == ()
    assert len(module.elastic_pipeline_regions) == 1
    region = module.elastic_pipeline_regions[0]
    assert region.source_endpoint == "input"
    assert region.destination_endpoint == "output"
    assert region.selected == "balanced_levels_dsp"
    assert region.timing.minimum_unstalled_latency == 3
    assert region.timing.ii_no_stall == 1
    assert region.timing.capacity == 3
    assert region.timing.variable_wall_clock_latency
    assert region.timing.stall_policy is ElasticStallPolicy.GLOBAL_CLOCK_ENABLE
    assert region.plan.valid_stage_count == 3
    assert region.plan.ready_control_lut_estimate == 2
    assert region.selected_candidate.estimate.ff >= region.plan.valid_stage_count
    assert len(region.plan.data_stage_instances) == 7
    inventory = module_feature_inventory(module)
    elastic = inventory.of_kind(ModuleFeatureKind.ELASTIC_PIPELINE)
    assert len(elastic) == 1
    assert elastic[0].identity == region.semantic_id
    assert restore(lower(module)) == module


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            lambda canonical, region: replace(
                canonical,
                elastic_pipeline_regions=(
                    replace(region, source_endpoint="missing"),
                ),
            ),
            "endpoints do not resolve uniquely",
        ),
        (
            lambda canonical, region: replace(
                canonical,
                elastic_pipeline_regions=(
                    replace(region, destination_endpoint="input"),
                ),
            ),
            "source and destination endpoints must be distinct",
        ),
        (
            lambda canonical, region: replace(
                canonical,
                clock="other_clock",
            ),
            "clock/reset disagrees",
        ),
        (
            lambda canonical, region: replace(
                canonical,
                clock_domains=(
                    replace(canonical.clock_domains[0], reset="other_reset"),
                ),
            ),
            "share one clock domain",
        ),
        (
            lambda canonical, region: replace(
                canonical,
                clock_domains=(
                    replace(canonical.clock_domains[0], edge=ClockEdge.FALLING),
                ),
            ),
            "rising-edge synchronous active-high",
        ),
    ),
)
def test_canonical_elastic_endpoint_and_domain_corruption_is_rejected(
    mutation, message: str,
) -> None:
    canonical = lower(_module())
    region = canonical.elastic_pipeline_regions[0]
    with pytest.raises(ValueError, match=message):
        mutation(canonical, region)


@pytest.mark.parametrize(
    ("port_mutation", "message"),
    (
        (
            lambda port: replace(port, direction=PortDirection.OUTPUT),
            "source endpoint must be a ready/valid input",
        ),
        (
            lambda port: replace(port, protocol=InterfaceProtocol.WIRE),
            "source endpoint must be a ready/valid input",
        ),
        (
            lambda port: replace(port, type=UIntType(8)),
            "source endpoint payload type disagrees",
        ),
        (
            lambda port: replace(port, domain="other_clock"),
            "share one clock domain",
        ),
    ),
)
def test_canonical_elastic_source_abi_corruption_is_rejected(
    port_mutation, message: str,
) -> None:
    canonical = lower(_module())
    ports = (
        port_mutation(canonical.ports[0]),
        *canonical.ports[1:],
    )
    with pytest.raises(ValueError, match=message):
        replace(canonical, ports=ports)


@pytest.mark.parametrize(
    ("port_mutation", "message"),
    (
        (
            lambda port: replace(port, direction=PortDirection.INPUT),
            "destination endpoint must be a ready/valid output",
        ),
        (
            lambda port: replace(port, protocol=InterfaceProtocol.WIRE),
            "destination endpoint must be a ready/valid output",
        ),
        (
            lambda port: replace(port, type=UIntType(8)),
            "destination endpoint payload type disagrees",
        ),
    ),
)
def test_canonical_elastic_destination_abi_corruption_is_rejected(
    port_mutation, message: str,
) -> None:
    canonical = lower(_module())
    ports = (
        canonical.ports[0],
        port_mutation(canonical.ports[1]),
    )
    with pytest.raises(ValueError, match=message):
        replace(canonical, ports=ports)


def test_canonical_elastic_plan_stage_identity_corruption_is_rejected() -> None:
    canonical = lower(_module())
    region = canonical.elastic_pipeline_regions[0]
    stages = list(region.plan.data_stage_instances)
    stages[0] = (stages[-1][0] + 100, stages[0][1])
    changed_plan = replace(
        region.plan,
        data_stage_instances=tuple(sorted(stages)),
    )
    with pytest.raises(ValueError, match="data stages disagree with its plan"):
        replace(
            canonical,
            elastic_pipeline_regions=(replace(region, plan=changed_plan),),
        )


def test_canonical_elastic_empty_stage_array_is_rejected() -> None:
    canonical = lower(_module())
    region = canonical.elastic_pipeline_regions[0]
    with pytest.raises(ValueError, match="data stages disagree with its plan"):
        replace(
            canonical,
            elastic_pipeline_regions=(
                replace(
                    region,
                    plan=replace(region.plan, data_stage_instances=()),
                ),
            ),
        )


def test_canonical_elastic_coordinated_latency_corruption_is_rejected() -> None:
    canonical = lower(_module())
    region = canonical.elastic_pipeline_regions[0]
    candidates = tuple(
        replace(
            candidate,
            latency=4,
            pipeline_plan=replace(
                candidate.pipeline_plan,
                inserted_registers=4,
            ),
        )
        if candidate.name == region.selected
        else candidate
        for candidate in region.candidates
    )
    changed = replace(
        region,
        candidates=candidates,
        plan=replace(region.plan, latency=4, valid_stage_count=4),
        timing=replace(
            region.timing,
            minimum_unstalled_latency=4,
            capacity=4,
        ),
    )
    with pytest.raises(ValueError, match="metadata disagrees with its expression"):
        replace(canonical, elastic_pipeline_regions=(changed,))


def test_malformed_canonical_elastic_region_cannot_publish_an_artifact() -> None:
    canonical = lower(_module())
    region = canonical.elastic_pipeline_regions[0]
    with pytest.raises(ValueError, match="endpoints do not resolve uniquely"):
        corrupted = replace(
            canonical,
            elastic_pipeline_regions=(
                replace(region, source_endpoint="missing"),
            ),
        )
        emit_sv_artifact(restore(corrupted))


def test_elastic_region_keeps_m35_public_rv_safety_only() -> None:
    properties = build_formal_design(_module()).properties
    assert tuple(item.generated_from for item in properties) == (
        "ready_valid:input",
        "ready_valid:output",
    )
    assert all("elastic" not in item.id for item in properties)


def test_elastic_region_is_explicitly_outside_m36_and_m38() -> None:
    module = _module()
    names = {
        "clock": "clk",
        "reset": "rst",
        **{f"port:{port.name}": port.name for port in module.ports},
    }
    with pytest.raises(EquivalenceError, match="variable-latency elastic"):
        publish_bindings(
            module,
            side=BindingSide.IMPLEMENTATION,
            selected_ir_identity="selected",
            backend="direct_systemverilog",
            artifact_hash_value="artifact",
            rtl_names=names,
        )
    with pytest.raises(CrossBackendError, match="variable-latency elastic"):
        validate_module_route(module)


def test_m39_available_skips_without_disqualifying_and_required_fails() -> None:
    verifier_calls = 0

    def verifier(*args, **kwargs):
        nonlocal verifier_calls
        verifier_calls += 1
        raise AssertionError("elastic M39 route must not execute M36")

    available = CompilationSession(
        SOURCE,
        formal_policy=FormalPolicy.AVAILABLE,
        formal_verifier=verifier,
        include_clash=False,
    ).selected_ir
    records = available.elastic_pipeline_regions[0].formal_records
    assert len(records) == 1
    assert records[0].status is FormalStatus.SKIPPED
    assert records[0].eligible
    assert records[0].rank == 1
    assert records[0].cache_state == "not-run"
    assert records[0].formal_route == "unsupported_variable_latency_elastic"
    assert verifier_calls == 0

    for policy in (FormalPolicy.REQUIRED_BMC, FormalPolicy.REQUIRED_PROVEN):
        with pytest.raises(SemanticError, match="no M36 route"):
            CompilationSession(
                SOURCE,
                formal_policy=policy,
                formal_verifier=verifier,
                include_clash=False,
            ).selected_ir
    assert verifier_calls == 0


def test_physical_mapping_falls_back_only_when_preferred() -> None:
    module = _module()
    preferred = plan_backend_implementations(
        module,
        backend_requests=(("systemverilog", "preferred"),),
        architecture="not-a-real-architecture",
        architecture_mode="preferred",
    )
    plan = preferred.plan_for("systemverilog")
    assert plan.status is BackendPlanStatus.GENERIC_FALLBACK
    assert "clock-enable/stall" in plan.reason

    with pytest.raises(
        BackendImplementationPlanningError,
        match="required backend implementation is unavailable",
    ):
        plan_backend_implementations(
            module,
            backend_requests=(("systemverilog", "required"),),
            architecture="not-a-real-architecture",
            architecture_mode="required",
        )


@pytest.mark.parametrize(
    ("extra", "message"),
    (
        ("reg state : u8 = 0", "cannot be mixed with user state"),
        (
            "in side : u8",
            "supports exactly two ready/valid ports",
        ),
    ),
)
def test_user_owned_state_and_extra_runtime_inputs_are_rejected(
    extra: str, message: str
) -> None:
    broken = SOURCE.replace("    in input : rv<ElasticProductsInput>", f"    {extra}\n\n    in input : rv<ElasticProductsInput>")
    with pytest.raises(SemanticError, match=message):
        analyze(parse(broken))


def test_protocol_control_capture_is_rejected() -> None:
    broken = SOURCE.replace(
        "input.payload.g * input.payload.h",
        "mux(input.valid, input.payload.g, input.payload.h) * input.payload.h",
    )
    with pytest.raises(SemanticError, match="may capture only.*input.payload"):
        analyze(parse(broken))


@pytest.mark.parametrize(
    "physical_declarations",
    (
        "clock clk { edge falling }\n    reset rst @clk",
        (
            "clock clk\n    reset rst @clk { mode asynchronous "
            "polarity active_high power_up unspecified }"
        ),
        (
            "clock clk\n    reset rst @clk { mode synchronous "
            "polarity active_low power_up unspecified }"
        ),
        (
            "clock clk\n    reset rst @clk { mode synchronous "
            "polarity active_high power_up reset }"
        ),
    ),
)
def test_non_common_physical_clock_reset_contracts_fail_semantically(
    physical_declarations: str,
) -> None:
    broken = SOURCE.replace("clock clk\n    reset rst", physical_declarations)
    with pytest.raises(SemanticError, match="rising-edge.*synchronous active-high"):
        analyze(parse(broken))
