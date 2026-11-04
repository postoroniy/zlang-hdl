"""Exact arithmetic keeps carry bits without pre-extending both operands."""

from itertools import product

import pytest

from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.native_simulation import simulate


@pytest.mark.parametrize(("width", "output_width"), ((8, 9), (8, 16), (8, 80), (64, 65), (64, 80)))
def test_exact_addition_preserves_carry_and_final_widening(width, output_width):
    prefix = (
        f"module ExactAdd {{ in value:uint<{width}> in increment:uint<1> "
        f"out result:uint<{output_width}> "
    )
    verbose = compile_source(
        prefix + f"result=truncate<{output_width}>(extend<{output_width}>(value)+"
        f"extend<{output_width}>(increment)) }}"
    ).ir
    addition = "value+increment"
    concise = compile_source(
        prefix + "result=" + (addition if output_width == width + 1
                              else f"extend<{output_width}>({addition})") + " }"
    ).ir
    bindings = emit_artifact(concise).bindings
    assert any(binding.rtl_path == "result" and binding.width == output_width for binding in bindings)
    for value, increment in product((0, 1, (1 << width) - 2, (1 << width) - 1), (0, 1)):
        expected = value + increment
        assert simulate(concise, value=value, increment=increment)["result"] == expected
        assert simulate(verbose, value=value, increment=increment)["result"] == expected
