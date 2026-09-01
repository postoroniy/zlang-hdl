"""Backend smoke for concrete compile-time generated vectors."""

from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source


def test_generated_vector_reaches_both_concrete_backends() -> None:
    compilation = compile_source(
        "module Generated<N=4> { out y:vec<4,u8> "
        "y=generate(i in 0..N) i }"
    )
    assert "0 :: Unsigned 8" in compilation.clash
    assert "3 :: Unsigned 8" in compilation.clash
    artifact = emit_artifact(compilation.ir)
    assert "assign y = {8'd0, 8'd1, 8'd2, 8'd3};" in artifact.text


def test_compile_time_real_intrinsic_quantizes_to_backend_constant() -> None:
    compilation = compile_source(
        "module RealConst { out y:SF_Sat2.14 "
        "y=quantize<SF_Sat2.14>(cos(pi() / 4)){round nearest_even overflow saturate} }"
    )
    assert "(11585 :: Signed 16)" in compilation.clash
    artifact = emit_artifact(compilation.ir)
    assert "assign y = 16'sd11585;" in artifact.text
    assert "cos" not in compilation.clash
    assert "sin" not in artifact.text
