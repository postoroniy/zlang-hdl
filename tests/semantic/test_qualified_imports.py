from __future__ import annotations

import pytest

from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.opt.lowering import restore
from zlang.semantic import SemanticError


LEGACY = """
import std.math.complex
module QualifiedComplex {
    in values : vec<2,Complex<u8>>
    out total : Complex<u9>
    out made : Complex<u8>
    total = complex_sum(values)
    made = Complex { re = values[0].re im = values[0].im }
}
"""


QUALIFIED = """
import std.math.complex as cx
module QualifiedComplex {
    in values : vec<2,cx.Complex<u8>>
    out total : cx.Complex<u9>
    out made : cx.Complex<u8>
    total = cx.complex_sum(values)
    made = cx.Complex { re = values[0].re im = values[0].im }
}
"""


def test_alias_normalizes_to_existing_semantic_and_canonical_identity() -> None:
    legacy = compile_source(LEGACY)
    qualified = compile_source(QUALIFIED)

    assert qualified.ir == legacy.ir
    assert qualified.selected_ir_identity == legacy.selected_ir_identity
    assert restore(qualified.optimization_ir) == qualified.ir
    assert qualified.clash == legacy.clash

    legacy_sv = emit_sv_artifact(
        legacy.ir, selected_ir_identity=legacy.selected_ir_identity
    )
    qualified_sv = emit_sv_artifact(
        qualified.ir, selected_ir_identity=qualified.selected_ir_identity
    )
    assert qualified_sv.text == legacy_sv.text
    assert qualified_sv.artifact_hash == legacy_sv.artifact_hash
    assert qualified_sv.build_identity == legacy_sv.build_identity


def test_legacy_unqualified_import_behavior_is_preserved() -> None:
    result = compile_source(LEGACY, include_clash=False)
    assert tuple(output.name for output in result.ir.outputs) == ("total", "made")


def test_qualified_generic_function_explicit_specialization_and_operators() -> None:
    source = """
    import std.math.complex as cx
    module ExplicitQualified {
        in a : cx.Complex<u8>
        in b : cx.Complex<u8>
        out sum : cx.Complex<u9>
        out reduced : cx.Complex<u9>
        sum = a + b
        reduced = cx.complex_sum<T=u8,N=2>([a,b])
    }
    """
    result = compile_source(source, include_clash=False)
    assert tuple(str(output.type) for output in result.ir.outputs) == (
        "Complex<u9>",
        "Complex<u9>",
    )


@pytest.mark.parametrize(
    ("source", "code", "message"),
    (
        (
            """
            import std.math.complex as cx
            module Bad { in x : nope.Complex<u8> out y : u8 y = 0 }
            """,
            "ZL-IMPORT-ALIAS-UNKNOWN",
            "unknown import alias 'nope'",
        ),
        (
            """
            import std.math.complex as cx
            module Bad { in x : cx.Missing<u8> out y : u8 y = 0 }
            """,
            "ZL-IMPORT-MEMBER-UNKNOWN",
            "has no type member 'Missing'",
        ),
        (
            """
            import std.math.complex as cx
            module Bad { in x : Complex<u8> out y : Complex<u8> y = x }
            """,
            "ZL-IMPORT-ALIAS-REQUIRED",
            "requires its import alias",
        ),
    ),
)
def test_alias_diagnostics_are_structured(
    source: str, code: str, message: str
) -> None:
    with pytest.raises(SemanticError) as caught:
        compile_source(source, include_clash=False)
    assert caught.value.code == code
    assert message in str(caught.value)


def test_duplicate_alias_is_rejected_before_typing() -> None:
    source = """
    import std.math.complex as math
    import std.math.fixed as math
    module Bad { in x : u8 out y : u8 y = x }
    """
    with pytest.raises(SemanticError) as caught:
        compile_source(source, include_clash=False)
    assert caught.value.code == "ZL-IMPORT-ALIAS-DUPLICATE"
    assert "duplicate import alias 'math'" in str(caught.value)


def test_alias_does_not_reexport_a_transitive_dependency() -> None:
    source = """
    import std.dsp.fft as fft
    module Bad { in x : fft.Complex<u8> out y : u8 y = 0 }
    """
    with pytest.raises(SemanticError) as caught:
        compile_source(source, include_clash=False)
    assert caught.value.code == "ZL-IMPORT-MEMBER-UNKNOWN"
