from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from dataclasses import replace

import pytest

from zlang import compile_source
from zlang.backend.systemverilog import emit_artifact as emit_systemverilog_artifact
from zlang.backend.manifest import BackendArtifact
from zlang.backend.manifest import publish_artifact
from zlang.equivalence import (
    artifact_hash,
    emit_miter,
    emit_reference_model,
    formal_tools_available,
    make_equivalence_property,
    publish_bindings,
    run_equivalence_formal,
)
from zlang.ir.equivalence import (
    BindingMap,
    BindingSide,
    EquivalenceMode,
    EquivalenceStatus,
)
from zlang.simulate import simulate
from zlang.toolchain import lint_with_verilator


SOURCE = """
module TextTupleBackend {
    in pair : (char,string<2>)
    out first : char
    out text : string<2>
    out literal : (char,string<2>)
    out same_text : bit
    out same_pair : bit
    out different_pair : bit
    out raw_pair : bits<24>
    in raw_in : bits<24>
    out restored : (char,string<2>)
    in signed_pair : (s8,s8)
    out signed_sum : s9

    first = pair[0]
    text = pair[1]
    literal = ('Z',"OK")
    same_text = pair[1] == "OK"
    same_pair = pair == ('A',"BC")
    different_pair = pair != ('A',"BC")
    raw_pair = pack(pair)
    restored = unpack<(char,string<2>)>(raw_in)
    signed_sum = signed_pair[0] + signed_pair[1]
}
"""


MEMORY_SOURCE = """
module TextMemoryBackend {
    clock clk reset rst
    in read_address : u1
    in write_enable : bit
    in write_address : u1
    in write_data : string<2>
    out read_data : string<2>

    memory table : mem<string<2>,2> {
        read_latency 1
        collision read_first
    }
    table.read_address = read_address
    table.write_enable = write_enable
    table.write_address = write_address
    table.write_data = write_data
    read_data = table.read_data
}
"""


TUPLE_MEMORY_SOURCE = """
module TupleMemoryBackend {
    clock clk reset rst
    in read_address : u1
    in write_enable : bit
    in write_address : u1
    in write_data : (u8,bit)
    out read_data : (u8,bit)

    memory table : mem<(u8,bit),2> {
        read_latency 1
        collision read_first
    }
    table.read_address = read_address
    table.write_enable = write_enable
    table.write_address = write_address
    table.write_data = write_data
    read_data = table.read_data
}
"""


TUPLE_DELAY_SOURCE = """
module TupleDelay {
    clock clk reset rst
    in x : (u8,bit)
    out y : (u8,bit)
    y = delay<2>(x)
}
"""


TUPLE_PIPELINE_SOURCE = """
module TuplePipeline {
    clock clk reset rst
    in x : (u8,bit)
    out y : (u8,bit)
    y = pipeline(2) { x }
}
"""


TUPLE_ROM_SOURCE = """
module TupleRomBackend {
    clock clk reset rst
    in address : u1
    out data : (u8,bit)
    rom table : rom<(u8,bit),2> {
        read_latency 1
        init [(0x12,0),(0xa5,1)]
    }
    table.read_address = address
    data = table.read_data
}
"""


TUPLE_REQUEST_RESPONSE_SOURCE = """
module TupleRequestResponseBackend {
    clock clk reset rst
    interface mem : request_response<(u8,bit),(bit,u8)> {
        max_outstanding 1
        ordering in_order
    }
    in request : (u8,bit)
    in issue : bit
    in consume : bit
    out response : (bit,u8)
    mem.request.payload = request
    mem.request.valid = issue
    mem.response.ready = consume
    response = mem.response.payload
}
"""


FORMAL_SOURCE = """
module TupleProjectionFormal {
    in a : u8
    in b : bit
    out y : u8
    pair : (u8,bit) = (a,b)
    y = pair[0]
}
"""


PACKING_FORMAL_SOURCE = """
module TuplePackingFormal {
    in a : u8
    in b : bit
    out y : bits<9>
    y = pack((a,b))
}
"""


VECTOR_TUPLE_SOURCE = """
module TupleVectorBindings {
    in p : vec<2,(u8,bit)>
    out q : vec<2,(u8,bit)>
    q = p
}
"""


COLLISION_SOURCE = """
module TupleCoreNames {
    in p : (u8,bit)
    in p_item0 : u8
    out q : (u8,bit)
    q = p
}
"""


AGGREGATE_VECTOR_SOURCE = """
struct TupleVectorLane { data:u8 last:bit }
protocol TupleVectorBus {
    role source
    role sink
    channel data:rv<vec<2,(u4,TupleVectorLane)>> source -> sink
}
module AggregateTupleVector {
    clock clk reset rst
    interface incoming:TupleVectorBus.sink
    interface outgoing:TupleVectorBus.source
    outgoing.data.payload = incoming.data.payload
    outgoing.data.valid = incoming.data.valid
    incoming.data.ready = outgoing.data.ready
}
"""


AGGREGATE_PARENT_SOURCE = """
protocol TupleBus {
    role initiator
    role target
    channel req:rv<(u8,bit)> initiator -> target
}
module AggregateTupleParent {
    clock clk reset rst
    interface bus:TupleBus.initiator
    in p:(u8,bit)
    bus.req.payload=p
    bus.req.valid=1
}
"""


ORIGIN_SOURCE = """module TupleOrigin {
    in p : (u8,bit)
    out q : (bit,u8)
    q = (
        p[1],
        p[0]
    )
}
"""


EXPECTED_PUBLIC_BINDINGS = {
    "port:pair.item0": "pair__item0",
    "port:pair.item1": "pair__item1",
    "port:first": "first",
    "port:text": "text",
    "port:literal.item0": "literal__item0",
    "port:literal.item1": "literal__item1",
    "port:same_text": "same_text",
    "port:same_pair": "same_pair",
    "port:different_pair": "different_pair",
    "port:raw_pair": "raw_pair",
    "port:raw_in": "raw_in",
    "port:restored.item0": "restored__item0",
    "port:restored.item1": "restored__item1",
    "port:signed_pair.item0": "signed_pair__item0",
    "port:signed_pair.item1": "signed_pair__item1",
    "port:signed_sum": "signed_sum",
}




def _run_text_tuple_testbench(
    tmp_path: Path,
    rtl: tuple[Path, ...],
    *,
    suffix: str,
) -> None:
    bench = tmp_path / f"text_tuple_{suffix}.sv"
    bench.write_text(
        """
`default_nettype none
module text_tuple_tb;
  logic [7:0] pair__item0;
  logic [7:0] pair__item1 [0:1];
  logic [7:0] first;
  logic [7:0] text [0:1];
  logic [7:0] literal__item0;
  logic [7:0] literal__item1 [0:1];
  logic same_text;
  logic same_pair;
  logic different_pair;
  logic [23:0] raw_pair;
  logic [23:0] raw_in;
  logic [7:0] restored__item0;
  logic [7:0] restored__item1 [0:1];
  logic signed [7:0] signed_pair__item0;
  logic signed [7:0] signed_pair__item1;
  logic signed [8:0] signed_sum;

  TextTupleBackend dut (
    .pair__item0(pair__item0),
    .pair__item1(pair__item1),
    .first(first),
    .text(text),
    .literal__item0(literal__item0),
    .literal__item1(literal__item1),
    .same_text(same_text),
    .same_pair(same_pair),
    .different_pair(different_pair),
    .raw_pair(raw_pair),
    .raw_in(raw_in),
    .restored__item0(restored__item0),
    .restored__item1(restored__item1),
    .signed_pair__item0(signed_pair__item0),
    .signed_pair__item1(signed_pair__item1),
    .signed_sum(signed_sum)
  );

  initial begin
    pair__item0 = 8'h41;
    pair__item1[0] = 8'h42;
    pair__item1[1] = 8'h43;
    raw_in = 24'h58595a;
    signed_pair__item0 = -8'sd5;
    signed_pair__item1 = 8'sd7;
    #1;
    if (first !== 8'h41 || text[0] !== 8'h42 || text[1] !== 8'h43)
      $fatal(1, "tuple projection/string output mismatch");
    if (literal__item0 !== 8'h5a || literal__item1[0] !== 8'h4f ||
        literal__item1[1] !== 8'h4b)
      $fatal(1, "tuple literal mismatch");
    if (same_text !== 1'b0 || same_pair !== 1'b1 ||
        different_pair !== 1'b0 || raw_pair !== 24'h414243)
      $fatal(1, "tuple equality/packing mismatch");
    if (restored__item0 !== 8'h58 || restored__item1[0] !== 8'h59 ||
        restored__item1[1] !== 8'h5a)
      $fatal(1, "tuple unpack mismatch");
    if (signed_sum !== 9'sd2)
      $fatal(1, "signed tuple projection mismatch");

    pair__item0 = 8'h5a;
    pair__item1[0] = 8'h4f;
    pair__item1[1] = 8'h4b;
    #1;
    if (same_text !== 1'b1 || same_pair !== 1'b0 ||
        different_pair !== 1'b1 || raw_pair !== 24'h5a4f4b)
      $fatal(1, "second tuple vector mismatch");
    $finish;
  end
endmodule
`default_nettype wire
"""
    )
    object_dir = tmp_path / f"obj_text_tuple_{suffix}"
    completed = subprocess.run(
        (
            "verilator",
            "--binary",
            "--timing",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            "--Mdir",
            str(object_dir),
            "--top-module",
            "text_tuple_tb",
            *(str(path) for path in rtl),
            str(bench),
        ),
        cwd=tmp_path,
        env={**os.environ, "CCACHE_DISABLE": "1"},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(object_dir / "Vtext_tuple_tb"),),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(
    shutil.which("verilator") is None,
    reason="strict Verilator is unavailable",
)
def test_text_tuple_direct_sv_is_bit_exact(tmp_path: Path) -> None:
    module = compile_source(SOURCE).ir
    rtl = tmp_path / "TextTupleBackend.sv"
    rtl.write_text(emit_systemverilog_artifact(module).text)
    _run_text_tuple_testbench(tmp_path, (rtl,), suffix="direct")












@pytest.mark.skipif(
    shutil.which("verilator") is None,
    reason="strict Verilator is unavailable",
)
def test_text_tuple_direct_sv_is_strict_verilator_clean(tmp_path: Path) -> None:
    for source in (
        SOURCE,
        MEMORY_SOURCE,
        TUPLE_MEMORY_SOURCE,
        COLLISION_SOURCE,
        AGGREGATE_VECTOR_SOURCE,
        AGGREGATE_PARENT_SOURCE,
        TUPLE_DELAY_SOURCE,
        TUPLE_PIPELINE_SOURCE,
        TUPLE_ROM_SOURCE,
        TUPLE_REQUEST_RESPONSE_SOURCE,
    ):
        module = compile_source(source).ir
        rtl = tmp_path / f"{module.name}.sv"
        rtl.write_text(emit_systemverilog_artifact(module).text)
        lint_with_verilator((rtl,), module.name)




def _verilate_tuple_stage(
    tmp_path: Path,
    rtl: tuple[Path, ...],
    top: str,
    suffix: str,
) -> None:
    harness = tmp_path / f"tuple_stage_{suffix}.cpp"
    harness.write_text(f'''\
#include "V{top}.h"
#include "verilated.h"

static void edge(V{top}& dut, unsigned value, unsigned flag, bool reset) {{
  dut.clk = 0;
  dut.rst = reset;
  dut.x___05Fitem0 = value;
  dut.x___05Fitem1 = flag;
  dut.eval();
  dut.clk = 1;
  dut.eval();
}}

int main(int argc, char **argv) {{
  Verilated::commandArgs(argc, argv);
  V{top} dut;
  edge(dut, 1, 0, true);
  if (dut.y___05Fitem0 != 0 || dut.y___05Fitem1 != 0) return 1;
  edge(dut, 2, 1, false);
  if (dut.y___05Fitem0 != 0 || dut.y___05Fitem1 != 0) return 2;
  edge(dut, 3, 0, false);
  if (dut.y___05Fitem0 != 2 || dut.y___05Fitem1 != 1) return 3;
  edge(dut, 4, 1, false);
  if (dut.y___05Fitem0 != 3 || dut.y___05Fitem1 != 0) return 4;
  edge(dut, 5, 0, true);
  if (dut.y___05Fitem0 != 0 || dut.y___05Fitem1 != 0) return 5;
  return 0;
}}
''')
    object_dir = tmp_path / f"obj_{suffix}"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(object_dir), "--top-module", top,
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(object_dir / f"V{top}"),), capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(
    shutil.which("verilator") is None,
    reason="strict Verilator is unavailable",
)
@pytest.mark.parametrize(
    ("source", "top"),
    (
        (TUPLE_DELAY_SOURCE, "TupleDelay"),
        (TUPLE_PIPELINE_SOURCE, "TuplePipeline"),
    ),
)
def test_tuple_stage_direct_sv_is_cycle_exact(
    tmp_path: Path, source: str, top: str
) -> None:
    rtl = tmp_path / f"{top}.sv"
    rtl.write_text(
        emit_systemverilog_artifact(
            compile_source(source).ir
        ).text
    )
    _verilate_tuple_stage(tmp_path, (rtl,), top, f"direct_{top}")
