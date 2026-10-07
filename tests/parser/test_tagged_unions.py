from __future__ import annotations

from pathlib import Path

from zlang.ast import (
    FieldExpr,
    SwitchExpr,
    TaggedUnionConstructExpr,
    TaggedUnionMatchExpr,
)
from zlang.parser import parse


ROOT = Path(__file__).resolve().parents[2]


def test_union_declaration_constructor_and_match_preserve_source_order() -> None:
    syntax = parse(
        "union Message { Idle Data { value:u8 } Error { code:bits<4> } } "
        "module M { in x:u8 out y:u8 "
        "m:Message=Message.Data { value=x } "
        "y=match m { Message.Idle=>0 Message.Data { value }=>value "
        "Message.Error { code }=>extend<8>(code) } }"
    )
    (declaration,) = syntax.tagged_unions
    assert declaration.name == "Message"
    assert tuple(item.name for item in declaration.variants) == (
        "Idle", "Data", "Error",
    )
    assert tuple(item.name for item in declaration.variants[1].fields) == ("value",)
    constructor = syntax.generic_declarations[0].initializer
    assert isinstance(constructor, TaggedUnionConstructExpr)
    expression = syntax.assignments[0].expression
    assert isinstance(expression, TaggedUnionMatchExpr)
    assert tuple((arm.union_name, arm.variant, arm.binders) for arm in expression.arms) == (
        ("Message", "Idle", ()),
        ("Message", "Data", ("value",)),
        ("Message", "Error", ("code",)),
    )


def test_enum_references_and_switches_coexist_with_union_syntax() -> None:
    syntax = parse(
        "enum Phase { Idle Active } union U { Empty Value { x:u1 } } "
        "module M { in phase:Phase out y:u1 "
        "u:U=U.Value { x=1 } "
        "e:u1=switch phase { Phase.Idle=>0 Phase.Active=>1 } "
        "c:u1=switch Phase.Active { Phase.Idle=>0 Phase.Active=>1 } "
        "y=match u { U.Empty=>0 U.Value { x }=>x } }"
    )
    assert isinstance(syntax.generic_declarations[0].initializer, TaggedUnionConstructExpr)
    assert isinstance(syntax.generic_declarations[1].initializer, SwitchExpr)
    assert isinstance(syntax.generic_declarations[2].initializer, SwitchExpr)
    assert isinstance(syntax.assignments[0].expression, TaggedUnionMatchExpr)
    assert isinstance(syntax.generic_declarations[2].initializer.selector, FieldExpr)


def test_real_wifi_enum_switch_regression_parses_without_arm_separators() -> None:
    source = (
        ROOT
        / "examples/projects/80211a_transmitter/src/interleaver.zhl"
    )
    if source.exists():
        parse(source.read_text())
