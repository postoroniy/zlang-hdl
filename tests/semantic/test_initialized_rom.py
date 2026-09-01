from __future__ import annotations

import pytest

from zlang.backend.companions import companion_for_rom
from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.ir.constants import ConstantExpressionError, constant_runtime_value
from zlang.ir.storage import RomSignal
from zlang.ir.types import (
    BitsType,
    EnumType,
    FixedType,
    SIntType,
    StructType,
    UIntType,
    VecType,
)
from zlang.semantic import SemanticError
from zlang.simulate import simulate_cycles


def _compile(source: str):
    return compile_source(source, include_clash=False).ir


SCALAR_ROM = """
module ScalarRom<D=4> {
    clock clk
    reset rst
    in address : u2
    out data : u8
    rom table : rom<u8,D> {
        read_latency 1
        init generate(i in 0..D) i
    }
    table.read_address = address
    data = table.read_data
}
"""


def test_scalar_rom_semantics_identity_contents_and_origin() -> None:
    module = _compile(SCALAR_ROM)
    assert len(module.roms) == 1
    rom = module.roms[0]
    assert (rom.name, rom.depth, rom.element_type) == ("table", 4, UIntType(8))
    assert rom.address_type == UIntType(2)
    assert rom.read_latency == 1
    assert tuple(constant_runtime_value(word) for word in rom.contents) == (0, 1, 2, 3)
    assert all(isinstance(word, expr.Constant) for word in rom.contents)
    assert isinstance(rom.read_address, expr.InputRef)
    assert rom.semantic_id.startswith("rom:")
    assert len(rom.initialization_identity) == 64
    assert len(rom.content_hash) == 64
    assert rom.evaluator_schema == "zlang-ct-v1"
    assert rom.source_origin is not None
    assignment = module.assignments[0]
    assert isinstance(assignment.expression, expr.RomRef)
    assert assignment.expression.signal is RomSignal.READ_DATA


def test_rom_one_cycle_latency_reset_and_contents_persist() -> None:
    module = _compile(SCALAR_ROM)
    outputs = simulate_cycles(
        module,
        (
            {"address": 3},
            {"address": 1},
            {"address": 2},
            {"address": 0},
            {"address": 3},
            {"address": 1},
            {"address": 0},
        ),
        (True, False, False, False, True, False, False),
    )
    # Reset clears only the registered result.  The first address accepted
    # after each reset appears exactly one cycle later; immutable words remain.
    assert outputs == [
        {"data": 0},
        {"data": 0},
        {"data": 1},
        {"data": 2},
        {"data": 0},
        {"data": 0},
        {"data": 1},
    ]


def test_depth_one_and_non_power_of_two_roms_are_legal_with_proven_addresses() -> None:
    one = _compile("""
        module One { clock clk reset rst out y:u8
          rom t:rom<u8,1>{ read_latency 1 init generate(i in 0..1) 7 }
          t.read_address=0 y=t.read_data }
    """)
    assert one.roms[0].address_type == UIntType(1)
    assert [item["y"] for item in simulate_cycles(
        one, ({}, {}, {}), (True, False, False)
    )] == [0, 0, 7]

    three = _compile("""
        module Three { clock clk reset rst in high:bit out y:u8
          rom t:rom<u8,3>{ read_latency 1 init generate(i in 0..3) i }
          t.read_address=mux(high,2,0) y=t.read_data }
    """)
    assert three.roms[0].depth == 3
    assert three.roms[0].address_type == UIntType(2)


def test_fixed_struct_vector_and_nested_constant_contents() -> None:
    fixed = _compile("""
        module FixedRom { clock clk reset rst out y:SF2.2
          rom t:rom<SF2.2,2>{
            read_latency 1
            init generate(i in 0..2) 1.5
          }
          t.read_address=0 y=t.read_data }
    """)
    assert fixed.roms[0].element_type == FixedType(4, 2)
    assert tuple(constant_runtime_value(word) for word in fixed.roms[0].contents) == (6, 6)

    aggregate = _compile("""
        struct Pair { hi:u4 lo:u4 }
        struct Entry { pair:Pair lanes:vec<2,u4> }
        module AggregateRom { clock clk reset rst in address:u1 out y:Entry
          rom t:rom<Entry,2>{
            read_latency 1
            init generate(i in 0..2) Entry {
              pair = Pair { hi = i lo = i }
              lanes = generate(j in 0..2) j
            }
          }
          t.read_address=address y=t.read_data }
    """)
    element = aggregate.roms[0].element_type
    assert isinstance(element, StructType)
    assert isinstance(element.fields[1].type, VecType)
    assert constant_runtime_value(aggregate.roms[0].contents[1]) == {
        "pair": {"hi": 1, "lo": 1},
        "lanes": (0, 1),
    }


def test_rom_initializer_evaluates_constant_slice_concat_and_bitcast_layout() -> None:
    module = _compile("""
        module PackedRom { clock clk reset rst in address:u2 out y:u8
          rom t:rom<u8,4>{
            read_latency 1
            init generate(i in 0..4)
              bitcast<u8>(concat(i[1:0], 0b10_1010))
          }
          t.read_address=address y=t.read_data }
    """)

    assert tuple(
        constant_runtime_value(word) for word in module.roms[0].contents
    ) == (0x2A, 0x6A, 0xAA, 0xEA)
    assert companion_for_rom(module.roms[0]).text == (
        "00101010\n01101010\n10101010\n11101010\n"
    )


def test_register_initializers_accept_constant_struct_generate_map_and_reshape() -> None:
    module = _compile("""
        struct Pair { hi:u4 lo:u4 }
        module ConstantRegisters { clock clk reset rst out raw:bits<8>
          reg pair:Pair=Pair{hi=10 lo=3}
          reg flat:vec<4,u4>=reshape<vec<4,u4>>(
            generate(i in 0..2)
              generate(j in 0..2) extend<4>(j)
          )
          reg joined:vec<4,u4>=concat(
            generate(i in 0..2) extend<4>(i),
            map(i in 0..2) { extend<4>(i) }
          )
          raw=pack(pair) }
    """)

    assert tuple(
        (register.name, constant_runtime_value(register.initial))
        for register in module.registers
    ) == (
        ("pair", {"hi": 10, "lo": 3}),
        ("flat", (0, 1, 0, 1)),
        ("joined", (0, 1, 0, 1)),
    )
    assert simulate_cycles(
        module, ({}, {}, {}), (True, False, False)
    ) == [{"raw": 0xA3}, {"raw": 0xA3}, {"raw": 0xA3}]


def test_compact_functional_region_is_a_register_reset_constant() -> None:
    module = _compile("""
        module CompactRegister { clock clk reset rst out y:u8
          reg values:vec<32,u8>=generate(i in 0..32) i
          y=values[31] }
    """)
    initial = module.registers[0].initial
    assert isinstance(initial, expr.FunctionalRegion)
    assert constant_runtime_value(initial) == tuple(range(32))
    assert simulate_cycles(
        module, ({}, {}, {}), (True, False, False)
    ) == [{"y": 31}, {"y": 31}, {"y": 31}]


def test_compact_functional_region_materializes_bounded_rom_contents() -> None:
    module = _compile("""
        module CompactRom { clock clk reset rst in address:u5 out y:u8
          rom table:rom<u8,32>{
            read_latency 1
            init generate(i in 0..32) i
          }
          table.read_address=address
          y=table.read_data }
    """)
    rom = module.roms[0]
    assert len(rom.contents) == 32
    assert tuple(constant_runtime_value(word) for word in rom.contents) == tuple(
        range(32)
    )
    assert all(isinstance(word, expr.Constant) for word in rom.contents)
    assert [item["y"] for item in simulate_cycles(
        module,
        ({"address": 31}, {"address": 7}, {"address": 0}),
        (True, False, False),
    )] == [0, 0, 7]


def test_constant_runtime_value_covers_legacy_pack_unpack_and_collection_nodes() -> None:
    u4 = UIntType(4)
    bits4 = BitsType(4)
    bits8 = BitsType(8)
    left = expr.Pack(expr.Constant(0xA, u4), bits4)
    right = expr.Pack(expr.Constant(0x3, u4), bits4)
    joined = expr.Concat((left, right), bits8)

    assert constant_runtime_value(joined) == 0xA3
    assert constant_runtime_value(expr.Slice(joined, 5, 2, bits4)) == 0x8
    assert constant_runtime_value(expr.Unpack(joined, SIntType(8))) == -93
    assert constant_runtime_value(
        expr.Bitcast(joined, VecType(2, u4))
    ) == (0xA, 0x3)

    row_type = VecType(2, u4)
    flat_type = VecType(4, u4)
    first = expr.Generate(
        "i", 0, 2, (expr.Constant(1, u4), expr.Constant(2, u4)), row_type
    )
    second = expr.Map(
        "i", 0, 2, (expr.Constant(3, u4), expr.Constant(4, u4)), row_type
    )
    vector_concat = expr.VectorConcat((first, second), flat_type)
    assert constant_runtime_value(vector_concat) == (1, 2, 3, 4)

    nested = expr.Generate(
        "i", 0, 2, (first, second), VecType(2, row_type)
    )
    assert constant_runtime_value(expr.Reshape(nested, flat_type)) == (1, 2, 3, 4)


def test_constant_representation_operations_reject_enum_layout() -> None:
    enum = EnumType("E", ("A", "B"), "test:E")
    packed = expr.Pack(expr.Constant(0, enum), BitsType(1))
    with pytest.raises(ConstantExpressionError, match="not bit-packable"):
        constant_runtime_value(packed)


def test_rom_content_and_specialization_identity_are_deterministic_and_sensitive() -> None:
    first = _compile(SCALAR_ROM).roms[0]
    same = _compile(SCALAR_ROM).roms[0]
    changed = _compile(SCALAR_ROM.replace("0..D) i", "0..D) 7")).roms[0]
    assert (first.semantic_id, first.initialization_identity, first.content_hash) == (
        same.semantic_id,
        same.initialization_identity,
        same.content_hash,
    )
    assert changed.content_hash != first.content_hash
    assert changed.initialization_identity != first.initialization_identity


@pytest.mark.parametrize(
    ("source", "message"),
    (
        (
            "module R { clock clk reset rst out y:u8 "
            "rom t:rom<u8,2>{read_latency 2 init generate(i in 0..2) i} "
            "t.read_address=0 y=t.read_data }",
            "requires exactly read_latency 1",
        ),
        (
            "module R { clock clk reset rst in a:u2 out y:u8 "
            "rom t:rom<u8,3>{read_latency 1 init generate(i in 0..3) i} "
            "t.read_address=a y=t.read_data }",
            "static range 0..3 is not within 0..2",
        ),
        (
            "module R { clock clk reset rst in x:u8 out y:u8 "
            "rom t:rom<u8,2>{read_latency 1 init generate(i in 0..2) x} "
            "t.read_address=0 y=t.read_data }",
            "initializer must contain compile-time constants only",
        ),
        (
            "module R { clock clk reset rst out y:u8 "
            "rom t:rom<u8,3>{read_latency 1 init generate(i in 0..2) i} "
            "t.read_address=0 y=t.read_data }",
            "creates 2 elements, expected 3",
        ),
        (
            "enum E { A B } module R { clock clk reset rst out y:E "
            "rom t:rom<E,2>{read_latency 1 init generate(i in 0..2) E.A} "
            "t.read_address=0 y=t.read_data }",
            "recursively bit-packable and non-enum",
        ),
        (
            "module R { clock clk reset rst out y:u8 "
            "rom t:rom<u8,2>{read_latency 1 init generate(i in 0..2) i} "
            "y=t.read_data }",
            "has no 'read_address' assignment",
        ),
        (
            "module R { clock clk reset rst out y:u8 "
            "rom t:rom<u8,2>{read_latency 1 init generate(i in 0..2) i} "
            "t.write_data=0 t.read_address=0 y=t.read_data }",
            "has no field 'write_data'",
        ),
    ),
)
def test_invalid_rom_semantics_fail_closed(source: str, message: str) -> None:
    with pytest.raises(SemanticError, match=message):
        _compile(source)
