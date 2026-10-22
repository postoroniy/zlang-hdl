"""Storage construction consumes existing typed immutable-local products."""

import pytest

from zlang.compiler import compile_source
from zlang.backend.systemverilog import emit_artifact
from zlang.semantic import SemanticError
from zlang.semantic import analyze
from zlang.parser import parse
from zlang.compilation_selection import inline_locals


ROM = """module Top { clock clk reset rst in a:u2 out y:u8
 rom table:rom<u8,4>{read_latency 1 init generate(i in 0..4) i}
 address=a
 table.read_address=address
 y=table.read_data
}
"""

MEMORY = """module Top { clock clk reset rst
 in a:u2 in d:u8 in write:bit out y:u8
 memory table:mem<u8,4>{read_latency 1 collision read_first}
 address=a data=d enabled=write
 table.read_address=address table.write_address=address
 table.write_data=data table.write_enable=enabled y=table.read_data
}
"""

FIFO = """module Top { clock clk reset rst
 in d:u8 in push,pop:bit out y:u8
 fifo queue:fifo<u8,4>
 data=d accepted=push retired=pop
 queue.data=data queue.push=accepted queue.pop=retired y=queue.front
}
"""


@pytest.mark.parametrize("text", (ROM, MEMORY, FIFO))
def test_global_storage_controls_accept_typed_locals(text):
    semantic = analyze(parse(text))
    assert semantic.locals
    inlined = inline_locals(semantic)
    for before, after in zip((*semantic.fifos, *semantic.memories, *semantic.roms),
                             (*inlined.fifos, *inlined.memories, *inlined.roms), strict=True):
        assert before.source_origin == after.source_origin
        if hasattr(before, "semantic_id"):
            assert before.semantic_id == after.semantic_id
    result = compile_source(text)
    assert emit_artifact(result.ir).text


def test_rom_local_range_is_checked_not_only_its_type():
    text = ROM.replace("rom<u8,4>", "rom<u8,3>").replace("0..4", "0..3")
    with pytest.raises(SemanticError, match="static range.*not.*within"):
        compile_source(text)
    compile_source(text.replace("address=a", "address=a >> 1", 1))


def test_ported_memory_controls_accept_locals():
    text = """module Top { clock clk reset rst in a:u2 in d:u8 in we:bit out y:u8
 memory table:mem<u8,4>{read_port read write_port write read_latency 1 collision old}
 address=a value=d enabled=we
 table.read.address=address table.write.address=address
 table.write.enable=enabled table.write.data=value y=table.read.data
 }"""
    semantic = analyze(parse(text))
    selected = inline_locals(semantic)
    assert selected.memories[0].semantic_id == semantic.memories[0].semantic_id
    assert selected.memories[0].ports[0].address.name == "a"
    assert selected.memories[0].ports[1].write_data.name == "d"
    assert emit_artifact(compile_source(text).ir).text


def test_rom_local_does_not_weaken_exact_address_width():
    with pytest.raises(SemanticError, match="expected exact|has type|requires"):
        compile_source(ROM.replace("address=a", "address=extend<3>(a)", 1))


def test_storage_local_cdc_is_rejected():
    text = ROM.replace("clock clk reset rst", "clock clk clock other reset rst @clk reset ro @other")
    text = text.replace("in a:u2 out y:u8", "in a:u2 @clk out y:u8 @clk")
    text = text.replace("rom<u8,4>{", "rom<u8,4> @clk {")
    text = text.replace("address=a", "reg state:u2 @other=0\n address=state", 1)
    with pytest.raises(SemanticError, match="clock-domain mismatch"):
        compile_source(text)


def test_combinational_memory_feedback_through_local_is_rejected():
    text = MEMORY.replace("read_latency 1", "read_latency 0").replace("address=a", "address=truncate<2>(table.read_data)", 1)
    with pytest.raises(SemanticError, match="cyclic|cycle|feedback"):
        compile_source(text)
