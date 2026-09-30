from __future__ import annotations

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

    assert "zlang_scatter_contributions_" in generated
    assert "zlang_scatter_reduce_" in generated
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


@pytest.mark.skipif(shutil.which("yosys") is None, reason="Yosys unavailable")
def test_large_boolean_destination_decode_uses_bounded_procedural_scatter(
    tmp_path: Path,
) -> None:
    generated = emit_experimental(
        compile_source(SCATTER_HIT_SOURCE, top="ScatterHit192").ir
    )
    assert "zlang_scatter_contributions_" not in generated
    assert "region_scatter_address" in generated
    assert "always_comb" in generated
    assert len(generated.encode("utf-8")) < 65_536

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
