from pathlib import Path

from zlang.compiler import compile_source
from zlang.backend.systemverilog import emit_experimental
from zlang.ir import expressions as expr
from zlang.ir.types import FixedType, StructType


ROOT = Path(__file__).resolve().parents[2]


def test_contextual_parameterized_struct_constructor_is_exact() -> None:
    result = compile_source(
        "struct Pair<type T>{left:T right:T} "
        "fn pair(a:fixed<18,16>,b:fixed<18,16>)"
        "->Pair<fixed<18,16>>{Pair{left=a right=b}} "
        "module Top{in a:fixed<18,16> in b:fixed<18,16> "
        "out y:Pair<fixed<18,16>> y=pair(a,b)}"
    )
    pair = result.ir.functions[0].body
    assert isinstance(pair, expr.StructConstruct)
    assert isinstance(pair.type, StructType)
    assert pair.type.name == "Pair<fixed<18,16>>"
    assert all(isinstance(field.type, FixedType) for field in pair.type.fields)


def test_complex_butterfly_and_typed_stream_compile_to_both_backends() -> None:
    source = (ROOT / "examples/complex_fft_butterfly.zl").read_text()
    butterfly = compile_source(source, top="ComplexFFTButterfly")
    stream = compile_source(source, top="ComplexStreamIdentity")
    assert "complex_mul_18_16" not in butterfly.clash
    assert "complex_add_18_16" not in butterfly.clash
    assert any(
        item.name == "operator*" and str(item.return_type) == "Complex<fixed<35,30>>"
        for item in butterfly.ir.generic_specializations
    )
    assert [str(port.type) for port in butterfly.ir.inputs] == [
        "Complex<fixed<18,16>>",
        "Complex<fixed<18,16>>",
        "Complex<fixed<16,14>>",
    ]
    assert "module ComplexFFTButterfly" in emit_experimental(butterfly.ir)
    endpoint = stream.ir.aggregate_protocol_endpoints[0]
    assert endpoint.specialization_identity.startswith("AXIStreamOf<T=Complex<fixed<18,16>>")
    assert isinstance(endpoint.members[0].payload_type, StructType)
    assert "module ComplexStreamIdentity" in emit_experimental(stream.ir)
