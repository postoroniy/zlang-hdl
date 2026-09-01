from __future__ import annotations

from dataclasses import replace

import pytest

from zlang.compiler import compile_source
from zlang.ir.expressions import Constant, Switch, UnionConstruct, UnionField, UnionTag
from zlang.ir.module import Assignment, Module, Port, PortDirection
from zlang.ir.packing import pack_tagged_union_runtime, tagged_union_field_slice
from zlang.ir.runtime_values import TaggedUnionValue
from zlang.ir.type_codec import (
    TypeCodecError,
    canonical_type_data,
    canonical_type_from_data,
)
from zlang.ir.types import (
    BitsType,
    FixedType,
    SIntType,
    TaggedUnionField,
    TaggedUnionType,
    TaggedUnionVariant,
    UIntType,
)
from zlang.opt import OptimizationStage, lower, restore
from zlang.opt.ir import ExpressionOp
from zlang.semantic import SemanticError
from zlang.simulate import simulate, simulate_cycles


SOURCE = """
union Message {
    Idle
    Data { value : s8 }
    Gain { value : fixed<8,4> }
}
module UnionValue {
    in kind : u2
    in data : s8
    in gain : fixed<8,4>
    out raw : bits<8>
    message : Message = switch kind {
        0 => Message.Idle
        1 => Message.Data { value = data }
        else => Message.Gain { value = gain }
    }
    selected : s8 = match message {
        Message.Idle => 0
        Message.Data { value } => value
        Message.Gain { value } => fixed_to_raw(value)
    }
    raw = bitcast<bits<8>>(selected)
}
"""


STATE_SOURCE = """
union Message { Idle Data { value : s8 } Error { code : bits<4> } }
module UnionState {
    clock clk
    reset rst
    in load : bit
    in data : s8
    out raw : bits<8>
    reg message : Message = Message.Idle
    when load { message <- Message.Data { value = data } }
    selected : s8 = match message {
        Message.Idle => 0
        Message.Data { value } => value
        Message.Error { code } => bitcast<s8>(extend<8>(code))
    }
    raw = bitcast<bits<8>>(selected)
}
"""


def _compile(source: str = SOURCE, top: str | None = None):
    return compile_source(source, top=top, include_clash=False).ir


def test_nominal_layout_match_lowering_and_runtime_values_are_exact() -> None:
    module = _compile()
    (union_type,) = module.tagged_unions
    assert union_type.tag_width == 2
    assert union_type.payload_width == 8
    assert union_type.width == 10
    assert tuple(union_type.tag(item.name) for item in union_type.variants) == (0, 1, 2)
    expression = module.assignments[0].expression.expression
    assert isinstance(expression, Switch)
    assert isinstance(expression.selector, UnionTag)
    assert any(
        isinstance(case.expression, UnionField)
        for case in expression.cases
    )
    assert simulate(module, kind=0, data=-7, gain=0) == {"raw": 0}
    assert simulate(module, kind=1, data=-7, gain=0) == {"raw": 0xF9}
    assert simulate(module, kind=2, data=0, gain=-19) == {"raw": 0xED}


def test_bare_fieldless_constructor_is_a_constant_register_initializer() -> None:
    module = _compile(STATE_SOURCE, "UnionState")
    assert isinstance(module.registers[0].initial, UnionConstruct)
    assert module.registers[0].initial.variant == "Idle"
    assert simulate_cycles(
        module,
        (
            {"load": 0, "data": 0},
            {"load": 1, "data": -7},
            {"load": 0, "data": 0},
            {"load": 0, "data": 0},
        ),
        (True, False, False, False),
    ) == [
        {"raw": 0}, {"raw": 0}, {"raw": 0xF9}, {"raw": 0xF9},
    ]


def test_union_canonical_round_trip_retains_declaration_and_nodes() -> None:
    module = _compile(STATE_SOURCE, "UnionState")
    canonical = lower(module, stage=OptimizationStage.HIGH_LEVEL)
    assert canonical.tagged_unions == module.tagged_unions
    ops = {node.op for node in canonical.expressions}
    assert {ExpressionOp.UNION_CONSTRUCT, ExpressionOp.UNION_TAG, ExpressionOp.UNION_FIELD} <= ops
    assert restore(canonical) == module


@pytest.mark.parametrize(
    "source,detail",
    (
        ("union E {} module M { out y:u1 y=0 }", "must contain at least one variant"),
        ("union U { A A } module M { out y:u1 y=0 }", "duplicate variant"),
        ("union U { A { x:u1 x:u1 } } module M { out y:u1 y=0 }", "duplicate field"),
        ("struct S { x:u1 } union U { A { x:S } } module M { out y:u1 y=0 }", "flat scalar"),
        ("union U { A { x:u1 } } module M { out y:U y=U.A {} }", "missing x"),
        ("union A { V { x:u1 } } union B { V { x:u1 } } module M { out y:A y=B.V { x=1 } }", "expected exact A"),
        ("union A { V } union B { V } module M { out y:u1 y=match A.V { B.V=>0 } }", "does not belong"),
        ("union U { A } module M { out y:u1 y=match U.A { U.A=>0 U.A=>1 } }", "duplicate match"),
        ("union U { A B } module M { out y:u1 y=match U.A { U.A=>0 } }", "missing tagged-union match"),
        ("union U { A } module M { in x:U out y:u1 y=0 }", "cannot expose tagged-union type"),
    ),
)
def test_union_diagnostics_fail_closed(source: str, detail: str) -> None:
    with pytest.raises(SemanticError, match=detail):
        _compile(source)


def test_module_and_canonical_declaration_tables_reject_forged_nominal_types() -> None:
    declared = TaggedUnionType(
        "U", (TaggedUnionVariant("A", (TaggedUnionField("x", UIntType(8)),)),),
        "declared",
    )
    forged = TaggedUnionType(
        "U", (TaggedUnionVariant("A", (TaggedUnionField("x", UIntType(8)),)),),
        "forged",
    )
    output = Port(PortDirection.OUTPUT, "y", forged)
    with pytest.raises(ValueError, match="absent from the exact module declaration"):
        Module(
            "Bad",
            (output,),
            (
                Assignment(
                    output,
                    UnionConstruct(
                        "A", (("x", Constant(0, UIntType(8))),), forged
                    ),
                ),
            ),
            tagged_unions=(declared,),
        )

    module = _compile(STATE_SOURCE, "UnionState")
    canonical = lower(module, stage=OptimizationStage.HIGH_LEVEL)
    forged_canonical = replace(
        module.tagged_unions[0], declaration_identity="forged-canonical"
    )
    constructor = next(
        node for node in canonical.expressions
        if node.op is ExpressionOp.UNION_CONSTRUCT
    )
    malformed = replace(constructor, type=forged_canonical)
    with pytest.raises(ValueError, match="exact canonical declaration"):
        replace(
            canonical,
            expressions=tuple(
                malformed if node.id == constructor.id else node
                for node in canonical.expressions
            ),
        )


def test_union_type_codec_and_runtime_packing_freeze_exact_layout() -> None:
    module = _compile()
    (union_type,) = module.tagged_unions
    assert canonical_type_from_data(canonical_type_data(union_type)) == union_type
    assert pack_tagged_union_runtime(
        TaggedUnionValue(union_type, "Idle")
    ) == 0
    assert pack_tagged_union_runtime(
        TaggedUnionValue(union_type, "Data", (("value", -7),))
    ) == 0x1F9
    assert pack_tagged_union_runtime(
        TaggedUnionValue(union_type, "Gain", (("value", -19),))
    ) == 0x2ED
    assert tagged_union_field_slice(union_type, "Data", "value") == (7, 0)

    malformed = canonical_type_data(union_type)
    malformed["variants"][1]["fields"][0]["type"] = {
        "kind": "struct",
        "name": "Nested",
        "fields": [{"name": "x", "type": {"kind": "bit", "width": 1}}],
    }
    with pytest.raises(TypeCodecError, match="flat scalar"):
        canonical_type_from_data(malformed)


def test_canonical_union_nodes_reject_wrong_variant_field_and_tag_width() -> None:
    module = _compile(STATE_SOURCE, "UnionState")
    canonical = lower(module, stage=OptimizationStage.HIGH_LEVEL)
    constructor = next(node for node in canonical.expressions if node.op is ExpressionOp.UNION_CONSTRUCT and node.attribute("variant") == "Data")
    bad_constructor = replace(
        constructor,
        attributes=(("variant", "Missing"), ("field_names", ("value",))),
    )
    with pytest.raises(ValueError, match="union constructor.*invalid layout"):
        replace(canonical, expressions=tuple(bad_constructor if node.id == constructor.id else node for node in canonical.expressions))

    tag = next(node for node in canonical.expressions if node.op is ExpressionOp.UNION_TAG)
    bad_tag_type = BitsType(tag.type.width + 1)
    bad_tag = replace(
        tag,
        type=bad_tag_type,
        metadata=replace(tag.metadata, width=bad_tag_type.width),
    )
    with pytest.raises(ValueError, match="union tag.*invalid types"):
        replace(canonical, expressions=tuple(bad_tag if node.id == tag.id else node for node in canonical.expressions))

    field = next(node for node in canonical.expressions if node.op is ExpressionOp.UNION_FIELD)
    bad_field = replace(field, attributes=(("variant", "Data"), ("field", "missing")))
    with pytest.raises(ValueError, match="union field.*invalid projection"):
        replace(canonical, expressions=tuple(bad_field if node.id == field.id else node for node in canonical.expressions))
