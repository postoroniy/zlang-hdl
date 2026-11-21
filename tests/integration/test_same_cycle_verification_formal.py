from __future__ import annotations

import shutil

import pytest

from zlang.backend.systemverilog import emit_formal_artifact
from zlang.compiler import compile_source
from zlang.formal import (
    build_recursive_formal_design,
    connect_formal_design,
    emit_harness,
    run_verilog_formal,
)
from zlang.ir.formal import FormalStatus, ProofMode
from zlang.verification_bundle import (
    VerificationRunConfig,
    load_verification_bundle,
    run_verification_bundle_staged,
)
from zlang.verification_publication import (
    publish_compilation_verification_bundle,
)

from tests.semantic.test_same_cycle_verification_predicates import VERIFIED


FORMAL_TOOLS = ("yosys", "sby", "yosys-smtbmc", "z3")


RUNTIME_INDEXED = """
module RuntimePredicate {
    clock clk reset rst
    in raw : bits<4>
    in index : u2
    out y : bit
    values : vec<4,bit> = bitcast<vec<4,bit>>(raw)
    y = values[index]
    assert selected_bit { y == values[index] }
}
"""


def _connected(source: str):
    compilation = compile_source(
        source,
        source_unit="tests/fixtures/same_cycle_predicates.zhl",
    )
    artifact = emit_formal_artifact(
        compilation.ir,
        build_recursive_formal_design(compilation.ir),
    )
    return compilation, connect_formal_design(compilation.formal_design, artifact)


def test_same_cycle_predicates_publish_and_round_trip_in_immutable_bundle(
    tmp_path,
) -> None:
    compilation, connected = _connected(VERIFIED)
    source_properties = tuple(
        item for item in connected.properties
        if (item.generated_from or "").startswith("verification-")
    )
    assert source_properties
    assert all(item.non_executable_reason is None for item in source_properties)
    assert all(item.predicate is not None for item in source_properties)

    manifest = publish_compilation_verification_bundle(
        compilation, tmp_path / "bundle"
    )
    loaded = load_verification_bundle(tmp_path / "bundle")
    assert loaded.manifest == manifest
    payload = loaded.verification_ir["payload"]
    predicates = {
        item["generated_from"]: item["predicate"]
        for item in payload["properties"]
        if str(item.get("generated_from", "")).startswith("verification-")
    }
    assert set(predicates) >= {
        "verification-assert:$module:packed_matches",
        "verification-assert:$module:mux_matches",
        "verification-assert:$module:switch_matches",
        "verification-assert:$module:signed_representation",
        "verification-assert:$module:exact_bitcast",
        "verification-assert:$module:arithmetic_commutes",
        "verification-assert:$module:bit_logic_identity",
    }
    assert all(value["schema"] == "zlang-formal-predicate-v1" for value in predicates.values())


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in FORMAL_TOOLS),
    reason="Yosys, SymbiYosys, yosys-smtbmc, and Z3 are required",
)
def test_real_solver_passes_concat_and_finds_swapped_field_mutation(tmp_path) -> None:
    correct, connected = _connected(VERIFIED)
    correct_origin = next(
        item.source_origin
        for item in connected.properties
        if item.generated_from == "verification-assert:$module:packed_matches"
    )
    passed = run_verilog_formal(
        emit_harness(connected, depth=4),
        top="SameCyclePredicateSurface__safety_verification_formal",
        property_id="same-cycle.structured.pass",
        depth=4,
        systemverilog=True,
        source_origin=correct_origin,
        work_directory=tmp_path / "pass",
    )
    assert passed.status is FormalStatus.BOUNDED_PASS
    assert passed.counterexample is None

    mutated_source = VERIFIED.replace(
        "joined = packed_value(high, low)",
        "joined = packed_value(low, high)",
    )
    assert mutated_source != VERIFIED
    mutated, mutated_connected = _connected(mutated_source)
    mutated_property = next(
        item for item in mutated_connected.properties
        if item.generated_from == "verification-assert:$module:packed_matches"
    )
    failed = run_verilog_formal(
        emit_harness(mutated_connected, depth=4),
        top="SameCyclePredicateSurface__safety_verification_formal",
        property_id=mutated_property.id,
        depth=4,
        systemverilog=True,
        source_origin=mutated_property.source_origin,
        work_directory=tmp_path / "mutation",
    )
    assert failed.status is FormalStatus.FAILED
    assert failed.counterexample is not None
    assert failed.counterexample.raw_trace
    assert failed.source_origin == mutated_property.source_origin
    assert failed.source_origin is not None
    assert failed.source_origin.construct == "assert packed_matches"


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in FORMAL_TOOLS),
    reason="Yosys, SymbiYosys, yosys-smtbmc, and Z3 are required",
)
@pytest.mark.parametrize(("width", "selector"), [(2, "index"), (1, "index"), (2, "(index | 2)")])
def test_real_solver_checks_range_proven_runtime_index(tmp_path, width, selector) -> None:
    source = RUNTIME_INDEXED.replace("index : u2", f"index : u{width}")
    source = source.replace("values[index]", f"values[{selector}]")
    compilation, connected = _connected(source)
    property_ = next(
        item for item in connected.properties
        if item.generated_from == "verification-assert:$module:selected_bit"
    )
    passed = run_verilog_formal(
        emit_harness(connected, depth=4),
        top="RuntimePredicate__safety_verification_formal",
        property_id=property_.id,
        depth=4,
        systemverilog=True,
        source_origin=property_.source_origin,
        work_directory=tmp_path / "runtime-index-pass",
    )
    assert passed.status is FormalStatus.BOUNDED_PASS

    mutated_source = source.replace(
        f"y = values[{selector}]",
        "y = raw[0]",
    )
    assert mutated_source != source
    _, mutated = _connected(mutated_source)
    mutated_property = next(
        item for item in mutated.properties
        if item.generated_from == "verification-assert:$module:selected_bit"
    )
    failed = run_verilog_formal(
        emit_harness(mutated, depth=4),
        top="RuntimePredicate__safety_verification_formal",
        property_id=mutated_property.id,
        depth=4,
        systemverilog=True,
        source_origin=mutated_property.source_origin,
        work_directory=tmp_path / "runtime-index-mutation",
    )
    assert failed.status is FormalStatus.FAILED
    assert failed.counterexample is not None
    assert failed.counterexample.raw_trace
    assert failed.source_origin == mutated_property.source_origin


@pytest.mark.parametrize("operator,data_type", [("<<", "u2"), (">>", "u2"), (">>", "s2")])
def test_shift_property_preserves_the_full_unsigned_distance(tmp_path, operator, data_type) -> None:
    for name, count, expected in (
        ("full", "n", FormalStatus.PROVEN),
        ("narrowed", "truncate<2>(n)", FormalStatus.FAILED),
    ):
        _, connected = _connected(f"""
            module Shift {{
                clock clk reset rst in x:{data_type} in n:u65 out y:{data_type}
                y = x {operator} {count}
                assert agrees {{ y == (x {operator} n) }}
            }}
        """)
        goal = next(
            p for p in connected.properties if p.generated_from == "verification-assert:$module:agrees"
        )
        result = run_verilog_formal(
            emit_harness(connected, depth=8, mode=ProofMode.PROVE),
            top="Shift__safety_verification_formal", property_id=goal.id,
            mode=ProofMode.PROVE, depth=8, timeout_seconds=30, systemverilog=True,
            source_origin=goal.source_origin, work_directory=tmp_path / name,
        )
        assert result.status is expected
        if expected is FormalStatus.FAILED:
            assert result.counterexample is not None and result.counterexample.raw_trace
            assert result.source_origin == goal.source_origin


def test_shift_in_requirement_keeps_the_failing_input_reachable(tmp_path) -> None:
    # At x=1, n=4 the requirement holds and the goal fails. An implicit
    # two-bit count would exclude that input from the solver's domain.
    compilation = compile_source("""
        module Window {
            clock clk reset rst in x:u2 in n:u3 out y:bit
            y = (x == 0) | (n != 4)
            contract window @ clk {
                require masked { (x << n) == 0 }
                ensure valid { y }
            }
        }
    """, source_unit="window.zhl")
    bundle = tmp_path / "bundle"
    publish_compilation_verification_bundle(compilation, bundle)
    report = run_verification_bundle_staged(
        bundle, config=VerificationRunConfig(mode=ProofMode.PROVE, depth=8, timeout_seconds=30),
        work_directory=tmp_path / "solver",
    )
    [goal] = [result for result in report.results if result.kind == "safety"]
    [feasibility] = [result for result in report.results if result.kind == "cover"]
    assert feasibility.status == "witnessed"
    assert goal.status == "failed" and goal.counterexample is not None
    assert goal.assumption_ids and goal.source_origin.construct == "ensure valid"
