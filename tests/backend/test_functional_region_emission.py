from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.backend.systemverilog import emitter as sv_emitter
from zlang.backend.expression_materialization import MaterializedExpression
from zlang.compiler import compile_source
from zlang.ir import (
    Add,
    Assignment,
    Binary,
    BinaryOperator,
    BitType,
    CompileTimeBinderRef,
    CompileTimeExpr,
    Constant,
    FunctionalCaptureRef,
    FunctionalRegion,
    FunctionalRegionKind,
    FunctionalValue,
    FunctionalTable,
    FunctionalTableLookup,
    InputRef,
    Module,
    Mux,
    Port,
    PortDirection,
    Reduce,
    ReductionOperator,
    UIntType,
    VecType,
    VectorIndex,
)
from zlang.native_simulation import simulate


U8 = UIntType(8)
ROOT = Path(__file__).resolve().parents[2]
SCATTER_12X4X4_SOURCE = (
    ROOT / "examples/structural/scatter_12x4x4.zhl"
).read_text(encoding="utf-8")


SCATTER_SOURCE = """
fn c6<K>() -> u6 { K }
module Scatter32 {
    in valid : bit
    in addresses : vec<4,u6>
    in data : vec<4,u4>
    out result : vec<32,u4>

    result = generate(dst in 0..32)
        reduce(|, generate(i in 0..4)
            mux(valid & (addresses[i] == c6<K=dst>()), data[i], 0))
}
"""


SCATTER_HIT_SOURCE = """
fn c8<K>() -> u8 { K }
module ScatterHit192 {
    in valid : bit
    in addresses : vec<12,vec<4,vec<4,u8>>>
    out hit : vec<192,bit>

    hit = generate(dst in 0..192) {
        reduce(|, generate(lane in 0..12) {
            reduce(|, generate(group in 0..4) {
                reduce(|, generate(byte in 0..4) {
                    valid & (addresses[lane][group][byte] == c8<K=dst>())
                })
            })
        })
    }
}
"""


WIDE_SCATTER_SOURCE = """
fn scatter_index16<K>() -> u16 { K }
module WideScatter4096 {
    in valid : bit
    in addresses : vec<3,u16>
    in values : vec<3,u8>
    out result : vec<4096,u8>

    result = generate(dst in 0..4096)
        reduce(|, generate(i in 0..3)
            mux(
                valid & (addresses[i] == scatter_index16<K=dst>()),
                values[i],
                0
            ))
}
"""


SEQUENTIAL_SCATTER_SOURCE = """
fn scatter_index<K>() -> u8 { K }
module SequentialScatter12x4x4 {
    clock clk reset rst
    in valid : bit
    in addresses : vec<12,vec<4,vec<4,u8>>>
    in values : vec<12,vec<4,vec<4,u8>>>
    out result : vec<192,u8>
    reg state : vec<192,u8> = repeat(0)

    state <- generate(dst in 0..192) {
        reduce(|, generate(lane in 0..12) {
            reduce(|, generate(group in 0..4) {
                reduce(|, generate(byte in 0..4) {
                    mux(
                        valid &
                        (addresses[lane][group][byte] == scatter_index<K=dst>()),
                        values[lane][group][byte],
                        0
                    )
                })
            })
        })
    }
    result = state
}
"""


def _module(expression: FunctionalRegion) -> Module:
    output = Port(PortDirection.OUTPUT, "result", expression.type)
    inputs = ()
    if expression.captures:
        inputs = (Port(PortDirection.INPUT, "values", VecType(8, U8)),)
    return Module("FunctionalRegionEmission", (*inputs, output), (Assignment(output, expression),))


def test_module_region_uses_nonzero_lsb_first_structural_generate() -> None:
    binder = CompileTimeBinderRef("fixture:index", "index", 3, 7)
    capture = FunctionalCaptureRef("fixture:values", "values", VecType(8, U8))
    region = FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        binder,
        VectorIndex(capture, CompileTimeExpr.ref(binder), U8),
        (),
        ((capture, InputRef("values", capture.type)),),
        VecType(4, U8),
    )

    generated = emit_experimental(_module(region))

    assert "generate" in generated
    assert "for (genvar" in generated
    assert " = 3;" in generated and " < 7;" in generated
    assert " - 3) * 8) +: 8]" in generated
    assert "values[(32'(" in generated
    assert "{8'(values" not in generated
    assert "always_comb" not in generated


def test_nested_region_composition_is_dependency_first() -> None:
    inner_binder = CompileTimeBinderRef("fixture:inner", "inner", 0, 2)
    inner = FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        inner_binder,
        Constant(7, U8),
        (),
        (),
        VecType(2, U8),
    )
    outer_binder = CompileTimeBinderRef("fixture:outer", "outer", 3, 5)
    outer = FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        outer_binder,
        inner,
        (),
        (),
        VecType(2, inner.type),
    )

    plan = sv_emitter._functional_region_composition_plan(outer)

    assert tuple(node.region for node in plan.nodes) == (inner, outer)
    assert plan.nodes[-1].dependencies == (plan.nodes[0].identity,)
    assert plan.nodes[0].free_binders == ()


def test_exact_binder_free_region_invariant_has_one_shared_owner() -> None:
    def region(identity: str) -> FunctionalRegion:
        binder = CompileTimeBinderRef(identity, "index", 0, 1)
        return FunctionalRegion(
            FunctionalRegionKind.GENERATE,
            binder,
            Constant(0, U8),
            (),
            (),
            VecType(1, U8),
        )

    left = Add(InputRef("a", U8), InputRef("b", U8), U8)
    right = Add(InputRef("a", U8), InputRef("b", U8), U8)

    def record(
        owner: FunctionalRegion,
        value: Add,
        suffix: str,
    ) -> tuple[FunctionalRegion, sv_emitter.FunctionalRegionEmissionPlan]:
        scatter = sv_emitter._FunctionalScatterEmissionPlan(
            (),
            (),
            (MaterializedExpression(value, f"local_{suffix}"),),
            f"enable_{suffix}",
            f"address_{suffix}",
            f"value_{suffix}",
        )
        return owner, sv_emitter.FunctionalRegionEmissionPlan(
            f"plan:{suffix}",
            f"result_{suffix}",
            "",
            8,
            (),
            (),
            scatter=scatter,
        )

    shared = sv_emitter._shared_functional_region_materialization(
        {
            "left": record(region("fixture:left"), left, "left"),
            "right": record(region("fixture:right"), right, "right"),
        },
        used_names={"result_left", "result_right"},
    )

    assert len(shared) == 1
    assert shared[0].expression == left
    assert shared[0].name.startswith("zlang_region_shared_0")


def test_region_table_uses_one_deterministic_case_lookup() -> None:
    binder = CompileTimeBinderRef("fixture:table-index", "index", 2, 6)
    table = FunctionalTable(
        "fixture:table",
        tuple(Constant(value, U8) for value in (11, 22, 33, 44)),
        U8,
        start=2,
    )
    lookup = FunctionalTableLookup(
        table.name,
        CompileTimeExpr.ref(binder),
        U8,
    )
    region = FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        binder,
        lookup,
        (table,),
        (),
        VecType(4, U8),
    )

    first = emit_experimental(_module(region))
    second = emit_experimental(_module(region))

    assert first == second
    assert first.count("case (") == 1
    for index, value in enumerate((11, 22, 33, 44), start=2):
        assert f"{index}:" in first
        assert f"8'd{value}" in first


def test_destination_decode_is_lowered_as_bounded_candidate_scatter() -> None:
    module = compile_source(SCATTER_SOURCE, top="Scatter32").ir
    generated = emit_experimental(module)

    assert "module zlang_scatter_structural_helper_" in generated
    assert "assign reduce_" in generated
    assert "< 6'd32" in generated
    assert "region_reduce_" not in generated
    assert "always_comb" not in generated

    expected = [0] * 32
    expected[1] = 0x2 | 0x4
    expected[31] = 0x3
    assert simulate(
        module,
        valid=1,
        addresses=[1, 1, 40, 31],
        data=[2, 4, 8, 3],
    ) == {"result": expected}


def test_scatter_lowering_rejects_nonzero_miss_value() -> None:
    source = SCATTER_SOURCE.replace(
        "data[i], 0)",
        "data[i], 1)",
    )
    generated = emit_experimental(compile_source(source, top="Scatter32").ir)

    assert "region_scatter_address" not in generated
    assert generated.count("for (") == 2


def test_scatter_plan_elides_only_candidate_binder_dimensions_absent_from_entry(
) -> None:
    destination = CompileTimeBinderRef("fixture:destination", "dst", 0, 8)
    dead = CompileTimeBinderRef("fixture:dead", "dead", 0, 4)
    live = CompileTimeBinderRef("fixture:live", "live", 0, 2)
    type4 = UIntType(4)
    address = VectorIndex(
        InputRef("addresses", VecType(2, type4)),
        CompileTimeExpr.ref(live),
        type4,
    )
    value = VectorIndex(
        InputRef("data", VecType(2, type4)),
        CompileTimeExpr.ref(live),
        type4,
    )
    destination_value = FunctionalValue(CompileTimeExpr.ref(destination), type4)
    destination_capture = FunctionalCaptureRef(
        "fixture:destination-capture",
        "destination",
        type4,
    )
    enabled = Binary(
        BinaryOperator.BIT_AND,
        InputRef("valid", BitType()),
        Binary(
            BinaryOperator.EQUAL,
            address,
            destination_capture,
            type4,
            BitType(),
        ),
        BitType(),
        BitType(),
    )
    leaf = Mux(enabled, value, Constant(0, type4), type4)
    live_region = FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        live,
        leaf,
        (),
        ((destination_capture, destination_value),),
        VecType(2, type4),
    )
    live_reduction = Reduce(ReductionOperator.BIT_OR, live_region, type4)
    dead_region = FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        dead,
        live_reduction,
        (),
        (),
        VecType(4, type4),
    )
    dead_reduction = Reduce(ReductionOperator.BIT_OR, dead_region, type4)
    outer = FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        destination,
        dead_reduction,
        (),
        (),
        VecType(8, type4),
    )

    plan = sv_emitter._functional_region_plan(outer, "result")

    assert plan.scatter is not None
    assert tuple(
        item.region.binder.identity for item in plan.scatter.dimensions
    ) == (live.identity,)


def test_table_backed_structural_scatter_uses_its_bounded_plan() -> None:
    destination = CompileTimeBinderRef("fixture:table-destination", "dst", 0, 4)
    candidate = CompileTimeBinderRef("fixture:table-candidate", "item", 0, 4)
    table = FunctionalTable(
        "fixture:scatter-address-table",
        tuple(Constant(value, U8) for value in (3, 1, 0, 2)),
        U8,
    )
    address = FunctionalTableLookup(
        table.name,
        CompileTimeExpr.ref(candidate),
        U8,
    )
    destination_value = FunctionalValue(CompileTimeExpr.ref(destination), U8)
    destination_capture = FunctionalCaptureRef(
        "fixture:table-destination-capture",
        "destination",
        U8,
    )
    leaf = Mux(
        Binary(
            BinaryOperator.EQUAL,
            address,
            destination_capture,
            U8,
            BitType(),
        ),
        Constant(1, U8),
        Constant(0, U8),
        U8,
    )
    candidate_region = FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        candidate,
        leaf,
        (table,),
        ((destination_capture, destination_value),),
        VecType(4, U8),
    )
    outer = FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        destination,
        Reduce(ReductionOperator.BIT_OR, candidate_region, U8),
        (),
        (),
        VecType(4, U8),
    )
    plan = sv_emitter._functional_region_plan(outer, "result")
    lowering = sv_emitter._functional_scatter_lowering_plan(outer, plan)

    assert plan.scatter is not None
    assert lowering.strategy is sv_emitter.FunctionalScatterLoweringStrategy.STRUCTURAL
    assert any(item.table_temporaries for item in plan.scatter.dimensions)

    generated = emit_experimental(_module(outer))

    assert "zlang_scatter_chunks_" not in generated
    assert "zlang_scatter_chunk_" in generated
    assert generated.count("always_comb begin") == 1
    assert generated.count("case (") == 1


def test_scatter_lowering_plan_uses_documented_cost_boundaries() -> None:
    strategy = sv_emitter.FunctionalScatterLoweringPlan.select_strategy

    assert strategy(256, 256) is sv_emitter.FunctionalScatterLoweringStrategy.STRUCTURAL
    assert strategy(257, 1) is sv_emitter.FunctionalScatterLoweringStrategy.CHUNKED_PROCEDURAL
    assert strategy(256, 257) is sv_emitter.FunctionalScatterLoweringStrategy.CHUNKED_PROCEDURAL


def test_192_bit_hit_scatter_remains_structural() -> None:
    module = compile_source(SCATTER_12X4X4_SOURCE, top="Scatter12x4x4Witness").ir
    region = next(
        assignment.expression
        for assignment in module.assignments
        if assignment.target.name == "hit"
    )
    assert isinstance(region, FunctionalRegion)
    emission = sv_emitter._functional_region_plan(region, "hit_result")
    lowering = sv_emitter._functional_scatter_lowering_plan(region, emission)

    assert lowering.strategy is sv_emitter.FunctionalScatterLoweringStrategy.STRUCTURAL
    assert lowering.effective_candidate_count == 192
    assert lowering.result_width == 192
    assert len(lowering.chunks) == 12
    assert tuple(chunk.write_count for chunk in lowering.chunks) == (16,) * 12


def test_192_byte_scatter_uses_twelve_natural_sixteen_write_chunks() -> None:
    module = compile_source(SCATTER_12X4X4_SOURCE, top="Scatter12x4x4Witness").ir
    region = next(
        assignment.expression
        for assignment in module.assignments
        if assignment.target.name == "result"
    )
    assert isinstance(region, FunctionalRegion)
    emission = sv_emitter._functional_region_plan(region, "value_result")
    lowering = sv_emitter._functional_scatter_lowering_plan(region, emission)

    assert lowering.strategy is sv_emitter.FunctionalScatterLoweringStrategy.CHUNKED_PROCEDURAL
    assert lowering.effective_candidate_count == 192
    assert lowering.result_width == 1536
    assert len(lowering.chunks) == 12
    assert tuple(chunk.write_count for chunk in lowering.chunks) == (16,) * 12
    assert tuple(value for chunk in lowering.chunks for _, value in chunk.fixed_binders) == tuple(range(12))
    assert tuple(len(level) for level in lowering.reduction_names) == (6, 3, 2, 1)
    assert all(
        chunk.write_count <= sv_emitter.MAX_SCATTER_CHUNK_WRITES
        and chunk.write_count * lowering.result_width
        <= sv_emitter.MAX_SCATTER_CHUNK_RESULT_BIT_UPDATES
        for chunk in lowering.chunks
    )


def test_sequential_scatter_uses_the_same_chunked_policy() -> None:
    generated = emit_experimental(
        compile_source(
            SEQUENTIAL_SCATTER_SOURCE,
            top="SequentialScatter12x4x4",
        ).ir
    )

    assert generated.count("module zlang_scatter_chunk_helper_") == 2
    assert generated.count("zlang_scatter_chunk_") >= 12
    assert generated.count("always_comb begin") == 2
    assert "always_ff @(posedge clk)" in generated
    assert "candidate < 16" in generated


def test_flat_scatter_entries_split_into_stable_sixteen_entry_chunks() -> None:
    destination = CompileTimeBinderRef("fixture:flat-destination", "dst", 0, 192)
    region = FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        destination,
        Constant(0, U8),
        (),
        (),
        VecType(192, U8),
    )
    entry = sv_emitter._FunctionalScatterEntry(
        Constant(1, BitType()),
        Constant(0, U8),
        Constant(1, U8),
    )
    scatter = sv_emitter._FunctionalScatterEmissionPlan(
        (),
        (entry,) * 48,
        (),
        "flat_enable",
        "flat_address",
        "flat_value",
    )
    emission = sv_emitter.FunctionalRegionEmissionPlan(
        "0123456789abcdef",
        "flat_result",
        "",
        8,
        (),
        (),
        scatter=scatter,
    )

    first = sv_emitter._functional_scatter_lowering_plan(region, emission)
    second = sv_emitter._functional_scatter_lowering_plan(region, emission)

    assert first == second
    assert tuple(chunk.write_count for chunk in first.chunks) == (16, 16, 16)
    assert tuple((chunk.entry_start, chunk.entry_stop) for chunk in first.chunks) == (
        (0, 16),
        (16, 32),
        (32, 48),
    )
    assert len({chunk.accumulator for chunk in first.chunks}) == 3


def test_scatter_wider_than_chunk_budget_uses_irreducible_single_write_chunks(
) -> None:
    module = compile_source(WIDE_SCATTER_SOURCE, top="WideScatter4096").ir
    region = next(
        assignment.expression
        for assignment in module.assignments
        if assignment.target.name == "result"
    )
    assert isinstance(region, FunctionalRegion)
    emission = sv_emitter._functional_region_plan(region, "wide_result")

    lowering = sv_emitter._functional_scatter_lowering_plan(region, emission)

    assert lowering.strategy is sv_emitter.FunctionalScatterLoweringStrategy.CHUNKED_PROCEDURAL
    assert lowering.result_width == 4096 * 8
    assert tuple(chunk.write_count for chunk in lowering.chunks) == (1, 1, 1)
    assert lowering.result_width > sv_emitter.MAX_SCATTER_CHUNK_RESULT_BIT_UPDATES

    first = emit_experimental(module)
    second = emit_experimental(module)

    assert first == second
    assert first.count("zlang_scatter_chunk_helper_") >= 1
    assert first.count("_instance (") >= 3


@pytest.mark.skipif(shutil.which("yosys") is None, reason="Yosys unavailable")
def test_large_boolean_destination_decode_uses_structural_scatter(
    tmp_path: Path,
) -> None:
    generated = emit_experimental(
        compile_source(SCATTER_HIT_SOURCE, top="ScatterHit192").ir
    )
    assert "module zlang_scatter_structural_helper_" in generated
    assert "zlang_scatter_chunk_helper_" not in generated
    assert "always_comb" not in generated
    assert len(generated.encode("utf-8")) < 128_000

    rtl = tmp_path / "ScatterHit192.sv"
    rtl.write_text(generated, encoding="utf-8")
    completed = subprocess.run(
        (
            "yosys",
            "-q",
            "-p",
            f"read_verilog -sv {rtl}; hierarchy -check -top ScatterHit192; "
            "proc; opt; stat",
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.performance
@pytest.mark.skipif(shutil.which("yosys") is None, reason="Yosys unavailable")
def test_public_wide_scatter_yosys_process_is_time_and_memory_bounded(
    tmp_path: Path,
) -> None:
    module = compile_source(
        SCATTER_12X4X4_SOURCE,
        top="Scatter12x4x4Witness",
    ).ir
    generated = emit_experimental(module)
    assert generated == emit_experimental(module)
    assert generated.count("always_comb begin") == 2
    assert generated.count("if (") == 1
    assert generated.count("contribution[") == 1
    assert " = result[" not in generated
    assert "result[" not in generated
    assert generated.count("module zlang_scatter_chunk_helper_") == 2
    assert "module zlang_scatter_structural_helper_" in generated
    assert len(generated.encode("utf-8")) < 256_000
    assert generated.count("write_instance (") == 2
    assert "candidate < 16" in generated
    assert "result = reduce_" in generated

    rtl = tmp_path / "Scatter12x4x4Witness.sv"
    rtl.write_text(generated, encoding="utf-8")
    metrics = tmp_path / "yosys.metrics"
    completed = subprocess.run(
        (
            "/usr/bin/time",
            "-f",
            "%e %M",
            "-o",
            str(metrics),
            "yosys",
            "-q",
            "-p",
            f"read_verilog -sv {rtl}; "
            "hierarchy -check -top Scatter12x4x4Witness; proc; opt; stat",
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wall_seconds, peak_rss_kib = metrics.read_text(encoding="utf-8").split()
    assert float(wall_seconds) <= 30
    assert int(peak_rss_kib) <= 512 * 1024


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("verilator", "yosys", "iverilog")),
    reason="SystemVerilog acceptance tools unavailable",
)
def test_region_loop_is_accepted_by_supported_sv_frontends(tmp_path: Path) -> None:
    binder = CompileTimeBinderRef("fixture:tools", "index", 0, 4)
    table = FunctionalTable(
        "fixture:tools-table",
        tuple(Constant(value, U8) for value in (1, 2, 3, 4)),
        U8,
    )
    region = FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        binder,
        FunctionalTableLookup(table.name, CompileTimeExpr.ref(binder), U8),
        (table,),
        (),
        VecType(4, U8),
    )
    rtl = tmp_path / "FunctionalRegionEmission.sv"
    rtl.write_text(emit_experimental(_module(region)), encoding="utf-8")
    commands = (
        ("verilator", "--lint-only", "--timing", "-Wall", "-Wno-fatal", str(rtl)),
        ("yosys", "-q", "-p", f"read_verilog -sv {rtl}; hierarchy -check -top FunctionalRegionEmission"),
        ("iverilog", "-g2012", "-s", "FunctionalRegionEmission", "-o", str(tmp_path / "probe.vvp"), str(rtl)),
    )
    for command in commands:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="Icarus Verilog unavailable",
)
def test_scatter_rtl_matches_lsb_first_collision_and_range_semantics(
    tmp_path: Path,
) -> None:
    module = compile_source(SCATTER_SOURCE, top="Scatter32").ir
    vectors = (
        ([1, 1, 40, 31], [2, 4, 8, 3]),
        ([0, 7, 7, 63], [1, 2, 8, 15]),
        ([4, 3, 2, 1], [9, 8, 7, 6]),
    )

    checks: list[str] = []
    for ordinal, (addresses, values) in enumerate(vectors):
        expected = simulate(
            module,
            valid=1,
            addresses=addresses,
            data=values,
        )["result"]
        packed_addresses = sum(value << (index * 6) for index, value in enumerate(addresses))
        packed_values = sum(value << (index * 4) for index, value in enumerate(values))
        packed_expected = sum(value << (index * 4) for index, value in enumerate(expected))
        checks.extend(
            (
                f"    addresses = 24'h{packed_addresses:06x};",
                f"    data = 16'h{packed_values:04x};",
                "    #1;",
                f"    if (result !== 128'h{packed_expected:032x}) "
                f"$fatal(1, \"scatter vector {ordinal} mismatch\");",
            )
        )

    rtl = tmp_path / "Scatter32.sv"
    rtl.write_text(
        emit_experimental(module)
        + "\nmodule Scatter32Tb;\n"
        + "  logic valid;\n"
        + "  logic [3:0][5:0] addresses;\n"
        + "  logic [3:0][3:0] data;\n"
        + "  logic [31:0][3:0] result;\n"
        + "  Scatter32 dut(.*);\n"
        + "  initial begin\n"
        + "    valid = 1'b1;\n"
        + "\n".join(checks)
        + "\n    $finish;\n"
        + "  end\n"
        + "endmodule\n",
        encoding="utf-8",
    )
    executable = tmp_path / "scatter.vvp"
    compiled = subprocess.run(
        ("iverilog", "-g2012", "-s", "Scatter32Tb", "-o", str(executable), str(rtl)),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    simulated = subprocess.run(
        ("vvp", str(executable)),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert simulated.returncode == 0, simulated.stdout + simulated.stderr


@pytest.mark.skipif(
    shutil.which("verilator") is None,
    reason="Verilator unavailable",
)
def test_chunked_wide_scatter_matches_native_for_every_output_element(
    tmp_path: Path,
) -> None:
    module = compile_source(
        SCATTER_12X4X4_SOURCE,
        top="Scatter12x4x4Witness",
    ).ir

    def nested(values: list[int]) -> list[list[list[int]]]:
        return [
            [
                values[lane * 16 + group * 4:lane * 16 + group * 4 + 4]
                for group in range(4)
            ]
            for lane in range(12)
        ]

    cases: list[tuple[int, list[int], list[int]]] = []
    cases.append((0, [0] * 192, [0xff] * 192))
    one_addresses = [255] * 192
    one_values = [0] * 192
    one_addresses[0] = 7
    one_values[0] = 0x12
    cases.append((1, one_addresses, one_values))
    collision_addresses = [255] * 192
    collision_values = [0] * 192
    for index, value in ((0, 1), (16, 2), (191, 4)):
        collision_addresses[index] = 5
        collision_values[index] = value
    cases.append((1, collision_addresses, collision_values))
    multi_addresses = [255] * 192
    multi_values = [0] * 192
    for index, address, value in (
        (0, 10, 0x11),
        (15, 20, 0x22),
        (16, 30, 0x44),
        (80, 40, 0x88),
        (191, 190, 0xaa),
    ):
        multi_addresses[index] = address
        multi_values[index] = value
    cases.append((1, multi_addresses, multi_values))

    checks: list[str] = []
    for ordinal, (valid, addresses, values) in enumerate(cases):
        expected = simulate(
            module,
            valid=valid,
            addresses=nested(addresses),
            values=nested(values),
        )
        packed_addresses = sum(
            value << (index * 8) for index, value in enumerate(addresses)
        )
        packed_values = sum(
            value << (index * 8) for index, value in enumerate(values)
        )
        packed_hit = sum(
            value << index for index, value in enumerate(expected["hit"])
        )
        packed_result = sum(
            value << (index * 8)
            for index, value in enumerate(expected["result"])
        )
        checks.extend(
            (
                f"    valid = 1'b{valid};",
                f"    addresses = 1536'h{packed_addresses:0384x};",
                f"    values = 1536'h{packed_values:0384x};",
                "    #1;",
                f"    if (hit !== 192'h{packed_hit:048x}) "
                f"$fatal(1, \"hit vector {ordinal} mismatch\");",
                f"    if (result !== 1536'h{packed_result:0384x}) "
                f"$fatal(1, \"result vector {ordinal} mismatch\");",
            )
        )

    rtl = tmp_path / "Scatter12x4x4Witness.sv"
    rtl.write_text(emit_experimental(module), encoding="utf-8")
    bench = tmp_path / "tb.sv"
    bench.write_text(
        "module tb;\n"
        "  logic valid;\n"
        "  logic [11:0][3:0][3:0][7:0] addresses;\n"
        "  logic [11:0][3:0][3:0][7:0] values;\n"
        "  logic [191:0] hit;\n"
        "  logic [191:0][7:0] result;\n"
        "  Scatter12x4x4Witness dut(.*);\n"
        "  initial begin\n"
        + "\n".join(checks)
        + "\n    $finish;\n"
        "  end\n"
        "endmodule\n",
        encoding="utf-8",
    )
    obj = tmp_path / "obj"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    compiled = subprocess.run(
        (
            "verilator",
            "--binary",
            "--timing",
            "-Wno-fatal",
            "--top-module",
            "tb",
            str(rtl),
            str(bench),
            "-Mdir",
            str(obj),
        ),
        capture_output=True,
        text=True,
        timeout=180,
        env=environment,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    simulated = subprocess.run(
        (str(obj / "Vtb"),),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert simulated.returncode == 0, simulated.stdout + simulated.stderr
