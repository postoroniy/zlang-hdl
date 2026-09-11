from __future__ import annotations

import pytest

from zlang.compiler import compile_source
from zlang.backend.systemverilog import emit_artifact as emit_systemverilog_artifact
from zlang.ir import expressions as expr
from zlang.ir.types import BitsType, UIntType, VecType
from zlang.opt import lower, restore
from zlang.opt.identity import canonical_ir_identity
from zlang.semantic import SemanticError
from zlang.simulate import simulate


def _compile(source: str):
    return compile_source(source).ir


def test_char_and_string_aliases_erase_to_existing_exact_ir() -> None:
    text = _compile(
        "module Text { out letter:char out word:string<2> "
        "letter='A' word=\"AB\" }"
    )
    vector = _compile(
        "module Text { out letter:u8 out word:vec<2,u8> "
        "letter=0x41 word=[0x41,0x42] }"
    )

    assert text.outputs[0].type == UIntType(8)
    assert text.outputs[1].type == VecType(2, UIntType(8))
    assert text.assignments == vector.assignments
    assert lower(text).assignments == lower(vector).assignments
    assert restore(lower(text)).assignments == text.assignments
    assert simulate(text) == {"letter": 0x41, "word": [0x41, 0x42]}


def test_strings_inherit_vector_concat_index_equality_length_and_packing() -> None:
    module = _compile(
        "module TextOps { out joined:string<7> out first:char "
        "out same:bit out count:u3 out packed:bits<16> "
        "joined=concat(\"802.\",\"11a\") first=\"AB\"[0] "
        "same=\"AB\"==\"AB\" count=length(\"ABC\") "
        "packed=pack(\"AB\") }"
    )

    assert simulate(module) == {
        "joined": [56, 48, 50, 46, 49, 49, 97],
        "first": 0x41,
        "same": 1,
        "count": 3,
        "packed": 0x4142,
    }
    values = {item.target.name: item.expression for item in module.assignments}
    assert isinstance(values["joined"], expr.VectorConcat)
    assert values["packed"].type == BitsType(16)


def test_string_types_survive_aliases_functions_and_generic_specialization() -> None:
    module = _compile(
        "type Tag=string<2> "
        "fn first(x:Tag){x[0]} "
        "module Child<type T>{in x:T out y:T y=x} "
        "module Top { out tag:Tag out initial:char "
        "value:Tag=\"OK\" child:Child<T=string<2>> {x=value} "
        "tag=child.y initial=first(value) }"
    )

    assert simulate(module) == {"tag": [79, 75], "initial": 79}
    assert module.outputs[0].type == VecType(2, UIntType(8))
    assert module.outputs[1].type == UIntType(8)


def test_char_and_string_aliases_do_not_create_distinct_overload_identities() -> None:
    with pytest.raises(SemanticError, match="duplicate function 'same'"):
        _compile(
            "fn same(x:char){x} fn same(x:u8){x} "
            "module Top { out y:u8 y=same('A') }"
        )

    char_module = _compile("module Alias { out y:char y='A' }")
    byte_module = _compile("module Alias { out y:u8 y=0x41 }")
    assert char_module.assignments == byte_module.assignments
    assert canonical_ir_identity(lower(char_module)) == canonical_ir_identity(
        lower(byte_module)
    )

    string_module = _compile('module Alias { out y:string<2> y="OK" }')
    vector_module = _compile(
        "module Alias { out y:vec<2,u8> y=[0x4f,0x4b] }"
    )
    assert canonical_ir_identity(lower(string_module)) == canonical_ir_identity(
        lower(vector_module)
    )
    string_artifact = emit_systemverilog_artifact(string_module)
    vector_artifact = emit_systemverilog_artifact(vector_module)
    assert string_artifact.text == vector_artifact.text
    assert string_artifact.artifact_hash == vector_artifact.artifact_hash


def test_string_literal_is_a_compile_time_vector_for_parameters_and_rom_images() -> None:
    parameterized = _compile(
        "module Holder<image:string<2>> { out first:char first=image[0] } "
        "module Top { out y:char image:string<2>=\"OK\" "
        "h:Holder<image=image> y=h.first }"
    )
    rom = _compile(
        "module TextRom { clock clk reset rst in address:u1 out y:char "
        "rom table:rom<char,2>{read_latency 1 init \"OK\"} "
        "table.read_address=address y=table.read_data }"
    )

    assert simulate(parameterized) == {"y": 79}
    assert rom.roms[0].element_type == UIntType(8)
    assert tuple(item.value for item in rom.roms[0].contents) == (79, 75)


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            'module Empty { out y:vec<1,u8> y="" }',
            "empty string literal is not supported",
        ),
        (
            'module Length { out y:string<3> y="AB" }',
            "string literal has exact type vec<2,u8>, expected exact vec<3,u8>",
        ),
        (
            'module Numeric { out y:u16 y="AB" }',
            "string literal has exact type vec<2,u8>, expected exact u16",
        ),
        (
            "module CharWidth { out y:u16 y='A' }",
            "character literal has exact type u8, expected exact u16",
        ),
    ),
)
def test_text_literals_reject_implicit_length_or_numeric_conversion(
    source: str, message: str
) -> None:
    with pytest.raises(SemanticError, match=message):
        _compile(source)


@pytest.mark.parametrize(
    "source",
    (
        "type string=u8 module Top{out y:u8 y=0}",
        "struct string{value:u8} module Top{out y:u8 y=0}",
    ),
)
def test_string_type_family_name_cannot_be_redeclared(source: str) -> None:
    with pytest.raises(
        SemanticError,
        match="'string' uses a reserved type name",
    ):
        _compile(source)
