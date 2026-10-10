"""Bounded ready/valid temporal sharing regression coverage."""

from __future__ import annotations

import random
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from zlang.backend.systemverilog import (
    emit_artifact,
    emit_artifact_with_source_map,
    emit_experimental,
    emit_formal_artifact,
)
from zlang.backend.systemverilog.expression import render_expression
from zlang.compiler import compile_source
from zlang.compilation_session import CompilationSession
from zlang.formal import build_recursive_formal_design
from zlang.formal_exploration import FormalPolicy
from zlang.formal_orchestration import collect_formal_selection_evidence
from zlang.formal_temporal_stream import (
    CapacityOneTransactionVerifier,
    build_capacity_one_transaction_relation,
    emit_capacity_one_transaction_miter,
)
from zlang.ir.formal import FormalResult, FormalStatus, ProofMode
from zlang.native_simulation import simulate_cycles
from zlang.semantic import SemanticError
from tests.simulation.differential import run_differential
from zlang.shared_arithmetic import allocate_temporal_registers
from zlang.ir.temporal_admission import (
    TemporalAdmissionPolicy,
    derive_noninterleaved_admission,
)
from zlang.ir.temporal_storage import (
    RegisterBinding,
    ValueLifetime,
    build_temporal_storage_plan,
)
from zlang.ir.types import UIntType


ROOT = Path(__file__).resolve().parents[2]
PUBLIC_EXAMPLE = ROOT / "examples" / "temporal_shared_multiply.zhl"


SOURCE = """
struct PairInput {
    a : u16
    b : u16
    c : u16
    d : u16
}

module SharedTemporalMultiply {
    clock clk
    reset rst

    in input : rv<PairInput>
    out output : rv<u33>

    input -> output {
        transform pipeline(auto) {
            implement {
                input.payload.a * input.payload.b
                    + input.payload.c * input.payload.d
                intent {
                    latency <= 4
                    ii <= 4
                    dsp <= 1
                    minimize dsp
                }
            }
        }
    }
}
"""


SIGNED_SOURCE = (
    SOURCE.replace("SharedTemporalMultiply", "SignedSharedTemporalMultiply")
    .replace("u16", "s16")
    .replace("u33", "s33")
)

FORMAL_SOURCE = (
    SOURCE.replace("SharedTemporalMultiply", "FormalSharedTemporalMultiply")
    .replace("u16", "u2")
    .replace("u33", "u5")
)

TEMPORAL_READY_LOOP = (
    SOURCE.replace("module SharedTemporalMultiply", "module TemporalLeaf")
    + """
module TemporalAdapter {
    in in_word : rv<u33>
    out out_pair : rv<PairInput>
    in_word.ready = out_pair.ready
    out_pair.valid = in_word.valid
    out_pair.payload = PairInput { a=0 b=0 c=0 d=0 }
}

module TemporalReadyLoop {
    clock clk
    reset rst
    inst temporal : TemporalLeaf
    inst adapter : TemporalAdapter
    connect temporal.output -> adapter.in_word
    connect adapter.out_pair -> temporal.input
}
"""
)


def _input(a: int, b: int, c: int, d: int, *, valid: int = 1, ready: int = 1):
    return {
        "input": {"payload": {"a": a, "b": b, "c": c, "d": d}, "valid": valid},
        "output": {"ready": ready},
    }


def _module():
    return compile_source(SOURCE).ir


def test_public_temporal_shared_multiply_example_emits_and_simulates(
    tmp_path: Path,
) -> None:
    """The documented public-alpha witness remains an executable contract."""

    module = compile_source(PUBLIC_EXAMPLE.read_text(encoding="utf-8")).ir
    graph = module.elastic_pipeline_regions[0].temporal_graph
    assert graph is not None
    assert (graph.latency, graph.initiation_interval, graph.capacity) == (4, 4, 1)
    artifact = emit_artifact(module)
    assert "zlang_temporal_" in artifact.text
    trace = simulate_cycles(
        module,
        [_input(0, 0, 0, 0, valid=0), _input(2, 3, 4, 5),
         *(_input(0, 0, 0, 0, valid=0) for _ in range(5))],
        reset=[True, False, False, False, False, False, False],
    )
    assert [row["output"]["payload"] for row in trace if row["output"]["transfer"]] == [26]
    events = (
        {
            "reset": {"rst": True},
            "set": {
                "input": {"payload": {"a": 0, "b": 0, "c": 0, "d": 0}, "valid": 0},
                "output": {"ready": 1},
            },
            "edges": ("clk",),
        },
        {
            "reset": {"rst": False},
            "set": {
                "input": {"payload": {"a": 2, "b": 3, "c": 4, "d": 5}, "valid": 1},
                "output": {"ready": 1},
            },
            "edges": ("clk",),
        },
        *(
            {
                "set": {
                    "input": {
                        "payload": {"a": 0, "b": 0, "c": 0, "d": 0},
                        "valid": 0,
                    },
                    "output": {"ready": 1},
                },
                "edges": ("clk",),
            }
            for _ in range(5)
        ),
    )
    differential = run_differential(
        PUBLIC_EXAMPLE,
        top="TemporalSharedMultiply",
        events=events,
        directory=tmp_path / "verilator",
        timeout=60,
    )
    assert differential.native == differential.direct_sv


def test_shared_candidate_is_flow_controlled_and_identity_bears_schedule() -> None:
    region = _module().elastic_pipeline_regions[0]
    graph = region.temporal_graph
    assert graph is not None
    assert region.timing.minimum_unstalled_latency == 4
    assert region.timing.ii_no_stall == 4
    assert region.timing.capacity == 1
    assert graph.temporal_class.value == "flow_controlled"
    assert tuple((item.operation_id, item.start_cycle) for item in graph.operations) == (
        ("mul0", 1), ("mul1", 2), ("add0", 3),
    )
    assert [item.physical_resource_id for item in graph.resource_bindings] == [
        "multiply0", "multiply0", "add0",
    ]
    assert {item.value_id for item in graph.value_lifetimes} == {
        "input", "mul0", "mul1", "result",
    }
    assert {item.value_id for item in graph.register_bindings} == {
        "input", "mul0", "mul1", "result",
    }
    assert graph.resource_cost.dsp == 1
    assert graph.resource_cost.ff == graph.storage_plan.ff_cost
    assert len(graph.storage_plan.registers) == 4
    assert {item.value_ids[0] for item in graph.storage_plan.registers} == {
        "input", "mul0", "mul1", "result",
    }
    assert graph.implementation_identity.startswith("temporal:")


def test_interval_allocator_shares_only_non_overlapping_compatible_values() -> None:
    bindings = allocate_temporal_registers((
        ValueLifetime("first", 0, 1, 16, False),
        ValueLifetime("overlap", 1, 2, 16, False),
        ValueLifetime("later", 2, 3, 16, False),
        ValueLifetime("signed", 2, 3, 16, True),
        ValueLifetime("wide", 2, 3, 17, False),
    ))
    by_value = {item.value_id: item.physical_register_id for item in bindings}
    assert by_value["first"] == by_value["later"]
    assert by_value["first"] != by_value["overlap"]
    assert by_value["later"] != by_value["signed"]
    assert by_value["later"] != by_value["wide"]


def test_storage_plan_charges_unique_registers_once_and_rejects_overlap() -> None:
    lifetimes = (
        ValueLifetime("first", 0, 1, 16, False),
        ValueLifetime("later", 2, 3, 16, False),
    )
    bindings = allocate_temporal_registers(lifetimes)
    storage = build_temporal_storage_plan(
        lifetimes=lifetimes,
        bindings=bindings,
        value_types={"first": UIntType(16), "later": UIntType(16)},
        control_ff=3,
    )
    assert len(storage.registers) == 1
    assert storage.registers[0].value_ids == ("first", "later")
    assert storage.ff_cost == 19
    with pytest.raises(ValueError, match="overlapping temporal values"):
        build_temporal_storage_plan(
            lifetimes=(
                ValueLifetime("left", 0, 2, 16, False),
                ValueLifetime("right", 2, 3, 16, False),
            ),
            bindings=(
                # Deliberately bypass the allocator to test fail-closed plan validation.
                RegisterBinding("left", "shared_reg_0"),
                RegisterBinding("right", "shared_reg_0"),
            ),
            value_types={"left": UIntType(16), "right": UIntType(16)},
            control_ff=0,
        )
    with pytest.raises(ValueError, match="signedness"):
        build_temporal_storage_plan(
            lifetimes=(ValueLifetime("bad", 0, 1, 16, True),),
            bindings=(RegisterBinding("bad", "shared_reg_0"),),
            value_types={"bad": UIntType(16)},
            control_ff=0,
        )


def test_retire_reload_ii_is_derived_from_result_schedule_edge() -> None:
    admission = derive_noninterleaved_admission(
        final_result_cycle=3,
        policy=TemporalAdmissionPolicy.RETIRE_AND_RELOAD,
    )
    assert (admission.latency, admission.initiation_interval, admission.capacity) == (4, 4, 1)
    conservative = derive_noninterleaved_admission(
        final_result_cycle=3,
        policy=TemporalAdmissionPolicy.BLOCK_UNTIL_IDLE,
    )
    assert (
        conservative.latency,
        conservative.initiation_interval,
        conservative.capacity,
    ) == (4, 5, 1)


def test_temporal_identity_and_rtl_are_stable_and_isolate_how_from_what() -> None:
    first = _module()
    second = _module()
    first_region = first.elastic_pipeline_regions[0]
    second_region = second.elastic_pipeline_regions[0]
    first_graph = first_region.temporal_graph
    second_graph = second_region.temporal_graph
    assert first_graph is not None and second_graph is not None
    assert first_region.semantic_id == second_region.semantic_id
    assert first_graph == second_graph
    assert first_graph.implementation_identity == second_graph.implementation_identity
    assert first_graph.storage_plan == second_graph.storage_plan
    first_artifact = emit_artifact(first)
    second_artifact = emit_artifact(second)
    assert first_artifact.text == second_artifact.text
    assert first_artifact.artifact_hash == second_artifact.artifact_hash
    assert first_artifact.build_identity == second_artifact.build_identity
    _, first_source_map = emit_artifact_with_source_map(first)
    _, second_source_map = emit_artifact_with_source_map(second)
    assert first_source_map.to_json() == second_source_map.to_json()

    changed_schedule = first_graph.identity_data(
        semantic_region_identity=first_graph.semantic_region_identity,
        operations=(
            replace(first_graph.operations[0], start_cycle=2, result_cycle=2),
            *first_graph.operations[1:],
        ),
        dependencies=first_graph.dependencies,
        resource_bindings=first_graph.resource_bindings,
        value_lifetimes=first_graph.value_lifetimes,
        register_bindings=first_graph.register_bindings,
        storage_plan=first_graph.storage_plan,
        admission_policy=first_graph.admission_policy,
        latency=first_graph.latency,
        initiation_interval=first_graph.initiation_interval,
        capacity=first_graph.capacity,
        temporal_class=first_graph.temporal_class,
        resource_cost=first_graph.resource_cost,
    )
    changed_admission = first_graph.identity_data(
        semantic_region_identity=first_graph.semantic_region_identity,
        operations=first_graph.operations,
        dependencies=first_graph.dependencies,
        resource_bindings=first_graph.resource_bindings,
        value_lifetimes=first_graph.value_lifetimes,
        register_bindings=first_graph.register_bindings,
        storage_plan=first_graph.storage_plan,
        admission_policy=TemporalAdmissionPolicy.BLOCK_UNTIL_IDLE,
        latency=4,
        initiation_interval=5,
        capacity=1,
        temporal_class=first_graph.temporal_class,
        resource_cost=first_graph.resource_cost,
    )
    changed_resource = first_graph.identity_data(
        semantic_region_identity=first_graph.semantic_region_identity,
        operations=first_graph.operations,
        dependencies=first_graph.dependencies,
        resource_bindings=(
            replace(first_graph.resource_bindings[0], physical_resource_id="multiply1"),
            *first_graph.resource_bindings[1:],
        ),
        value_lifetimes=first_graph.value_lifetimes,
        register_bindings=first_graph.register_bindings,
        storage_plan=first_graph.storage_plan,
        admission_policy=first_graph.admission_policy,
        latency=first_graph.latency,
        initiation_interval=first_graph.initiation_interval,
        capacity=first_graph.capacity,
        temporal_class=first_graph.temporal_class,
        resource_cost=first_graph.resource_cost,
    )
    changed_register = first_graph.identity_data(
        semantic_region_identity=first_graph.semantic_region_identity,
        operations=first_graph.operations,
        dependencies=first_graph.dependencies,
        resource_bindings=first_graph.resource_bindings,
        value_lifetimes=first_graph.value_lifetimes,
        register_bindings=(
            replace(first_graph.register_bindings[0], physical_register_id="changed"),
            *first_graph.register_bindings[1:],
        ),
        storage_plan=first_graph.storage_plan,
        admission_policy=first_graph.admission_policy,
        latency=first_graph.latency,
        initiation_interval=first_graph.initiation_interval,
        capacity=first_graph.capacity,
        temporal_class=first_graph.temporal_class,
        resource_cost=first_graph.resource_cost,
    )
    changed_capacity = first_graph.identity_data(
        semantic_region_identity=first_graph.semantic_region_identity,
        operations=first_graph.operations,
        dependencies=first_graph.dependencies,
        resource_bindings=first_graph.resource_bindings,
        value_lifetimes=first_graph.value_lifetimes,
        register_bindings=first_graph.register_bindings,
        storage_plan=first_graph.storage_plan,
        admission_policy=first_graph.admission_policy,
        latency=first_graph.latency,
        initiation_interval=first_graph.initiation_interval,
        capacity=2,
        temporal_class=first_graph.temporal_class,
        resource_cost=first_graph.resource_cost,
    )
    assert changed_schedule != first_graph.implementation_identity
    assert changed_admission != first_graph.implementation_identity
    assert changed_resource != first_graph.implementation_identity
    assert changed_register != first_graph.implementation_identity
    assert changed_capacity != first_graph.implementation_identity
    assert first_region.semantic_id == second_region.semantic_id


def test_native_shared_candidate_snapshots_input_and_holds_pending_output() -> None:
    cycles = [
        _input(0, 0, 0, 0, valid=0),
        _input(2, 3, 4, 5),
        # These values must not alter the accepted transaction while ready=0.
        _input(100, 100, 100, 100),
        _input(101, 101, 101, 101),
        _input(102, 102, 102, 102, ready=0),
        _input(103, 103, 103, 103, ready=0),
        _input(104, 104, 104, 104, ready=0),
        _input(105, 105, 105, 105, ready=1),
        _input(7, 2, 3, 4, ready=1),
        _input(0, 0, 0, 0, valid=0, ready=1),
        _input(0, 0, 0, 0, valid=0, ready=1),
        _input(0, 0, 0, 0, valid=0, ready=1),
        _input(0, 0, 0, 0, valid=0, ready=1),
        _input(0, 0, 0, 0, valid=0, ready=1),
    ]
    trace = simulate_cycles(_module(), cycles, reset=[True] + [False] * (len(cycles) - 1))
    # Capacity one: only the first transaction enters before its stalled
    # result retires.  Output is stable while downstream blocks it.
    assert [item["input"]["transfer"] for item in trace[:7]] == [0, 1, 0, 0, 0, 0, 0]
    assert [trace[index]["output"] for index in (5, 6)] == [
        {"payload": 26, "valid": 1, "transfer": 0},
        {"payload": 26, "valid": 1, "transfer": 0},
    ]
    assert trace[7]["output"]["transfer"] == 1
    # Retire and reload share one edge.  Capacity remains one: the old output
    # is retired while the new payload is captured for a later result.
    assert trace[7]["input"]["transfer"] == 1
    assert [
        (index, item["output"]["payload"])
        for index, item in enumerate(trace)
        if item["output"]["transfer"]
    ] == [(7, 26), (11, 22050)]


def test_unstalled_temporal_latency_and_ii_follow_transfers() -> None:
    """Latency and II are observed at transfers, not inferred from comments."""

    trace = simulate_cycles(
        _module(),
        tuple(_input(value, 1, 0, 0) for value in range(1, 15)),
        reset=[True, *(False for _ in range(13))],
    )
    accepted = [
        index for index, row in enumerate(trace) if row["input"]["transfer"]
    ]
    retired = [
        index for index, row in enumerate(trace) if row["output"]["transfer"]
    ]
    assert accepted == [1, 5, 9, 13]
    assert retired == [5, 9, 13]
    assert [retired[index] - accepted[index] for index in range(len(retired))] == [4] * 3
    assert [later - earlier for earlier, later in zip(accepted, accepted[1:])] == [4] * 3


def test_native_shared_candidate_preserves_transaction_order_under_random_stalls() -> None:
    rng = random.Random(0x5A17)
    cycles = []
    submitted: list[int] = []
    for _ in range(48):
        a, b, c, d = (rng.randrange(1 << 16) for _ in range(4))
        valid = rng.randrange(2)
        cycles.append(_input(a, b, c, d, valid=valid, ready=rng.randrange(2)))
        submitted.append(a * b + c * d)
    cycles.extend(_input(0, 0, 0, 0, valid=0, ready=1) for _ in range(12))
    trace = simulate_cycles(_module(), cycles, reset=[True] + [False] * (len(cycles) - 1))
    accepted = [
        submitted[index]
        for index, item in enumerate(trace[:48])
        if item["input"]["transfer"]
    ]
    received = [
        item["output"]["payload"]
        for item in trace
        if item["output"]["transfer"]
    ]
    assert received == accepted


def test_signed_shared_candidate_preserves_exact_signed_payload(tmp_path: Path) -> None:
    trace = simulate_cycles(
        compile_source(SIGNED_SOURCE).ir,
        [
            _input(0, 0, 0, 0, valid=0),
            _input(-2, 3, 4, -5),
            *(_input(0, 0, 0, 0, valid=0) for _ in range(5)),
        ],
        reset=[True, False, False, False, False, False, False],
    )
    transfers = [
        row["output"]["payload"]
        for row in trace
        if row["output"]["transfer"]
    ]
    assert transfers == [-26]
    source = tmp_path / "signed_temporal_shared.zhl"
    source.write_text(SIGNED_SOURCE, encoding="utf-8")
    events = (
        {
            "reset": {"rst": True},
            "set": {
                "input": {"payload": {"a": 0, "b": 0, "c": 0, "d": 0}, "valid": 0},
                "output": {"ready": 1},
            },
            "edges": ("clk",),
        },
        {
            "reset": {"rst": False},
            "set": {
                "input": {"payload": {"a": -2, "b": 3, "c": 4, "d": -5}, "valid": 1},
                "output": {"ready": 1},
            },
            "edges": ("clk",),
        },
        *(
            {
                "set": {
                    "input": {"payload": {"a": 0, "b": 0, "c": 0, "d": 0}, "valid": 0},
                    "output": {"ready": 1},
                },
                "edges": ("clk",),
            }
            for _ in range(5)
        ),
    )
    differential = run_differential(
        source,
        top="SignedSharedTemporalMultiply",
        events=events,
        directory=tmp_path / "signed-verilator",
        timeout=60,
    )
    assert differential.native == differential.direct_sv


def test_randomized_native_verilator_transaction_scoreboard(tmp_path: Path) -> None:
    """Differential RTL evidence plus a transfer-indexed semantic scoreboard."""

    rng = random.Random(0xA114)
    cycles = []
    resets = []
    events = []
    for index in range(36):
        a, b, c, d = (rng.randrange(1 << 16) for _ in range(4))
        reset = index in {0, 17}
        valid = rng.randrange(2)
        ready = rng.randrange(2)
        values = _input(a, b, c, d, valid=valid, ready=ready)
        cycles.append(values)
        resets.append(reset)
        events.append({
            "reset": {"rst": reset},
            "set": {
                "input": {
                    "payload": {"a": a, "b": b, "c": c, "d": d},
                    "valid": valid,
                },
                "output": {"ready": ready},
            },
            "edges": ("clk",),
        })
    # Drain the final capacity-one epoch after randomized stalls.
    for _ in range(8):
        cycles.append(_input(0, 0, 0, 0, valid=0, ready=1))
        resets.append(False)
        events.append({
            "set": {
                "input": {"payload": {"a": 0, "b": 0, "c": 0, "d": 0}, "valid": 0},
                "output": {"ready": 1},
            },
            "edges": ("clk",),
        })

    expected: list[int] = []
    trace = simulate_cycles(_module(), cycles, reset=resets)
    for reset, values, row in zip(resets, cycles, trace, strict=True):
        if reset:
            expected.clear()
        if row["output"]["transfer"]:
            assert expected
            assert row["output"]["payload"] == expected.pop(0)
        if row["input"]["transfer"]:
            payload = values["input"]["payload"]
            expected.append(
                payload["a"] * payload["b"] + payload["c"] * payload["d"]
            )
        assert len(expected) <= 1

    source = tmp_path / "temporal_shared.zhl"
    source.write_text(SOURCE, encoding="utf-8")
    differential = run_differential(
        source,
        top="SharedTemporalMultiply",
        events=tuple(events),
        directory=tmp_path / "verilator",
        timeout=60,
    )
    assert differential.native == differential.direct_sv


def test_scalar_implement_cannot_select_ii_greater_than_one() -> None:
    scalar = """
module ScalarNoTemporal {
    clock clk
    reset rst
    in a : u16
    in b : u16
    in c : u16
    in d : u16
    out result : u33
    result = implement {
        a * b + c * d
        intent { ii == 4 dsp <= 1 minimize dsp }
    }
}
"""
    with pytest.raises(SemanticError, match="no implementation satisfies constraints"):
        compile_source(scalar)


def test_ii_one_constraint_selects_the_spatial_control_candidate() -> None:
    source = SOURCE.replace("ii <= 4", "ii == 1").replace("dsp <= 1", "dsp <= 2")
    region = compile_source(source).ir.elastic_pipeline_regions[0]
    temporal_region = _module().elastic_pipeline_regions[0]
    assert region.selected == "spatial_mul2"
    assert region.temporal_graph is None
    assert region.timing.ii_no_stall == 1
    # Source value/region identity is WHAT; the temporal graph's identity is
    # separately HOW/WHEN/WHERE.
    assert region.semantic_id == temporal_region.semantic_id


def test_temporal_candidate_uses_only_transaction_stream_bmc_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls = []

    def bounded_pass(_source: str, **kwargs):
        calls.append(kwargs["property_id"])
        return FormalResult(
            kwargs["property_id"],
            FormalStatus.BOUNDED_PASS,
            ProofMode.BMC,
            kwargs["engine"],
            kwargs["solver"],
            kwargs["depth"],
        )

    monkeypatch.setattr(
        "zlang.formal_temporal_stream.run_verilog_formal",
        bounded_pass,
    )
    selected = CompilationSession(
        FORMAL_SOURCE,
        formal_policy=FormalPolicy.AVAILABLE,
        formal_depth=10,
        formal_cache=tmp_path / "cache",
    ).selected_ir
    record = selected.elastic_pipeline_regions[0].formal_records[0]
    assert record.formal_route == "transaction_stream_equivalence_bmc"
    assert record.status is FormalStatus.BOUNDED_PASS
    assert record.mode is ProofMode.BMC
    assert record.property_identity is not None
    assert record.implementation_artifact_hash == record.artifact_hash
    assert record.reference_artifact_hash is not None
    assert len(calls) == 1

    required_session = CompilationSession(
        FORMAL_SOURCE,
        formal_policy=FormalPolicy.REQUIRED_BMC,
        formal_depth=10,
        formal_cache=tmp_path / "cache",
    )
    required = required_session.selected_ir
    required_record = required.elastic_pipeline_regions[0].formal_records[0]
    assert required_record.status is FormalStatus.BOUNDED_PASS
    assert required_record.eligible
    assert required_record.cache_state == "hit"
    assert len(calls) == 1
    evidence = collect_formal_selection_evidence(required_session.materialize())
    assert len(evidence) == 1
    assert evidence[0].route == "transaction_stream_equivalence_bmc"
    assert evidence[0].status == "bounded_pass"

    with pytest.raises(
        SemanticError,
        match="required_proven is unavailable.*bounded BMC",
    ):
        CompilationSession(
            FORMAL_SOURCE,
            formal_policy=FormalPolicy.REQUIRED_PROVEN,
        ).selected_ir


def test_temporal_proof_identity_binds_value_implementation_and_artifact() -> None:
    first_module = compile_source(FORMAL_SOURCE).ir
    changed_module = compile_source(
        FORMAL_SOURCE.replace(
            "input.payload.c * input.payload.d",
            "input.payload.a * input.payload.d",
        )
    ).ir
    first_region = first_module.elastic_pipeline_regions[0]
    changed_region = changed_module.elastic_pipeline_regions[0]
    first = CapacityOneTransactionVerifier(first_module, first_region)
    changed = CapacityOneTransactionVerifier(changed_module, changed_region)
    from zlang.formal_exploration import FormalExplorationConfig

    config = FormalExplorationConfig(
        policy=FormalPolicy.AVAILABLE,
        bmc_depth=10,
    )
    first_identity = first.cache_identity(object(), config)
    changed_identity = changed.cache_identity(object(), config)
    assert first_identity["property_identity"] != changed_identity["property_identity"]
    assert first_identity["artifact_hash"] != changed_identity["artifact_hash"]
    assert first_identity["harness_hash"] != changed_identity["harness_hash"]

    graph = first_region.temporal_graph
    assert graph is not None
    different_implementation = replace(
        graph,
        implementation_identity="temporal:" + "0" * 64,
    )
    relation = build_capacity_one_transaction_relation(
        replace(first_region, temporal_graph=different_implementation)
    )
    assert relation.expression_identity == first.relation.expression_identity
    assert relation.property_identity != first.relation.property_identity


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("yosys", "sby", "z3")),
    reason="Yosys, SymbiYosys, and Z3 are required",
)
def test_capacity_one_transaction_stream_miter_bounded_passes(tmp_path: Path) -> None:
    """BMC proves transfer ordering, occupancy, and atomic retire/reload."""

    module = compile_source(FORMAL_SOURCE).ir
    region = module.elastic_pipeline_regions[0]
    artifact = emit_formal_artifact(
        module, build_recursive_formal_design(module),
    )
    miter = emit_capacity_one_transaction_miter(
        region,
        artifact=artifact,
        render_expression=render_expression,
    )
    assert "zlang_temporal_output_transfer" in miter.source
    assert "if (zlang_temporal_input_transfer)" in miter.source
    selected = CompilationSession(
        FORMAL_SOURCE,
        formal_policy=FormalPolicy.REQUIRED_BMC,
        formal_depth=10,
        formal_timeout=60,
        formal_work_directory=tmp_path.resolve(),
    ).selected_ir
    record = selected.elastic_pipeline_regions[0].formal_records[0]
    assert record.formal_route == "transaction_stream_equivalence_bmc"
    assert record.status is FormalStatus.BOUNDED_PASS
    assert record.eligible


def test_temporal_admission_cycle_through_hierarchy_is_rejected() -> None:
    with pytest.raises(
        SemanticError,
        match="combinational ready/valid dependency cycle through temporal admission",
    ):
        compile_source(TEMPORAL_READY_LOOP, top="TemporalReadyLoop")


def test_direct_sv_is_deterministic_and_contains_no_ready_valid_loop(tmp_path: Path) -> None:
    module = _module()
    graph = module.elastic_pipeline_regions[0].temporal_graph
    assert graph is not None
    first = emit_experimental(module)
    assert first == emit_experimental(module)
    assert "shared_noninterleaved" not in first  # schedule is typed, not raw provider SV.
    assert "zlang_temporal_" in first
    assert "assign zlang_packed_input_ready = !rst" in first
    assert "assign zlang_packed_output_valid = !rst" in first
    for storage in graph.storage_plan.registers:
        assert f"_{storage.register_id}" in first
    ready_line = next(line for line in first.splitlines() if "input_ready =" in line)
    assert "input_valid" not in ready_line
    assert "output_ready" in ready_line
    path = tmp_path / "shared.sv"
    path.write_text(first)
    if shutil.which("yosys") is not None:
        result = subprocess.run(
            ("yosys", "-q", "-p", "read_verilog -sv shared.sv; hierarchy; proc; opt; check"),
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout
