from __future__ import annotations

import pytest

from zlang.ast import EnumMemberRef, FieldExpr, SwitchExpr, TypeName, TypeValueExpr
from zlang.parser import ParseError, parse


def test_enum_declaration_preserves_member_order_and_origin() -> None:
    syntax = parse(
        "enum Phase { Idle Header Payload Done } "
        "module M { out y:Phase y=Phase.Header }"
    )
    assert len(syntax.enums) == 1
    declaration = syntax.enums[0]
    assert declaration.name == "Phase"
    assert declaration.members == ("Idle", "Header", "Payload", "Done")
    assert declaration.origin is not None
    assert declaration.origin.start_line == 1
    assert isinstance(syntax.assignments[0].expression, FieldExpr)


def test_enum_switch_keys_are_qualified_and_else_is_optional() -> None:
    syntax = parse(
        "enum Phase { Idle Active } module M { out y:u1 "
        "y=switch Phase.Active { Phase.Idle=>0 Phase.Active=>1 } }"
    )
    expression = syntax.assignments[0].expression
    assert isinstance(expression, SwitchExpr)
    assert expression.default is None
    assert tuple(arm.key for arm in expression.arms) == (
        EnumMemberRef("Phase", "Idle"),
        EnumMemberRef("Phase", "Active"),
    )


def test_empty_enum_is_rejected_by_the_grammar() -> None:
    with pytest.raises(ParseError, match="syntax error"):
        parse("enum Empty {} module M { out y:u1 y=0 }")


def test_explicit_enum_preserves_backing_type_and_sparse_codes() -> None:
    syntax = parse(
        "enum WifiRate : bits<3> { Continue=0 Bpsk6=1 Qpsk12=2 Qam16_24=4 } "
        "module M { out y:WifiRate y=WifiRate.Qam16_24 }"
    )
    declaration = syntax.enums[0]
    assert declaration.backing_type == TypeName("bits<3>")
    assert declaration.members == ("Continue", "Bpsk6", "Qpsk12", "Qam16_24")
    assert declaration.encodings == (0, 1, 2, 4)


def test_compile_time_numeric_and_bare_nominal_conditions_both_parse() -> None:
    numeric = parse(
        "module M<N=1> { if N == 1 { out y:u1 y=1 } "
        "else { out y:u1 y=0 } }"
    )
    assert not isinstance(numeric.compile_time_ifs[0].condition.left, TypeValueExpr)

    nominal = parse(
        "enum E { A B } module M<type T> { if T == E { out y:u1 y=1 } "
        "else { out y:u1 y=0 } }"
    )
    condition = nominal.compile_time_ifs[0].condition
    # Uppercase identifiers stay syntactically neutral. Semantic resolution
    # decides whether they denote types or compile-time values, which also
    # permits nominal comparisons inside larger boolean conditions.
    assert not isinstance(condition.left, TypeValueExpr)
    assert not isinstance(condition.right, TypeValueExpr)
