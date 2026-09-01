from __future__ import annotations

from zlang.ast.nodes import CallExpr, StructConstructExpr, TypeName, VectorTypeName
from zlang.parser import parse


def test_import_alias_and_qualified_reference_forms_are_syntax_only() -> None:
    module = parse(
        """
        import std.math.complex as cx
        module QualifiedSyntax {
            in values : vec<2,cx.Complex<u8>>
            out total : cx.Complex<u9>
            out made : cx.Complex<u8>
            total = cx.complex_sum(values)
            made = cx.Complex { re = values[0].re im = values[0].im }
        }
        """
    )

    assert module.imports[0].path == "std.math.complex"
    assert module.imports[0].alias == "cx"
    assert module.ports[0].type_name == VectorTypeName(
        2, TypeName("cx.Complex<u8>")
    )
    call = module.assignments[0].expression
    constructor = module.assignments[1].expression
    assert isinstance(call, CallExpr)
    assert call.function == "cx.complex_sum"
    assert isinstance(constructor, StructConstructExpr)
    assert constructor.struct_name == "cx.Complex"


def test_legacy_import_has_no_alias() -> None:
    module = parse(
        """
        import std.math.complex
        module LegacySyntax { in x : Complex<u8> out y : Complex<u8> y = x }
        """
    )
    assert module.imports[0].alias is None
