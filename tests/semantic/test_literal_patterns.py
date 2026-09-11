from __future__ import annotations

from dataclasses import replace

import pytest

from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.runtime_values import minimum_signed_width, minimum_unsigned_width
from zlang.ir.types import BitsType, SIntType, UIntType
from zlang.opt import CanonicalizationError, OptimizationStage, lower, restore
from zlang.opt.ir import ExpressionOp
from zlang.parser import parse
from zlang.semantic import SemanticError
from zlang.semantic import analyze
from zlang.simulate import simulate


def _compile(source: str):
    return compile_source(source).ir


def _analyze(source: str):
    return analyze(parse(source))


@pytest.mark.parametrize(
    ("value", "width"),
    ((0, 1), (1, 1), (2, 2), (3, 2), (4, 3), (255, 8)),
)
def test_context_free_nonnegative_literals_use_minimum_unsigned_width(
    value: int, width: int
) -> None:
    module = _analyze(f"module M {{ value={value} out y:u16 y=value }}")
    local = module.locals[0]
    assert local.type == UIntType(width)
    assert local.expression == expr.Constant(
        value,
        UIntType(width),
        origin=local.expression.origin,
    )


@pytest.mark.parametrize(
    ("value", "width"),
    ((-1, 1), (-2, 2), (-3, 3), (-4, 3), (-123, 8), (-128, 8), (-129, 9)),
)
def test_context_free_negative_literals_use_minimum_signed_width(
    value: int, width: int
) -> None:
    module = _analyze(f"module M {{ value={value} out y:s16 y=value }}")
    local = module.locals[0]
    assert local.type == SIntType(width)
    assert isinstance(local.expression, expr.Constant)
    assert local.expression.value == value
    assert local.expression.origin is not None
    assert local.expression.origin.construct == f"literal {value}"


def test_literal_only_compounds_preserve_typed_operator_widths() -> None:
    module = _analyze(
        "module M { "
        "total=3+0 packed=concat(3+0,1) modular=-(1+0) double=--1 negzero=-0 "
        "out y:u3 y=total }"
    )
    locals_by_name = {item.name: item.expression for item in module.locals}
    assert locals_by_name["total"] == expr.Constant(3, UIntType(3))
    assert locals_by_name["packed"].type == BitsType(4)
    assert locals_by_name["modular"] == expr.Constant(3, UIntType(2))
    assert locals_by_name["double"] == expr.Constant(1, SIntType(2))
    assert locals_by_name["negzero"] == expr.Constant(0, SIntType(1))


def test_negative_direct_literals_are_contextual_in_operators_and_alternatives() -> None:
    module = _analyze(
        "module M { in x:fixed<8,4> in sx:s8 in choose:bit in selector:u1 "
        "plus=x+-1 reverse=-1+x equal=x==-1 "
        "left=choose ? -1 : sx right=choose ? sx : -1 "
        "selected=switch selector { 0=>-1 else=>sx } }"
    )
    locals_by_name = {item.name: item.expression for item in module.locals}
    assert str(locals_by_name["plus"].type) == "fixed<9,4>"
    assert str(locals_by_name["reverse"].type) == "fixed<9,4>"
    assert str(locals_by_name["equal"].type) == "bit"
    assert locals_by_name["left"].type == SIntType(8)
    assert locals_by_name["right"].type == SIntType(8)
    assert locals_by_name["selected"].type == SIntType(8)


def test_contextual_negative_literal_widens_without_unsigned_subtraction() -> None:
    module = _compile("module M { out y:s10 y=-123 }")
    value = module.assignments[0].expression
    assert value == expr.Constant(-123, SIntType(10), origin=value.origin)
    assert simulate(module) == {"y": -123}


@pytest.mark.parametrize("spelling", ("123", "0x7b", "0b111_1011", "000123"))
def test_positive_literal_radix_and_leading_zeros_do_not_change_identity(
    spelling: str,
) -> None:
    module = _analyze(f"module M {{ value={spelling} }}")
    assert module.locals[0].expression.type == UIntType(7)
    assert module.locals[0].expression.value == 123


@pytest.mark.parametrize("spelling", ("-123", "-0x7b", "-0b111_1011", "-000123"))
def test_negative_literal_radix_and_leading_zeros_do_not_change_identity(
    spelling: str,
) -> None:
    module = _analyze(f"module M {{ value={spelling} }}")
    assert module.locals[0].expression.type == SIntType(8)
    assert module.locals[0].expression.value == -123


def test_contextual_negative_fixed_literal_is_exact_and_range_checked() -> None:
    module = _compile("module M { out y:fixed<8,4> y=-1 }")
    value = module.assignments[0].expression
    assert value.type == module.outputs[0].type
    assert value.value == -16

    with pytest.raises(SemanticError, match="constant -129 does not fit s8"):
        _compile("module M { out y:s8 y=-129 }")

    for value, raw in ((-8, -128), (7, 112)):
        fitted = _compile(f"module M {{ out y:fixed<8,4> y={value} }}")
        assert fitted.assignments[0].expression.value == raw
    for value in (-9, 8):
        with pytest.raises(SemanticError, match="does not fit fixed<8,4>"):
            _compile(f"module M {{ out y:fixed<8,4> y={value} }}")


@pytest.mark.parametrize("target", ("bit", "u8", "bits<8>", "ufixed<8,4>"))
def test_negative_literal_rejects_unsigned_and_raw_contexts(target: str) -> None:
    with pytest.raises(
        SemanticError, match="negative integer literal -1 cannot initialize unsigned/raw"
    ):
        _compile(f"module M {{ out y:{target} y=-1 }}")


def test_ordinary_zeros_and_ones_are_exact_width_raw_constants() -> None:
    module = _compile(
        "module M<N=7> { out z:bits<7> out o:bits<8> out raw:u8 "
        "z=zeros<N> o=ones<N+1> raw=ones<8> }"
    )
    assignments = {item.target.name: item.expression for item in module.assignments}
    assert assignments["z"].type == BitsType(7)
    assert assignments["z"].value == 0
    assert assignments["o"].type == BitsType(8)
    assert assignments["o"].value == 0xFF
    assert isinstance(assignments["raw"], expr.Bitcast)
    assert assignments["raw"].expression == expr.Constant(
        0xFF,
        BitsType(8),
        origin=assignments["raw"].expression.origin,
    )
    assert simulate(module) == {"z": 0, "o": 0xFF, "raw": 0xFF}


def test_width_patterns_accept_parentheses_and_compile_time_intrinsics() -> None:
    module = _compile(
        "module M<N=8> { out z:bits<9> out o:bits<3> "
        "z=zeros<(N+1)> o=ones<floor_log2(N)> }"
    )
    assert simulate(module) == {"z": 0, "o": 7}


def test_singular_zero_remains_equiv_only() -> None:
    with pytest.raises(SemanticError, match="zero<x> is available only in equiv"):
        _compile("module M { out y:bits<8> y=zero<x> }")


def test_width_patterns_reject_nonpositive_and_incompatible_targets() -> None:
    with pytest.raises(SemanticError, match="type width must be positive"):
        _compile("module M<W=0> { out y:bit y=zeros<W> }")
    with pytest.raises(SemanticError, match="produces bits<8>, expected exact u7"):
        _compile("module M { out y:u7 y=ones<8> }")
    with pytest.raises(
        SemanticError,
        match="unresolved compile-time parameter 'W'",
    ):
        _compile("module M { out y:bits<8> y=zeros<W> }")


def test_concat_literal_widths_remain_exact() -> None:
    module = _compile(
        "module M { out a:bits<2> out b:bits<3> "
        "a=concat(0,1) b=concat(3,0) }"
    )
    assignments = {item.target.name: item.expression for item in module.assignments}
    assert assignments["a"].type == BitsType(2)
    assert assignments["b"].type == BitsType(3)
    assert simulate(module) == {"a": 1, "b": 6}


def test_concat_width_failure_reports_each_exact_operand() -> None:
    with pytest.raises(SemanticError) as caught:
        _compile("module M { out y:bits<5> y=concat(3,0) }")

    error = caught.value
    assert error.code == "ZL-WIDTH-CONCAT"
    assert str(error) == "concat produces bits<3>, expected exact bits<5>"
    assert error.notes == (
        "operand 1: exact type u2, packed width 2",
        "operand 2: exact type u1, packed width 1",
        "total packed width: 3",
    )
    assert error.primary is not None
    assert error.primary.construct == "concat"
    assert error.fixes


def test_minimum_width_helpers_cover_twos_complement_boundaries() -> None:
    assert [minimum_unsigned_width(value) for value in (0, 1, 2, 3, 4)] == [1, 1, 2, 2, 3]
    assert [minimum_signed_width(value) for value in (-1, -2, -3, -4, 0, 1)] == [
        1,
        2,
        3,
        3,
        1,
        2,
    ]


def test_literal_constants_round_trip_and_malformed_canonical_value_fails() -> None:
    module = _compile("module M { out n:s8 out raw:bits<4> n=-5 raw=ones<4> }")
    canonical = lower(module, stage=OptimizationStage.HIGH_LEVEL)
    assert restore(canonical) == module

    index, node = next(
        (index, node)
        for index, node in enumerate(canonical.expressions)
        if node.op is ExpressionOp.CONSTANT and node.type == BitsType(4)
    )
    malformed_node = replace(
        node,
        attributes=tuple(
            (name, 16 if name == "value" else value)
            for name, value in node.attributes
        ),
    )
    malformed = replace(
        canonical,
        expressions=canonical.expressions[:index]
        + (malformed_node,)
        + canonical.expressions[index + 1 :],
    )
    with pytest.raises(CanonicalizationError, match="value 16 does not fit exact type bits<4>"):
        restore(malformed)


def test_packed_constant_spelling_does_not_change_ir_or_artifact_identity() -> None:
    concise = compile_source(
        "module M{out z:bits<8> out o:bits<8> z=zeros<8> o=ones<8>}",
    )
    literal = compile_source(
        "module M{out z:bits<8> out o:bits<8> z=0 o=255}",
    )
    assert concise.high_level_ir_identity == literal.high_level_ir_identity
    assert concise.selected_ir_identity == literal.selected_ir_identity
    concise_artifact = emit_sv_artifact(
        concise.ir,
        selected_ir_identity=concise.selected_ir_identity,
    )
    literal_artifact = emit_sv_artifact(
        literal.ir,
        selected_ir_identity=literal.selected_ir_identity,
    )
    assert concise_artifact.text == literal_artifact.text
    assert concise_artifact.artifact_hash == literal_artifact.artifact_hash


def test_malformed_canonical_concat_width_metadata_fails_closed() -> None:
    module = _compile("module M { out y:bits<3> y=concat(3,0) }")
    canonical = lower(module, stage=OptimizationStage.HIGH_LEVEL)
    index, node = next(
        (index, node)
        for index, node in enumerate(canonical.expressions)
        if node.op is ExpressionOp.CONCAT
    )
    malformed_node = replace(
        node,
        attributes=tuple(
            (name, (1, 1) if name == "operand_widths" else value)
            for name, value in node.attributes
        ),
    )
    malformed = replace(
        canonical,
        expressions=canonical.expressions[:index]
        + (malformed_node,)
        + canonical.expressions[index + 1 :],
    )
    with pytest.raises(
        CanonicalizationError,
        match="operand widths do not match",
    ):
        restore(malformed)
