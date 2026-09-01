from zlang.ast import StructDestructureDecl, StructUpdateExpr, VectorLiteralExpr
from zlang.parser import parse


def test_vector_literal_and_immutable_struct_update_have_explicit_ast_nodes() -> None:
    syntax = parse(
        "struct Beat { data:u8 last:bit } module M { "
        "in beat:Beat out taps:vec<3,u8> out next:Beat "
        "taps=[1,2,3] next=beat with { last=1 } }"
    )

    literal, update = (item.expression for item in syntax.assignments)
    assert isinstance(literal, VectorLiteralExpr)
    assert len(literal.elements) == 3
    assert isinstance(update, StructUpdateExpr)
    assert update.fields[0].name == "last"
    assert literal.origin is not None
    assert update.origin is not None


def test_exhaustive_immutable_struct_destructure_has_explicit_syntax_node() -> None:
    syntax = parse(
        "struct Beat { data:u8 last:bit } module M { "
        "in beat:Beat out y:u8 Beat { data, last } = beat y=data }"
    )

    declaration = next(
        item for item in syntax.ordered_items
        if isinstance(item, StructDestructureDecl)
    )
    assert declaration.fields == ("data", "last")
    assert declaration.origin is not None
