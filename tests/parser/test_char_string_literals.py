from __future__ import annotations

import pytest

from zlang.ast import CharLiteralExpr, StringLiteralExpr, TypeName, VectorTypeName
from zlang.parser import ParseError, parse


def test_char_and_string_types_and_literals_have_explicit_syntax_nodes() -> None:
    syntax = parse(
        "module Text<N=3> { out letter:char out word:string<N> "
        "letter='A' word=\"ABC\" }"
    )

    assert syntax.ports[0].type_name == TypeName("char")
    assert syntax.ports[1].type_name == VectorTypeName("N", TypeName("char"))
    letter, word = (item.expression for item in syntax.assignments)
    assert isinstance(letter, CharLiteralExpr)
    assert letter.value == 0x41
    assert isinstance(word, StringLiteralExpr)
    assert word.values == (0x41, 0x42, 0x43)
    assert letter.origin is not None
    assert word.origin is not None


def test_byte_literal_escapes_and_comment_markers_are_lexically_atomic() -> None:
    syntax = parse(
        r'''module Escapes {
            out chars:vec<8,char> out slash:string<2> out block:string<2>
            chars=['\0','\n','\r','\t','\\','\'','\"','\xff']
            slash="//" block="/*"
        }'''
    )

    vector, slash, block = (item.expression for item in syntax.assignments)
    assert tuple(item.value for item in vector.elements) == (
        0,
        10,
        13,
        9,
        92,
        39,
        34,
        255,
    )
    assert slash.values == (47, 47)
    assert block.values == (47, 42)


@pytest.mark.parametrize(
    ("escape", "expected"),
    (
        (r"\0", 0),
        (r"\n", 10),
        (r"\r", 13),
        (r"\t", 9),
        (r"\\", 92),
        (r"\'", 39),
        (r'\"', 34),
        (r"\x00", 0),
        (r"\x7f", 127),
        (r"\xff", 255),
        (r"\xFF", 255),
    ),
)
def test_every_documented_escape_is_accepted_by_string_literals(
    escape: str, expected: int
) -> None:
    syntax = parse(
        f'module EscapedString {{ out value:string<1> value="{escape}" }}'
    )
    expression = syntax.assignments[0].expression
    assert isinstance(expression, StringLiteralExpr)
    assert expression.values == (expected,)


@pytest.mark.parametrize("literal", ("' '", "'~'", '" "', '"~"'))
def test_printable_ascii_boundaries_are_accepted(literal: str) -> None:
    type_ = "char" if literal.startswith("'") else "string<1>"
    parse(f"module Boundary {{ out value:{type_} value={literal} }}")


@pytest.mark.parametrize(
    "literal",
    (
        r"'\x0'",
        r"'\xGG'",
        r'"\x0"',
        r'"\xGG"',
        "'\t'",
        '"\t"',
        "'\x01'",
        '"\x01"',
        "'\x1f'",
        '"\x1f"',
        "'\x7f'",
        '"\x7f"',
    ),
)
def test_malformed_hex_and_raw_control_boundaries_are_parse_errors(
    literal: str,
) -> None:
    with pytest.raises(ParseError):
        parse(f"module Bad {{ out y:u8 y={literal} }}")


@pytest.mark.parametrize(
    "literal",
    (
        "''",
        "'AB'",
        r"'\q'",
        "'é'",
        '"é"',
        '"line\nbreak"',
    ),
)
def test_malformed_or_non_ascii_byte_literals_are_parse_errors(literal: str) -> None:
    with pytest.raises(ParseError):
        parse(f"module Bad {{ out y:u8 y={literal} }}")


def test_bare_or_zero_length_string_types_are_rejected() -> None:
    with pytest.raises(ParseError):
        parse("module Bare { out y:string y=0 }")
    with pytest.raises(ParseError):
        parse('module EmptyType { out y:string<0> y="A" }')
