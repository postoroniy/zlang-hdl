"""Shared direct-SV/Clash expression-materialization regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from zlang.backend.clash import emit as emit_clash
from zlang.backend.systemverilog import emit_experimental as emit_systemverilog
from zlang.compiler import compile_source
from zlang.toolchain import (
    find_clash_executable,
    generate_verilog,
    lint_with_verilator,
)


SOURCE = """
struct Pair20 { left : u10 right : u10 }

module MaterializedState {
    clock clk
    reset rst
    in fire : bit
    in a : u8
    in b : u8
    out y : u10

    fifo q : fifo<Pair20,2>

    repeated : u10 = truncate<10>(extend<10>(a + b) + 1)

    rule push when fire {
        q.push(Pair20 { left = repeated right = repeated })
    }

    y = q.front.left
}
"""


AGGREGATE_RESET_SOURCE = """
struct Meta {
    a : u8
    b : u8
    c : u8
    d : u8
    e : u8
}

module AggregateResetMaterialization {
    clock clk
    reset rst
    in fire : bit
    out y : u8

    fifo q : fifo<u8,2>
    reg meta0 : Meta = Meta { a = 0 b = 0 c = 0 d = 0 e = 0 }
    reg meta1 : Meta = Meta { a = 0 b = 0 c = 0 d = 0 e = 0 }

    rule push when fire { q.push(1) }
    y = q.front
}
"""


PURE_CHILD_SOURCE = """
module SharedPureChild {
    in a : u8
    in b : u8
    out y : bits<80>

    chunk : bits<16> = concat(
        bitcast<bits<8>>(a),
        bitcast<bits<8>>(b)
    )
    y = concat(chunk, chunk, chunk, chunk, chunk)
}

module SharedPureParent {
    in a : u8
    in b : u8
    out y : bits<80>

    inst child : SharedPureChild { a b }
    y = child.y
}
"""


CLASH_RESERVED_CALLABLE_PARAMETERS = """
fn clamp_value<type T>(value : T, low : T, high : T) -> T {
    mux(value < low, low, mux(value > high, high, value))
}

module ReservedCallableParameters {
    in value : fixed<8,4>
    in low : fixed<8,4>
    in high : fixed<8,4>
    out y : fixed<8,4>
    y = clamp_value(value, low, high)
}
"""


def test_shared_large_action_operand_is_materialized_once_in_both_backends() -> None:
    module = compile_source(SOURCE, include_clash=False).ir

    clash_first = emit_clash(module)
    clash_second = emit_clash(module)
    assert clash_first == clash_second
    assert clash_first.count("zlang_expr_0 =") == 1
    assert clash_first.count("<$> a <*> b") == 1
    assert "Pair20 (value_0) (value_0)" in clash_first

    sv_first = emit_systemverilog(module)
    sv_second = emit_systemverilog(module)
    assert sv_first == sv_second
    assert sv_first.count("assign zlang_expr_0 =") == 1
    assert sv_first.count("assign zlang_expr_2 =") == 1
    assert "{zlang_expr_0, zlang_expr_0}" in sv_first


def test_static_aggregate_resets_do_not_create_dead_clash_signal_bindings() -> None:
    module = compile_source(AGGREGATE_RESET_SOURCE, include_clash=False).ir

    clash_first = emit_clash(module)
    clash_second = emit_clash(module)
    assert clash_first == clash_second
    assert "zlang_expr_" not in clash_first
    assert clash_first.count("register (Meta") == 2

    # Direct SV renders reset expressions through its procedural expression
    # renderer, so its shared exact-width reset temporary remains live.
    sv_first = emit_systemverilog(module)
    sv_second = emit_systemverilog(module)
    assert sv_first == sv_second
    assert sv_first.count("assign zlang_expr_0 =") == 1
    assert "meta0 <= zlang_expr_0;" in sv_first
    assert "meta1 <= zlang_expr_0;" in sv_first


@pytest.mark.skipif(
    find_clash_executable() is None,
    reason="real Clash 1.11 is unavailable",
)
def test_static_aggregate_resets_compile_with_real_clash(tmp_path: Path) -> None:
    compilation = compile_source(
        AGGREGATE_RESET_SOURCE,
        top="AggregateResetMaterialization",
    )
    rtl = tuple(
        generate_verilog(
            compilation.clash,
            compilation.ir.name,
            tmp_path / "clash_rtl",
            find_clash_executable(),
        )
    )
    assert rtl


def test_pure_hierarchical_child_materializes_shared_typed_expression() -> None:
    compilation = compile_source(PURE_CHILD_SOURCE, top="SharedPureParent")

    first = compilation.clash
    second = compile_source(PURE_CHILD_SOURCE, top="SharedPureParent").clash
    assert first == second
    helper = first[
        first.index("sharedPureChild ::") : first.index("\ntopEntity ::")
    ]
    assert helper.count("zlang_child_expr_0 :: BitVector 16") == 1
    assert helper.count("zlang_child_expr_0 =") == 1
    assert helper.count("zlang_child_expr_0") == 7


def test_callable_parameter_abi_uses_the_same_clash_identifier_mapping() -> None:
    compilation = compile_source(
        CLASH_RESERVED_CALLABLE_PARAMETERS,
        top="ReservedCallableParameters",
    )
    assert "value low_zlang high_zlang =" in compilation.clash
    assert "(low_zlang)" in compilation.clash
    assert "(high_zlang)" in compilation.clash


@pytest.mark.skipif(
    find_clash_executable() is None,
    reason="real Clash 1.11 is unavailable",
)
def test_materialized_pure_hierarchical_child_compiles_with_real_clash(
    tmp_path: Path,
) -> None:
    compilation = compile_source(PURE_CHILD_SOURCE, top="SharedPureParent")
    rtl = tuple(
        generate_verilog(
            compilation.clash,
            compilation.ir.name,
            tmp_path / "clash_child_rtl",
            find_clash_executable(),
        )
    )
    assert rtl
    lint_with_verilator(rtl, compilation.ir.name)


@pytest.mark.skipif(
    find_clash_executable() is None,
    reason="real Clash 1.11 is unavailable",
)
def test_reserved_callable_parameters_compile_with_real_clash(
    tmp_path: Path,
) -> None:
    compilation = compile_source(
        CLASH_RESERVED_CALLABLE_PARAMETERS,
        top="ReservedCallableParameters",
    )
    rtl = tuple(
        generate_verilog(
            compilation.clash,
            compilation.ir.name,
            tmp_path / "clash_callable_rtl",
            find_clash_executable(),
        )
    )
    assert rtl
    lint_with_verilator(rtl, compilation.ir.name)
