# SPDX-License-Identifier: Apache-2.0
"""Module specialization retains resolved nominal types without reparsing display text."""

import pytest

from zlang.compiler import compile_source
from zlang.ir.types import EnumType, TaggedUnionType
from zlang.semantic import SemanticError


@pytest.mark.parametrize("specialization", ("<T=Mode>", ""))
@pytest.mark.parametrize("nested", (False, True))
def test_enum_module_parameter_keeps_its_original_nominal_identity(specialization, nested):
    text = """enum Mode:bits<2>{Idle=0 Busy=1 Done=2}
module Identity<type T>{in x:T out y:T y=x}
module Relay<type T>{in x:T out y:T child:Identity<T=T> child.x=x y=child.y}
module Top {in raw:bits<2> out encoded:bits<2>
 child:UNITARGS child.x=enum_decode<Mode>(raw,Mode.Idle) encoded=enum_encode(child.y)}
""".replace("UNITARGS", ("Relay" if nested else "Identity") + specialization)
    module = compile_source(text, top="Top").ir
    child_type = module.children[0].inputs[0].type
    assert isinstance(child_type, EnumType)
    assert child_type == next(item for item in module.enums if item.name == "Mode")
    assert child_type.width == 2 and child_type.explicit_backing_kind == "bits"


@pytest.mark.parametrize("type_name,value", (
    ("vec<2,Mode>", "[Mode.Idle,Mode.Busy]"),
    ("(Mode,Mode)", "(Mode.Idle,Mode.Busy)"),
    ("Packet", "Packet.Data {value=-7}"),
))
def test_nominal_types_inside_aggregate_module_parameters(type_name, value):
    text = f"""enum Mode:bits<2>{{Idle=0 Busy=1}}
union Packet {{Idle Data {{value:s8}}}}
module Identity<type T>{{in x:T out y:T y=x}}
module Top {{out y:{type_name}
 child:Identity<T={type_name}> child.x={value} y=child.y}}
"""
    module = compile_source(text, top="Top").ir
    assert module.children[0].inputs[0].type == module.outputs[0].type
    if type_name == "Packet":
        assert isinstance(module.outputs[0].type, TaggedUnionType)


def test_enum_type_facts_remain_available_to_deferred_callable_arguments():
    text = """enum Mode:bits<2>{Idle=0 Busy=1}
fn same(x:Mode)->Mode{x}
module Apply<type T,op:fn(T)->T>{in x:T out y:T y=op(x)}
module Top {in raw:bits<2> out y:bits<2>
 value:Mode=enum_decode<Mode>(raw,Mode.Idle)
 inst child:Apply<T=Mode,op=fn same> child.x=value y=enum_encode(child.y)}
"""
    assert compile_source(text, top="Top").ir.outputs[0].type.width == 2


def test_same_width_nominal_enums_cannot_be_substituted():
    text = """enum First:bits<2>{Idle=0 Busy=1}
enum Second:bits<2>{Idle=0 Busy=1}
module Identity<type T>{in x:T out y:T y=x}
module Top {in raw:bits<2> out y:bits<2>
 value:First=enum_decode<First>(raw,First.Idle)
 child:Identity<T=Second> child.x=value y=enum_encode(child.y)}
"""
    with pytest.raises(SemanticError, match="First|Second"):
        compile_source(text, top="Top")
