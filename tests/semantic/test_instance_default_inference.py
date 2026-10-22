"""Internal defaults must not force premature sibling-input evaluation."""

import pytest

from zlang.compiler import compile_source
from zlang.semantic import SemanticError


def source(width="W=8,", actual="u8"):
    return f"""
module Cell<W=8,D=2> {{
    in x:uint<W> out y:uint<W> y=x
}}
module Top {{
    in a:{actual} out y:{actual}
    first:Cell<{width}D=2>
    later:Cell<W=8,D=2>
    first.x=later.y
    later.x=a
    y=first.y
}}
"""


def test_unrelated_default_does_not_evaluate_forward_sibling():
    implicit = compile_source(
        source().replace("first:Cell<W=8,D=2>", "first:Cell<W=8>"), top="Top"
    )
    explicit = compile_source(source(), top="Top")
    assert implicit.ir == explicit.ir


def test_input_parameter_inference_still_overrides_its_default():
    result = compile_source(
        """
module Cell<W=8,D=2>{in x:uint<W> out y:uint<W> y=x}
module Top{in a:u16 out y:u16 c:Cell<D=2> c.x=a y=c.y}
""",
        top="Top",
    )
    assert str(result.ir.children[0].inputs[0].type) == "u16"


def test_concrete_input_type_mismatch_is_not_suppressed():
    with pytest.raises(SemanticError, match="u16|u8"):
        compile_source(source(actual="u16"), top="Top")
