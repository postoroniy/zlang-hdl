# SPDX-License-Identifier: Apache-2.0
"""The semantic reference preserves exact conversion width and signedness."""

import pytest
from zlang.compiler import compile_source
from zlang.backend.systemverilog import emit_artifact
from zlang.equivalence import emit_reference_model
from zlang.formal import run_verilog_formal
from zlang.ir.formal import ProofMode, FormalStatus


@pytest.mark.parametrize(
    "sw,sf,ow,of",
    ((33, 8, 16, 14), (33, 8, 33, 10), (27, 20, 16, 14), (8, 4, 5, 2), (8, 2, 5, 4)),
)
@pytest.mark.parametrize(
    "rounding", ("nearest_even", "floor", "away_zero", "toward_zero")
)
def test_reference_fixed_conversion_exact_carrier(sw, sf, ow, of, rounding, tmp_path):
    result = compile_source(f"""module Direct {{in x:fixed<{sw},{sf}> out y:fixed<{ow},{of}>
y=quantize<fixed<{ow},{of}>>(x) {{ round {rounding} overflow saturate }} }}""")
    value = result.ir.assignments[0].expression
    ref = emit_reference_model(
        "Reference", "y", value.type, (("x", value.expression.type),), value
    )
    source = (
        emit_artifact(result.ir).text
        + ref
        + f"module top(input [{sw - 1}:0] x);wire[{ow - 1}:0] a,b;Direct dut(.x,.y(a));Reference oracle(.x,.y(b));always @* assert(a==b);endmodule\n"
    )
    proof = run_verilog_formal(
        source,
        top="top",
        property_id="fixed-conversion",
        mode=ProofMode.PROVE,
        depth=8,
        timeout_seconds=30,
        systemverilog=True,
        flatten=True,
        work_directory=tmp_path,
    )
    assert proof.status is FormalStatus.PROVEN, proof
