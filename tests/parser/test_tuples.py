from __future__ import annotations

import pytest

from zlang.ast import (
    IndexExpr,
    TupleDestructureDecl,
    TupleLiteralExpr,
    TupleTypeName,
)
from zlang.parser import ParseError, parse


def test_tuple_type_literal_projection_and_flat_destructure_have_distinct_ast() -> None:
    syntax = parse(
        "module TupleSyntax { in p:(u8,(bit,u4)) out q:(u8,bit) "
        "(data,last)=q q=(p[0],p[1][0]) }"
    )

    assert isinstance(syntax.ports[0].type_name, TupleTypeName)
    assert isinstance(syntax.ports[0].type_name.elements[1], TupleTypeName)
    destructure = next(
        item for item in syntax.ordered_items
        if isinstance(item, TupleDestructureDecl)
    )
    assert destructure.names == ("data", "last")
    assignment = syntax.assignments[0]
    assert isinstance(assignment.expression, TupleLiteralExpr)
    assert all(isinstance(item, IndexExpr) for item in assignment.expression.elements)


def test_parentheses_remain_grouping_and_final_callable_tuple_is_a_value() -> None:
    syntax = parse(
        "fn pair(x:u8,y:bit){(x,y)} "
        "module Grouping { in x:u8 out y:u8 y=(x) }"
    )
    assert isinstance(syntax.functions[0].body, TupleLiteralExpr)
    assert not isinstance(syntax.assignments[0].expression, TupleLiteralExpr)


@pytest.mark.parametrize(
    "spelling",
    (
        "()",
        "(u8,)",
        "(u1,u1,u1,u1,u1,u1,u1,u1,u1)",
    ),
)
def test_tuple_type_arity_is_bounded(spelling: str) -> None:
    with pytest.raises(ParseError):
        parse(f"module Bad {{ out y:{spelling} y=0 }}")


@pytest.mark.parametrize(
    "spelling",
    (
        "()",
        "(0,)",
        "(0,0,0,0,0,0,0,0,0)",
    ),
)
def test_tuple_literal_arity_is_bounded(spelling: str) -> None:
    with pytest.raises(ParseError):
        parse(f"module Bad {{ out y:u1 y={spelling} }}")


def test_nested_tuple_commas_do_not_split_generic_arguments() -> None:
    syntax = parse(
        "struct Box<type T>{value:T} "
        "module Nested { in x:Box<(u8,u16)> out y:(u8,u16) y=x.value }"
    )
    assert syntax.ports[0].type_name.text == "Box<(u8,u16)>"


def test_tuple_wildcard_spelling_is_explicitly_rejected() -> None:
    with pytest.raises(
        ParseError,
        match="tuple wildcard '_' is not supported",
    ):
        parse("module Bad { in p:(u8,bit) out y:u8 (value,_)=p y=value }")


def test_nested_tuple_destructuring_is_not_part_of_the_bounded_slice() -> None:
    with pytest.raises(ParseError):
        parse(
            "module Bad { in p:((u8,bit),u4) out y:u8 "
            "((value,last),tag)=p y=value }"
        )


def test_tuple_destructuring_keeps_comments_as_whitespace() -> None:
    syntax = parse(
        "module Comments { in p:(u8,bit) out y:u8 "
        "(data, /* component */ last) /* before assignment */ = p y=data }"
    )
    destructure = next(
        item for item in syntax.ordered_items
        if isinstance(item, TupleDestructureDecl)
    )
    assert destructure.names == ("data", "last")


@pytest.mark.parametrize("name", ("when", "Foo"))
def test_tuple_binders_obey_ordinary_immutable_name_rules(name: str) -> None:
    with pytest.raises(ParseError, match="not a legal immutable binding name"):
        parse(
            f"module Bad {{ in p:(u8,bit) out y:bit ({name},last)=p y=last }}"
        )


def test_tuple_disambiguation_does_not_restrict_ordinary_call_whitespace() -> None:
    for call in ("f\n(x)", "f /* comment */\n(x)", "identity<T=u8>\n(x)"):
        functions = (
            "fn f(x:u8){x} fn identity<type T>(x:T){x} "
        )
        syntax = parse(
            functions + f"module Calls {{ in x:u8 out y:u8 y={call} }}"
        )
        assert syntax.assignments[0].expression.function in {"f", "identity"}
