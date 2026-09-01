"""Identity-preserving concise lowering accepted after formal closure."""

import pytest

from zlang.compiler import compile_source
from zlang.ir.module import RulePriority
from zlang.opt.identity import canonical_ir_identity
from zlang.opt.lowering import lower, restore
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


def _ir(source: str):
    return compile_source(source, include_clash=False).ir


def test_priority_chain_is_exact_adjacent_pair_sugar() -> None:
    prefix = (
        "module M { clock clk reset rst in a,b,c:bit out y:u8 reg x:u8=0 "
        "a0: when a { x <- 1 } a1: when b { x <- 2 } "
        "a2: when c { x <- 3 } "
    )
    concise = _ir(prefix + "priority a0 > a1 > a2 y=x }")
    verbose = _ir(prefix + "priority a0 > a1 priority a1 > a2 y=x }")
    expected = (RulePriority("a0", "a1"), RulePriority("a1", "a2"))
    assert concise.rule_priorities == verbose.rule_priorities == expected
    assert concise.resolved_transition == verbose.resolved_transition
    assert canonical_ir_identity(lower(concise)) == canonical_ir_identity(lower(verbose))


def test_contextual_resize_is_exact_explicit_resize_sugar() -> None:
    concise = _ir(
        "module M { in wide:u16 in narrow:s8 out lo:u8 out hi:s16 "
        "lo=truncate(wide) hi=extend(narrow) }"
    )
    explicit = _ir(
        "module M { in wide:u16 in narrow:s8 out lo:u8 out hi:s16 "
        "lo=truncate<8>(wide) hi=extend<16>(narrow) }"
    )
    assert concise.assignments == explicit.assignments
    assert canonical_ir_identity(lower(concise)) == canonical_ir_identity(lower(explicit))
    assert restore(lower(concise)) == concise


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            "module M { in x:u16 out y:u16 tmp=truncate(x) y=x }",
            "requires an explicit typed",
        ),
        (
            "module M { in x:u8 out y:s16 y=extend(x) }",
            "typed boundary requires exact s16",
        ),
        (
            "module M { in x:u16 out y:u8 y=extend(x) }",
            "cannot extend u16 to width 8",
        ),
    ),
)
def test_contextual_resize_fails_closed(source: str, message: str) -> None:
    with pytest.raises(SemanticError, match=message):
        _ir(source)


def test_compile_time_index_arithmetic_is_mathematical() -> None:
    concise = _ir(
        "module M { in values:vec<4,u8> out y:vec<4,u8> "
        "y=generate(i in 0..4) values[1+i-1] }"
    )
    explicit = _ir(
        "module M { in values:vec<4,u8> out y:vec<4,u8> "
        "y=generate(i in 0..4) values[truncate<2>(1+extend<2>(i)-1)] }"
    )
    assert concise.assignments == explicit.assignments
    assert canonical_ir_identity(lower(concise)) == canonical_ir_identity(lower(explicit))


def test_generated_collection_uses_exact_raw_function_return_boundary() -> None:
    concise = _ir(
        "fn reverse4(x:bits<4>) -> bits<4> { "
        "generate(i in 0..4) x[3-i] } "
        "module M { in x:bits<4> out y:bits<4> y=reverse4(x) }"
    )
    explicit = _ir(
        "fn reverse4(x:bits<4>) -> bits<4> { "
        "bitcast<bits<4>>(generate(i in 0..4) x[3-i]) } "
        "module M { in x:bits<4> out y:bits<4> y=reverse4(x) }"
    )
    assert concise.functions == explicit.functions
    assert canonical_ir_identity(lower(concise)) == canonical_ir_identity(lower(explicit))


def test_index_width_and_earlier_parameter_default_are_compile_time_only() -> None:
    module = analyze(parse(
        "module M<N=4,COUNT=N*2,IW=index_width(COUNT)> { out y:uint<IW> y=IW }"
    ))
    assert module.parameters == (
        ("N", "value", 4),
        ("COUNT", "value", 8),
        ("IW", "value", 3),
    )
    assert str(module.ports[0].type) == "u3"

    singleton = analyze(parse(
        "module M<N=1,IW=index_width(N)> { out y:uint<IW> y=IW }"
    ))
    assert singleton.parameters[-1] == ("IW", "value", 1)

    with pytest.raises(SemanticError, match="index_width requires a positive"):
        analyze(parse(
            "module M<N=0,IW=index_width(N)> { out y:uint<IW> y=0 }"
        ))


@pytest.mark.parametrize(
    "source",
    (
        "module M<IW=index_width(N),N=4> { out y:uint<IW> y=IW }",
        "module M<A=B+1,B=A+1> { out y:uint<A> y=A }",
    ),
)
def test_value_parameter_defaults_cannot_reference_later_declarations(
    source: str,
) -> None:
    with pytest.raises(
        SemanticError,
        match="references later parameter",
    ):
        analyze(parse(source))


def test_dependent_defaults_resolve_for_child_specialization() -> None:
    top = _ir(
        "module Child<N=4,IW=index_width(N)> { "
        "in x:uint<IW> out y:uint<IW> y=x } "
        "module Top { in x:u2 out y:u2 child:Child { x=x } y=child.y }"
    )
    assert tuple(
        (item.name, item.value)
        for item in top.instances[0].specializations
    ) == (
        ("N", 4),
        ("IW", 2),
    )


def test_child_forward_default_cannot_capture_parent_parameter() -> None:
    with pytest.raises(
        SemanticError,
        match="references later parameter 'N'",
    ):
        _ir(
            "module Child<IW=index_width(N),N=4> { "
            "in x:uint<IW> out y:uint<IW> y=x } "
            "module Top<N=8> { in x:u3 out y:u3 child:Child { x=x } y=child.y }"
        )


def test_static_vector_range_is_exact_vector_literal_sugar() -> None:
    concise = _ir(
        "module M { in values:vec<6,u8> out y:vec<3,u8> y=values[1..4] }"
    )
    explicit = _ir(
        "module M { in values:vec<6,u8> out y:vec<3,u8> "
        "y=[values[1],values[2],values[3]] }"
    )
    assert concise.assignments == explicit.assignments
    assert canonical_ir_identity(lower(concise)) == canonical_ir_identity(lower(explicit))


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            "module M { in x:u8 out y:bits<2> y=x[0..2] }",
            "vector range requires a vector",
        ),
        (
            "module M { in x:vec<4,u8> out y:vec<1,u8> y=x[2..2] }",
            "is empty",
        ),
        (
            "module M { in x:vec<4,u8> out y:vec<2,u8> y=x[3..5] }",
            "is out of range",
        ),
    ),
)
def test_static_vector_range_rejects_invalid_ranges(
    source: str, message: str
) -> None:
    with pytest.raises(SemanticError, match=message):
        _ir(source)
