from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.clash import emit as emit_clash
from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.ir import (
    Add,
    Assignment,
    Call,
    CompileTimeBinderRef,
    CompileTimeExpr,
    Constant,
    ExactReductionCombine,
    Function,
    FunctionParameter,
    FunctionalCaptureRef,
    FunctionalRegion,
    FunctionalRegionKind,
    FunctionalTable,
    FunctionalTableLookup,
    InputRef,
    Module,
    ParameterRef,
    Port,
    PortDirection,
    Reduce,
    ReductionOperator,
    VectorIndex,
    build_exact_reduction_plan,
)
from zlang.ir.functional import (
    materialize_exact_reduction,
    materialize_functional_region,
)
from zlang.ir.types import UIntType, VecType
from zlang.simulate import simulate
from zlang.toolchain import generate_verilog, lint_with_verilator


U8 = UIntType(8)


RV_PAYLOAD_REGION_SOURCE = """
module ReadyValidPayloadRegion64 {
    in input : rv<vec<64,u8>>
    out values : vec<64,u9>

    input.ready = 1
    values = generate(i in 0..64) { input.payload[i] + 1 }
}
"""


def _region32() -> FunctionalRegion:
    binder = CompileTimeBinderRef("backend:k", "k", 0, 32)
    table = FunctionalTable(
        "backend:values",
        tuple(Constant(index, U8) for index in range(32)),
        U8,
    )
    return FunctionalRegion(
        FunctionalRegionKind.GENERATE,
        binder,
        FunctionalTableLookup(
            table.name,
            CompileTimeExpr.ref(binder),
            U8,
        ),
        (table,),
        (),
        VecType(32, U8),
    )


def _nominal_reduction_module() -> tuple[Module, Reduce, tuple[Function, ...]]:
    functions: dict[int, Function] = {}
    u8_impl = Function(
        "add_u8_impl",
        (
            FunctionParameter("left", U8),
            FunctionParameter("right", U8),
        ),
        UIntType(9),
        Add(
            ParameterRef("left", U8),
            ParameterRef("right", U8),
            UIntType(9),
        ),
    )
    for width in range(8, 13):
        operand_type = UIntType(width)
        result_type = UIntType(width + 1)
        functions[width] = Function(
            f"add_u{width}",
            (
                FunctionParameter("left", operand_type),
                FunctionParameter("right", operand_type),
            ),
            result_type,
            (
                Call(
                    u8_impl.name,
                    (
                        ParameterRef("left", operand_type),
                        ParameterRef("right", operand_type),
                    ),
                    result_type,
                    u8_impl.callee_identity,
                )
                if width == 8
                else Add(
                    ParameterRef("left", operand_type),
                    ParameterRef("right", operand_type),
                    result_type,
                )
            ),
        )

    def combine(left, right) -> ExactReductionCombine:
        assert left == right
        function = functions[left.width]
        return ExactReductionCombine(
            function.return_type,
            function.name,
            function.callee_identity,
        )

    plan = build_exact_reduction_plan(U8, 32, combine)
    reduction = Reduce(
        ReductionOperator.ADD,
        _region32(),
        plan.root_type,
        plan=plan,
    )
    output = Port(PortDirection.OUTPUT, "y", plan.root_type)
    ordered = (u8_impl, *(functions[width] for width in sorted(functions)))
    return (
        Module(
            "NominalRegion32",
            (output,),
            (Assignment(output, reduction),),
            callable_definitions=ordered,
        ),
        reduction,
        ordered,
    )


def test_final_region_materialization_substitutes_binder_and_capture_exactly() -> None:
    vector_type = VecType(32, U8)
    binder = CompileTimeBinderRef("backend:capture:k", "k", 0, 32)
    capture = FunctionalCaptureRef("backend:capture", "values", vector_type)
    region = FunctionalRegion(
        FunctionalRegionKind.MAP,
        binder,
        VectorIndex(capture, CompileTimeExpr.ref(binder), U8),
        (),
        ((capture, InputRef("values", vector_type)),),
        vector_type,
    )

    elements = materialize_functional_region(region)

    assert len(elements) == 32
    assert all(isinstance(value, VectorIndex) for value in elements)
    assert tuple(value.index for value in elements) == tuple(range(32))
    assert all(value.expression == InputRef("values", vector_type) for value in elements)


def test_ready_valid_payload_region_is_bit_exact_and_backend_deterministic() -> None:
    module = compile_source(RV_PAYLOAD_REGION_SOURCE, include_clash=False).ir
    region = module.assignments[1].expression
    assert isinstance(region, FunctionalRegion)
    assert simulate(
        module,
        input={"payload": list(range(64)), "valid": 1},
    )["values"] == list(range(1, 65))
    assert emit_experimental(module) == emit_experimental(module)
    assert emit_clash(module) == emit_clash(module)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_ready_valid_payload_region_direct_sv_passes_strict_verilator(
    tmp_path: Path,
) -> None:
    module = compile_source(RV_PAYLOAD_REGION_SOURCE, include_clash=False).ir
    rtl = tmp_path / "ReadyValidPayloadRegion64.sv"
    rtl.write_text(emit_experimental(module))
    lint_with_verilator((rtl,), module.name)


def test_length32_region_and_exact_plan_emit_once_in_frozen_topology() -> None:
    module, reduction, functions = _nominal_reduction_module()
    lowered = materialize_exact_reduction(reduction)
    calls = []

    def walk(value) -> None:
        if isinstance(value, Call):
            calls.append(value)
            for argument in value.arguments:
                walk(argument)

    walk(lowered)
    assert len(calls) == 31
    assert {call.callee_identity for call in calls} == {
        function.callee_identity
        for function in functions
        if function.name != "add_u8_impl"
    }

    first_sv = emit_experimental(module)
    second_sv = emit_experimental(module)
    first_clash = emit_clash(module)
    second_clash = emit_clash(module)
    assert first_sv == second_sv
    assert first_clash == second_clash
    for level, expected_calls in zip(range(8, 13), (16, 8, 4, 2, 1), strict=True):
        # One additional occurrence is the helper declaration itself.
        assert first_sv.count(f"add_u{level}(") == expected_calls + 1
        assert first_clash.count(f"add_u{level} ") == expected_calls + 2
    assert first_sv.count("add_u8_impl(") == 2
    assert first_clash.count("add_u8_impl ") == 3
    assert "FunctionalRegion" not in first_sv
    assert "FunctionalRegion" not in first_clash


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_length32_exact_plan_direct_sv_passes_strict_verilator(tmp_path: Path) -> None:
    module, _, _ = _nominal_reduction_module()
    rtl = tmp_path / "NominalRegion32.sv"
    rtl.write_text(emit_experimental(module))
    lint_with_verilator((rtl,), module.name)


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_length32_exact_plan_callable_definitions_real_clash_generates(
    tmp_path: Path,
) -> None:
    module, _, functions = _nominal_reduction_module()
    source = emit_clash(module)
    for function in functions:
        assert source.count(f"{function.name} ::") == 1
    rtl = generate_verilog(
        source,
        module.name,
        tmp_path / "clash_nominal_region32",
        CLASH_EXECUTABLE,
    )
    lint_with_verilator(rtl, module.name)


@pytest.mark.parametrize(("length", "result_width"), ((32, 13), (33, 14)))
def test_compact_builtin_scalar_sum_materializes_only_at_execution_boundary(
    length: int,
    result_width: int,
) -> None:
    source = (
        f"module ScalarRegionSum{length}{{"
        f"in x:vec<{length},u8> out y:u{result_width} "
        f"y=sum(generate(i in 0..{length}) x[i])}}"
    )
    module = compile_source(source, include_clash=False).ir
    reduction = module.assignments[0].expression
    assert isinstance(reduction, Reduce)
    assert isinstance(reduction.collection, FunctionalRegion)
    assert reduction.plan is None and reduction.expanded is None

    values = list(range(length))
    assert simulate(module, x=values) == {"y": sum(values)}
    first_sv = emit_experimental(module)
    first_clash = emit_clash(module)
    assert emit_experimental(module) == first_sv
    assert emit_clash(module) == first_clash
    assert "FunctionalRegion" not in first_sv
    assert "FunctionalRegion" not in first_clash


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize(("length", "result_width"), ((32, 13), (33, 14)))
def test_compact_builtin_scalar_sum_direct_sv_passes_strict_verilator(
    tmp_path: Path,
    length: int,
    result_width: int,
) -> None:
    source = (
        f"module ScalarRegionSum{length}{{"
        f"in x:vec<{length},u8> out y:u{result_width} "
        f"y=sum(generate(i in 0..{length}) x[i])}}"
    )
    module = compile_source(source, include_clash=False).ir
    rtl = tmp_path / f"ScalarRegionSum{length}.sv"
    rtl.write_text(emit_experimental(module))
    lint_with_verilator((rtl,), module.name)


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
@pytest.mark.parametrize(("length", "result_width"), ((32, 13), (33, 14)))
def test_compact_builtin_scalar_sum_real_clash_generates_lint_clean_rtl(
    tmp_path: Path,
    length: int,
    result_width: int,
) -> None:
    top = f"ScalarRegionSum{length}"
    source = (
        f"module {top}{{in x:vec<{length},u8> out y:u{result_width} "
        f"y=sum(generate(i in 0..{length}) x[i])}}"
    )
    compilation = compile_source(source)
    rtl = generate_verilog(
        compilation.clash,
        top,
        tmp_path / f"clash_{length}",
        CLASH_EXECUTABLE,
    )
    lint_with_verilator(rtl, top)


COMPLEX_REGION_SOURCE = """
import std.math.complex

module ComplexRegion32 {
    in x : vec<32,Complex<fixed<8,4>>>
    out y : Complex<fixed<8,4>>

    exact = sum(generate(i in 0..32) x[i])
    y = complex_quantize_nearest_even_saturate<fixed<8,4>>(exact)
}
"""


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_compact_nominal_fixed_region_crosses_both_physical_backends(
    tmp_path: Path,
) -> None:
    """Exercise the actual compact IFFT combination at the threshold.

    N=8/N=16 are useful behavioral witnesses but remain below the compaction
    threshold.  This bounded N=32 sum crosses FunctionalRegion,
    ExactReductionPlan, nominal fixed-struct helpers, and one final explicit
    quantization without constructing an N=64 combinational IFFT.
    """

    compilation = compile_source(COMPLEX_REGION_SOURCE)
    module = compilation.ir
    output = module.assignments[0].expression
    assert isinstance(output, Call)
    reduction = output.arguments[0]
    assert isinstance(reduction, Reduce)
    assert isinstance(reduction.collection, FunctionalRegion)
    assert reduction.expanded is None and reduction.plan is not None
    assert len(reduction.plan.levels) == 5

    values = [
        {"re": 1, "im": 1 if index % 2 == 0 else -1}
        for index in range(32)
    ]
    assert simulate(module, x=values) == {"y": {"re": 32, "im": 0}}

    direct = tmp_path / "ComplexRegion32.sv"
    direct.write_text(emit_experimental(module))
    lint_with_verilator((direct,), module.name)

    clash_rtl = generate_verilog(
        emit_clash(module),
        module.name,
        tmp_path / "clash_complex_region",
        CLASH_EXECUTABLE,
    )
    lint_with_verilator(clash_rtl, module.name)
