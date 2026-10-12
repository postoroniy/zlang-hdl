import pytest

from zlang.ast import FixedRoundingMode, QuantizeExpr, RationalExpr
from zlang.parser import ParseError, parse


def _assignment(source: str):
    return parse(source).assignments[0].expression


def test_decimal_literals_are_exact_rational_ast_nodes() -> None:
    positive = _assignment("module F { out y:SF8.8 y=0.1 }")
    negative = _assignment("module F { out y:SF8.8 y=-1.25 }")
    assert positive == RationalExpr(1, 10)
    assert negative == RationalExpr(-5, 4)


def test_contextual_and_full_quantize_parse_to_one_ast_node() -> None:
    contextual = _assignment(
        "module F { out y:SF8.8 y=quantize(0.1,nearest_even) }"
    )
    explicit = _assignment(
        "module F { in a:SF4.4 out y:SF2.2 "
        "y=quantize<SF2.2>(a){round away_zero overflow saturate} }"
    )
    assert isinstance(contextual, QuantizeExpr)
    assert contextual.rounding is FixedRoundingMode.NEAREST_EVEN
    assert contextual.target_type is None
    assert isinstance(explicit, QuantizeExpr)
    assert explicit.rounding is FixedRoundingMode.AWAY_ZERO
    assert explicit.target_type.text == "SF2.2"


def test_dot_keeps_two_argument_form_and_accepts_rounding_argument() -> None:
    exact = _assignment(
        "module F { in a:vec<2,SF2.2> in b:vec<2,SF2.2> "
        "out y:fixed<9,4> y=dot(a,b) }"
    )
    rounded = _assignment(
        "module F { in a:vec<2,SF2.2> in b:vec<2,SF2.2> "
        "out y:SF4.2 y=dot(a,b,floor) }"
    )
    assert exact.rounding is None
    assert rounded.rounding is FixedRoundingMode.FLOOR


@pytest.mark.parametrize("mode", ["nearest", "truncate", "ceiling"])
def test_unknown_rounding_modes_are_rejected(mode: str) -> None:
    with pytest.raises(ParseError):
        parse(f"module F {{ out y:SF8.8 y=quantize(0.1,{mode}) }}")
