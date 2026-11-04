# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Storage controls resolve every path segment, without changing payload types."""

from hashlib import sha256

import pytest

from tests.semantic.test_initialized_rom import SCALAR_ROM
from tests.semantic.test_storage import FIFO_SOURCE, MEMORY_SOURCE
from zlang.compiler import compile_source
from zlang.ir.types import SIntType, StructType, UIntType
from zlang.semantic import SemanticError
from zlang.source import SourceSpan


PORTED_MEMORY = """module Ported {
  clock clk reset rst
  in a:u2 in data:u8 in enable:bit out y:u8
  memory table:mem<u8,4>{
    read_port fetch write_port store read_latency 1 collision old
  }
  table.fetch.address=a
  table.store.address=a
  table.store.enable=enable
  table.store.data=data
  y=table.fetch.data
}
"""


@pytest.mark.parametrize("field", ("data", "push", "pop"))
@pytest.mark.parametrize("middle", ("ghost", "front", "data"))
def test_fifo_rejects_ignored_middle_target_segment(field, middle):
    target = f"queue.{middle}.{field}"
    text = FIFO_SOURCE.replace(f"queue.{field} =", f"{target} =")
    with pytest.raises(SemanticError) as raised:
        compile_source(text, source_unit="fifo-target.zhl")
    error = raised.value
    assert str(error) == f"FIFO control '{target}' selects one field"
    assert error.code == "ZL-SEMANTIC-001"
    line = next(i for i, value in enumerate(text.splitlines(), 1) if target in value)
    column = text.splitlines()[line - 1].index("queue") + 1
    assert error.primary.span == SourceSpan(line, column, line, column + len("queue"))
    assert error.primary.source_unit == "fifo-target.zhl"
    assert error.primary.digest == sha256(text.encode()).hexdigest()


@pytest.mark.parametrize("middle", ("ghost", "read_data", "read_address"))
def test_rom_rejects_ignored_middle_target_segment(middle):
    target = f"table.{middle}.read_address"
    text = SCALAR_ROM.replace("table.read_address =", f"{target} =")
    with pytest.raises(SemanticError) as raised:
        compile_source(text)
    assert str(raised.value) == f"ROM control '{target}' selects one field"


@pytest.mark.parametrize("source,valid,bad", (
    (FIFO_SOURCE, "queue.data = data", "queue.ghost.data = missing"),
    (SCALAR_ROM, "table.read_address = address", "table.ghost.read_address = missing"),
), ids=("fifo", "rom"))
def test_target_shape_is_checked_before_operand_typing(source, valid, bad):
    with pytest.raises(SemanticError, match="selects one field"):
        compile_source(source.replace(valid, bad))


@pytest.mark.parametrize("prefix,payload,expected", (
    ("", "u8", UIntType),
    ("", "s8", SIntType),
    ("struct Pair { left:u4 right:u4 }\n", "Pair", StructType),
))
def test_direct_fifo_controls_preserve_exact_payload_type(prefix, payload, expected):
    text = prefix + FIFO_SOURCE.replace("data: u8", f"data: {payload}").replace(
        "front: u8", f"front: {payload}"
    ).replace("fifo<u8, 4>", f"fifo<{payload}, 4>")
    fifo, = compile_source(text).ir.fifos
    assert isinstance(fifo.element_type, expected)
    assert fifo.data.type == fifo.element_type
    assert fifo.data.name == "data"
    assert fifo.push.name == "push"
    assert fifo.pop.name == "pop"


@pytest.mark.parametrize("prefix,payload,value,expected", (
    ("", "u8", "7", UIntType),
    ("", "s8", "-3", SIntType),
    ("struct Pair { left:u4 right:u4 }\n", "Pair", "Pair{left=3 right=5}", StructType),
))
def test_direct_rom_control_preserves_exact_payload_type(prefix, payload, value, expected):
    text = prefix + f"""module ReadOnly {{
      clock clk reset rst in a:u1 out y:{payload}
      rom table:rom<{payload},2>{{read_latency 1 init generate(i in 0..2) {value}}}
      table.read_address=a
      y=table.read_data
    }}"""
    rom, = compile_source(text).ir.roms
    assert isinstance(rom.element_type, expected)
    assert rom.read_address.name == "a"
    assert rom.contents[0].type == rom.element_type


def test_enum_storage_boundaries_are_not_broadened_by_target_validation():
    declaration = "enum Mode:bits<2>{Idle=0 Busy=1}\n"
    with pytest.raises(SemanticError, match="top-level input 'data' cannot expose enum"):
        compile_source(declaration + FIFO_SOURCE.replace("data: u8", "data: Mode"))
    with pytest.raises(SemanticError, match="not recursively bit-packable and non-enum"):
        compile_source(declaration + """module EnumRom {
          clock clk reset rst out y:bits<2>
          rom table:rom<Mode,2>{read_latency 1 init [Mode.Idle,Mode.Busy]}
          table.read_address=0
          y=enum_encode(table.read_data)
        }""")


def test_declared_memory_ports_still_resolve_three_segments():
    memory, = compile_source(PORTED_MEMORY).ir.memories
    fetch, store = memory.ports
    assert (fetch.name, store.name) == ("fetch", "store")
    assert fetch.address.name == store.address.name == "a"
    assert store.write_data.name == "data"
    assert store.write_enable.name == "enable"


@pytest.mark.parametrize("source,before,after,message", (
    (FIFO_SOURCE, "queue.push = push", "queue.data = data", "assigned more than once"),
    (FIFO_SOURCE, "queue.push = push", "queue.full = push", "is read-only"),
    (FIFO_SOURCE, "queue.push = push", "queue.absent = push", "has no field 'absent'"),
    (FIFO_SOURCE, "queue.push = push", "queue = push", "has invalid depth"),
    (FIFO_SOURCE, "queue.push = push", "queue.extra.path.push = push", "has invalid depth"),
    (SCALAR_ROM, "table.read_address = address", "table.read_data = address", "is read-only"),
    (SCALAR_ROM, "table.read_address = address", "table.absent = address", "has no field 'absent'"),
    (SCALAR_ROM, "table.read_address = address", "table.extra.path.read_address = address", "has invalid depth"),
    (MEMORY_SOURCE, "table.read_address =", "table.ghost.read_address =", "legacy memory control"),
    (PORTED_MEMORY, "table.fetch.address=", "table.ghost.address=", "has no port 'ghost'"),
    (PORTED_MEMORY, "table.fetch.address=", "table.address=", "must select a named port and field"),
    (PORTED_MEMORY, "table.fetch.address=", "table.fetch.enable=", "has no writable field 'enable'"),
), ids=(
    "fifo-duplicate", "fifo-read-only", "fifo-unknown", "fifo-short", "fifo-long",
    "rom-read-only", "rom-unknown", "rom-long", "legacy-memory-depth",
    "ported-memory-unknown", "ported-memory-short", "ported-memory-field",
))
def test_existing_target_rejections_keep_their_precedence(source, before, after, message):
    with pytest.raises(SemanticError) as raised:
        compile_source(source.replace(before, after))
    assert message in str(raised.value)
