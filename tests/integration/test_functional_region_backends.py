from __future__ import annotations

from pathlib import Path
import shutil

import pytest

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
from zlang.toolchain import lint_with_verilator


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




@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_ready_valid_payload_region_direct_sv_passes_strict_verilator(
    tmp_path: Path,
) -> None:
    module = compile_source(RV_PAYLOAD_REGION_SOURCE).ir
    rtl = tmp_path / "ReadyValidPayloadRegion64.sv"
    rtl.write_text(emit_experimental(module))
    lint_with_verilator((rtl,), module.name)




@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_length32_exact_plan_direct_sv_passes_strict_verilator(tmp_path: Path) -> None:
    module, _, _ = _nominal_reduction_module()
    rtl = tmp_path / "NominalRegion32.sv"
    rtl.write_text(emit_experimental(module))
    lint_with_verilator((rtl,), module.name)






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
    module = compile_source(source).ir
    rtl = tmp_path / f"ScalarRegionSum{length}.sv"
    rtl.write_text(emit_experimental(module))
    lint_with_verilator((rtl,), module.name)




COMPLEX_REGION_SOURCE = """
import std.math.complex

module ComplexRegion32 {
    in x : vec<32,Complex<fixed<8,4>>>
    out y : Complex<fixed<8,4>>

    exact = sum(generate(i in 0..32) x[i])
    y = complex_quantize_nearest_even_saturate<fixed<8,4>>(exact)
}
"""
