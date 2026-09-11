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
