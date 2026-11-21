from __future__ import annotations

from pathlib import Path
import re

import pytest

from zlang import sim
from zlang.backend.systemverilog import emit_artifact_with_source_map, emit_experimental
from zlang.compiler import compile_source
from zlang.simulation_compare import compare_program


def _global_memory(
    *,
    latency: int,
    collision: str = "read_first",
    contents: str | None = None,
    read_data: str | None = None,
    masked: bool = False,
    named_control: bool = False,
) -> str:
    reset = (
        ""
        if contents is None or read_data is None
        else (
            "reset { "
            f"contents {contents} read_data {read_data} "
            "}"
        )
    )
    mask_port = "in mask:bits<2>" if masked else ""
    mask_binding = "table.write_mask=mask" if masked else ""
    source = f"""
module MemoryProfile {{
  clock clk reset rst
  in address:u2 in write_enable:bit in data:u16 {mask_port}
  out q:u16
  memory table:mem<u16,4> {{
    read_latency {latency}
    collision {collision}
    {reset}
  }}
  table.read_address=address
  table.write_enable=write_enable
  table.write_address=address
  table.write_data=data
  {mask_binding}
  q=table.read_data
}}
"""
    if not named_control:
        return source
    # Keep the legacy/global fixture intact and add a legal named-memory child
    # within this same source scenario and compiler invocation.
    return source + """
module NamedReadState {
  clock clk reset rst
  in address:u2 in write_enable:bit in data:u16
  out named_a,named_b:u16
  memory named:mem<u16,4> {
    read_port a read_port b write_port writer
    read_latency 4 collision read_first
    reset { contents clear read_data preserve }
  }
  named.a.address=address named.b.address=address
  named.writer.address=address named.writer.enable=write_enable
  named.writer.data=data
  named_a=named.a.data named_b=named.b.data
}
module ReadStateControls {
  clock clk reset rst
  in address:u2 in write_enable:bit in data:u16
  out q,named_a,named_b:u16
  legacy:MemoryProfile {address=address write_enable=write_enable data=data}
  ports:NamedReadState {address=address write_enable=write_enable data=data}
  q=legacy.q named_a=ports.named_a named_b=ports.named_b
}
"""


def _emit(source: str) -> str:
    module = compile_source(source).ir
    first = emit_experimental(module)
    assert emit_experimental(module) == first
    return first


def test_default_synchronous_memory_keeps_the_legacy_reset_shape() -> None:
    text = _emit(_global_memory(latency=1))

    assert "  initial begin" not in text
    assert "  always_ff @(posedge clk) begin" in text
    assert "      zlang_table_read_data <= '0;" in text
    assert (
        "zlang_table_cells[zlang_memory_reset_index] <= '0;" in text
    )


def test_zero_latency_masked_write_first_has_one_combinational_read_path() -> None:
    text = _emit(
        _global_memory(
            latency=0,
            collision="write_first",
            contents="preserve",
            read_data="clear",
            masked=True,
        )
    )

    read_driver = next(
        line
        for line in text.splitlines()
        if line.startswith("  assign zlang_table_read_data =")
    )
    assert "rst ? '0" in read_driver
    assert "!rst && write_enable" in read_driver
    assert "zlang_table_write_merged" in read_driver
    assert "zlang_table_cells[address]" in read_driver
    assert text.count("assign zlang_table_read_data =") == 1
    assert "zlang_table_read_data <=" not in text
    assert "zlang_table_cells[zlang_memory_reset_index] = '0;" in text
    assert "zlang_table_cells[zlang_memory_reset_index] <= '0;" not in text
    assert "  always @(posedge clk) begin" in text


def test_registered_preserve_profile_initializes_once_and_holds_on_reset() -> None:
    text = _emit(
        _global_memory(
            latency=1,
            contents="preserve",
            read_data="preserve",
            named_control=True,
        )
    )

    for port in ("a", "b"):
        assert text.count(f"    named_{port}_read_data = '0;") == 1
        assert f"named_{port}_read_data <= '0;" not in text
        for stage in range(3):
            assert text.count(f"    named_{port}_read_stage_{stage} = '0;") == 1
            assert f"named_{port}_read_stage_{stage} <= '0;" not in text
    # Contents clearing is independent of preserved read-state initialization.
    assert "named_cells_a[named_reset_index] <= '0;" in text
    assert "named_cells_a[named_reset_index] = '0;" not in text
    assert "  always_ff @(posedge clk) begin" not in text
    assert "    zlang_table_read_data = '0;" in text
    assert "zlang_table_cells[zlang_memory_reset_index] = '0;" in text
    assert "zlang_table_read_data <= '0;" not in text
    assert "zlang_table_cells[zlang_memory_reset_index] <= '0;" not in text
    assert "Memory contents and read result hold across reset" in text
    assert "  always @(posedge clk) begin" in text


@pytest.mark.parametrize("named", [False, True], ids=["global", "named"])
def test_async_read_address_uses_a_later_ram(tmp_path: Path, named: bool) -> None:
    ports = "read_port r write_port w" if named else ""
    ra, rd, wa, we, wd = (
        ("r.address", "r.data", "w.address", "w.enable", "w.data") if named else
        ("read_address", "read_data", "write_address", "write_enable", "write_data")
    )
    policy = f"{ports} read_latency 0 collision read_first reset {{contents clear read_data clear}}"
    source = tmp_path / "table_lookup.zhl"
    source.write_text(f"""
module Top {{
    clock clk reset rst
    in word:u136 in slot:u2 in write:bit in data:u8 out q:u8
    memory values:mem<u8,4> {{ {policy} }}
    memory selectors:mem<u136,4> {{ {policy} }}
    values.{ra}=bitcast<u2>(selectors.{rd}[135:134])
    values.{wa}=slot values.{we}=write values.{wd}=data
    selectors.{ra}=0 selectors.{wa}=0 selectors.{we}=1 selectors.{wd}=word
    q=values.{rd}
}}
""")
    cycles = [(0, 0, 0, 0), (1, 1, 1, 53), (3, 3, 1, 166), (1, 0, 0, 0), (2, 0, 0, 0)]
    events = [
        {"reset": {"rst": i == 0}, "edges": ["clk"],
         "set": {"word": (selector << 134) | 3, "slot": slot, "write": write, "data": data}}
        for i, (selector, slot, write, data) in enumerate(cycles)
    ]
    result = compare_program(sim.compile(source), events, simulator="iverilog", timeout=15)
    assert result.native_trace == result.rtl_trace == tuple({"q": n} for n in (0, 53, 166, 53, 0))


@pytest.mark.parametrize("child", [False, True], ids=["standalone", "composed"])
def test_materialized_ram_read_preserves_pre_edge_capture(tmp_path: Path, child: bool) -> None:
    prefix = "module Pass { in x:u8 out y:u8=x }" if child else ""
    output = "p:Pass {x=sample} comb=p.y" if child else "comb=sample"
    source = tmp_path / "read_capture.zhl"
    source.write_text(f"""
{prefix}
module Top {{
    clock clk reset rst in address:u2 in write,sel:bit in data:u8
    out comb:u8 out reg captured:u8=0
    memory cells:mem<u8,4> {{read_latency 0 collision read_first reset {{contents clear read_data clear}}}}
    cells.read_address=address cells.write_address=address cells.write_enable=write cells.write_data=data
    sample=mux(sel,cells.read_data,0)
    captured<-sample {output}
}}
""")
    events = [
        {"reset": {"rst": i == 0}, "edges": ["clk"],
         "set": {"address": 1, "write": write, "sel": sel, "data": data}}
        for i, (write, sel, data) in enumerate([(0, 1, 0), (1, 1, 53), (1, 1, 166), (0, 0, 0)])
    ]
    result = compare_program(sim.compile(source, top="Top"), events, simulator="iverilog", timeout=15)
    assert result.native_trace == result.rtl_trace == tuple(
        {"comb": live, "captured": old} for live, old in [(0, 0), (53, 0), (166, 53), (0, 0)]
    )


def test_scheduled_memory_uses_the_same_independent_reset_policies() -> None:
    source = """
module ScheduledMemoryProfile {
  clock clk reset rst
  in address:u2 in read_enable:bit in write_enable:bit in data:u8
  out q:u8
  memory table:mem<u8,4> {
    read_latency 1
    collision write_first
    reset { contents preserve read_data clear }
  }
  fetch: when read_enable { table.read(address) }
  store: when write_enable { table.write(address,data) }
  q=table.read_data
}
"""
    text = _emit(source)

    assert "zlang_table_cells[zlang_table_reset_index] = '0;" in text
    assert "zlang_table_cells[zlang_table_reset_index] <= '0;" not in text
    assert "      zlang_table_read_data <= '0;" in text
    assert "  always @(posedge clk) begin" in text
    assert "if (zlang_table_write_fire)" in text
    assert "if (zlang_table_read_fire) begin" in text


@pytest.mark.parametrize("ported", (False, True), ids=("global-top", "ported-child"))
def test_ram_read_stages_do_not_alias_public_ports(tmp_path: Path, ported: bool) -> None:
    collision = "m_r_read_stage_0" if ported else "m_read_stage_0"
    latency = 3 if ported else 2
    ports = f"clock clk reset rst in a:u1 in wr:bit in {collision}:u8 out q:u8"
    if ported:
        implementation = f"""
            memory m:mem<u8,2> {{
                read_port r write_port w read_latency {latency} collision read_first
                reset {{ contents preserve read_data preserve }}
            }}
            m.r.address=a m.w.address=a m.w.enable=wr m.w.data={collision}
            q=m.r.read_data
        """
        source = f"""
            module Child {{ {ports} {implementation} }}
            module Top {{
                {ports}
                lane:Child {{ a=a wr=wr {collision}={collision} }}
                q=lane.q
            }}
        """
    else:
        source = f"""
            module Top {{
                {ports}
                memory m:mem<u8,2> {{ read_latency {latency} collision read_first }}
                m.read_address=a m.write_address=a m.write_enable=wr
                m.write_data={collision} q=m.read_data
            }}
        """
    design = tmp_path / "memory_collision.zhl"
    design.write_text(source)
    program = sim.compile(design, top="Top")
    artifact, source_map = emit_artifact_with_source_map(program.module)
    repeat, repeat_map = emit_artifact_with_source_map(sim.compile(design, top="Top").module)
    assert repeat.text == artifact.text
    assert repeat_map.to_json() == source_map.to_json()
    assert any(leaf.external_name == collision for leaf in program.module.top_physical_abi.inputs)
    assert f"input wire logic [7:0] {collision}" in artifact.text

    # Reset, edge, address, write enable and data. The scoreboard reads the
    # pre-write word and advances only on an edge.
    cycles = (
        (True, True, 0, 1, 0xAA),
        (False, True, 0, 1, 0x34),
        (False, True, 1, 1, 0x7A),
        (False, False, 1, 1, 0x99),
        (False, True, 0, 1, 0x56),
        (False, True, 1, 0, 0),
        (False, True, 0, 0, 0),
        (False, True, 1, 0, 0),
        (True, True, 0, 1, 0x99),
        (False, True, 0, 0, 0),
        (False, True, 1, 0, 0),
        (False, True, 0, 0, 0),
    )
    cells, stages = [0, 0], [0] * latency
    events, expected = [], []
    for reset, edge, address, write, data in cycles:
        if edge and not reset:
            stages = [cells[address], *stages[:-1]]
            if write:
                cells[address] = data
        elif edge and not ported:
            cells, stages = [0, 0], [0] * latency
        events.append({
            "reset": {"rst": reset},
            "set": {"a": address, "wr": write, collision: data},
            "edges": ["clk"] if edge else [],
        })
        expected.append({"q": stages[-1]})

    comparison = compare_program(
        program, events, simulator="iverilog", artifact_directory=tmp_path / "comparison",
        timeout=30, build_timeout=30,
    )
    assert comparison.native_trace == tuple(expected)
    assert comparison.rtl_trace == tuple(expected)

    stages_text = re.findall(r"^  logic \[7:0\] (m_(?:r_)?read_stage_\w+);$", artifact.text, re.M)
    assert len(stages_text) == latency - 1
    assert collision not in stages_text
    normal = _emit(source.replace(collision, "user_data"))
    assert f"  logic [7:0] {collision};" in normal
