"""Post-corpus syntax ergonomics normalization coverage."""

from __future__ import annotations

import pytest

from zlang.compiler import compile_source
from zlang.ir import expressions as ir_expr
from zlang.parser import parse
from zlang.semantic import SemanticError, analyze


def test_grouped_ports_expand_in_source_order_with_distinct_ir_ports() -> None:
    result = analyze(parse(
        "module Top { in a, b:u8 out sum, carry:bit "
        "sum = a < b carry = a == b }"
    ))
    assert [port.name for port in result.ports] == ["a", "b", "sum", "carry"]
    assert [port.direction.value for port in result.ports] == [
        "input", "input", "output", "output"
    ]
    assert [port.name for port in result.ports].count("sum") == 1


def test_parser_retains_group_and_inline_source_origin_until_normalization() -> None:
    syntax = parse("module Top { in a, b:u8 out y:u9 = a + b }")
    grouped, inline = syntax.ports
    assert grouped.names == ("a", "b")
    assert grouped.origin is not None
    assert inline.initializer is not None
    assert inline.origin is not None


def test_inline_output_is_the_same_typed_ir_as_verbose_assignment() -> None:
    concise = compile_source("module Top { in a:u8 in b:u8 out y:u9 = a + b }").ir
    verbose = compile_source("module Top { in a:u8 in b:u8 out y:u9 y = a + b }").ir
    assert concise == verbose
    assert len(concise.assignments) == 1
    assert isinstance(concise.assignments[0].expression, ir_expr.Add)


def test_inline_output_preserves_domain_and_port_order() -> None:
    result = compile_source(
        "module Top { clock clk reset rst in a:u8 @clk "
        "out y:u8 @clk = a }"
    )
    assert [port.name for port in result.ir.ports] == ["a", "y"]
    assert result.ir.ports[-1].domain == "clk"


@pytest.mark.parametrize(
    "source, message",
    [
        (
            "module Top { in a:u8 = 0 out y:u8 y = a }",
            "input port 'a' cannot have an initializer",
        ),
        (
            "module Top { out p:rv<u8> = x }",
            "protocol port 'p' cannot have an initializer",
        ),
        (
            "module Top { out a,b:u8 = 0 }",
            "grouped port declarations cannot have an initializer",
        ),
        (
            "module Top { in a,a:u8 out y:u8 y = a }",
            "duplicate port",
        ),
    ],
)
def test_port_ergonomics_rejections_are_targeted(source: str, message: str) -> None:
    with pytest.raises(SemanticError, match=message):
        analyze(parse(source))


def test_struct_field_punning_matches_explicit_constructor() -> None:
    prefix = "struct Pair { left:u8 right:u8 }"
    shorthand = compile_source(
        prefix + " module Top { in left:u8 in right:u8 out y:Pair "
        "y = Pair { left right } }"
    ).ir
    explicit = compile_source(
        prefix + " module Top { in left:u8 in right:u8 out y:Pair "
        "y = Pair { left = left right = right } }"
    ).ir
    assert shorthand == explicit
    fields = shorthand.assignments[0].expression.fields
    assert [name for name, _ in fields] == ["left", "right"]


def test_struct_punning_supports_mixed_fields_and_ordinary_lookup_errors() -> None:
    prefix = "struct Pair { left:u8 right:u8 }"
    mixed = compile_source(
        prefix + " module Top { in left:u8 in fallback:u8 out y:Pair "
        "y = Pair { left right = fallback } }"
    ).ir
    assert [name for name, _ in mixed.assignments[0].expression.fields] == [
        "left", "right"
    ]
    with pytest.raises(SemanticError, match="fields mismatch"):
        analyze(parse(
            prefix + " module Top { in left:u8 out y:Pair "
            "y = Pair { left missing } }"
        ))
