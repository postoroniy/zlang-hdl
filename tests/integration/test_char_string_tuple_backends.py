from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from dataclasses import replace

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang import compile_source
from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.clash.public_wrapper import (
    ClashPublicTopWrapper,
    bind_artifact_to_public_wrapper,
)
from zlang.backend.systemverilog import emit_artifact as emit_systemverilog_artifact
from zlang.backend.manifest import BackendArtifact
from zlang.backend.manifest import publish_artifact
from zlang.cross_backend import run_cross_backend_formal
from zlang.equivalence import (
    artifact_hash,
    emit_miter,
    emit_reference_model,
    formal_tools_available,
    make_equivalence_property,
    publish_bindings,
    run_equivalence_formal,
)
from zlang.ir.cross_backend import (
    CrossBackendMode,
    CrossBackendProperty,
    CrossBackendRelation,
    CrossBackendStatus,
)
from zlang.ir.equivalence import (
    BindingMap,
    BindingSide,
    EquivalenceMode,
    EquivalenceStatus,
)
from zlang.simulate import simulate
from zlang.toolchain import generate_verilog, lint_with_verilator


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


def test_text_tuple_witness_uses_native_clash_and_exact_direct_sv_packing() -> None:
    compilation = compile_source(SOURCE)
    module = compilation.ir
    direct = emit_systemverilog_artifact(module)
    repeated_direct = emit_systemverilog_artifact(module)
    clash_wrapper = ClashPublicTopWrapper.build(module)

    assert direct.text == repeated_direct.text
    assert direct.artifact_hash == repeated_direct.artifact_hash
    assert simulate(
        module,
        pair=(0x41, [0x42, 0x43]),
        raw_in=0x58595A,
        signed_pair=(-5, 7),
    ) == {
        "first": 0x41,
        "text": [0x42, 0x43],
        "literal": (0x5A, [0x4F, 0x4B]),
        "same_text": 0,
        "same_pair": 1,
        "different_pair": 0,
        "raw_pair": 0x414243,
        "restored": (0x58, [0x59, 0x5A]),
        "signed_sum": 2,
    }

    assert "topEntity :: (Unsigned 8, Vec 2 (Unsigned 8))" in compilation.clash
    assert (
        'PortProduct "pair" [PortName "_item0", PortName "_item1"]'
        in compilation.clash
    )
    assert (
        "((90 :: Unsigned 8), (((79 :: Unsigned 8)) :> "
        "((75 :: Unsigned 8)) :> Nil))"
        in compilation.clash
    )

    assert "assign first = pair[23:16];" in direct.text
    assert "assign text = pair[15:0];" in direct.text
    assert "assign literal = {8'(8'd90), 16'({8'd79, 8'd75})};" in direct.text
    assert "$signed(signed_pair[15:8])" in direct.text
    assert "$signed(signed_pair[7:0])" in direct.text
    assert "assign raw_pair = 24'($unsigned(pair));" in direct.text
    assert ".pair({pair__item0, pair__item1[0], pair__item1[1]})" in direct.text

    assert ".pair__item0(pair__item0)" in clash_wrapper.text
    assert ".pair__item1({pair__item1[0], pair__item1[1]})" in clash_wrapper.text
    assert "assign literal__item0 = zlang_top_core_literal__item0[7:0];" in clash_wrapper.text


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
    module = compile_source(SOURCE, include_clash=False).ir
    rtl = tmp_path / "TextTupleBackend.sv"
    rtl.write_text(emit_systemverilog_artifact(module).text)
    _run_text_tuple_testbench(tmp_path, (rtl,), suffix="direct")


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and strict Verilator are required",
)
def test_text_tuple_clash_is_bit_exact(tmp_path: Path) -> None:
    compilation = compile_source(SOURCE)
    wrapper = ClashPublicTopWrapper.build(compilation.ir)
    rtl = generate_verilog(
        compilation.clash,
        compilation.ir.name,
        tmp_path / "text_tuple_clash_rtl",
        CLASH_EXECUTABLE,
        public_wrapper=wrapper,
    )
    _run_text_tuple_testbench(tmp_path, tuple(rtl), suffix="clash")


def test_text_tuple_artifacts_publish_only_real_public_top_bindings() -> None:
    module = compile_source(SOURCE).ir
    direct = emit_systemverilog_artifact(module)
    wrapper = ClashPublicTopWrapper.build(module)
    clash = bind_artifact_to_public_wrapper(
        emit_clash_artifact(module), wrapper
    )

    for artifact in (direct, clash):
        bindings = {
            binding.semantic_signal_id: binding
            for binding in artifact.bindings
        }
        for semantic_id, rtl_path in EXPECTED_PUBLIC_BINDINGS.items():
            binding = bindings[semantic_id]
            assert binding.rtl_module == module.name
            assert binding.rtl_path == rtl_path
            assert binding.physical_available
        assert not bindings["port:pair"].physical_available
        assert not bindings["port:literal"].physical_available
        assert not bindings["port:restored"].physical_available
        assert not bindings["port:signed_pair"].physical_available
        restored = BackendArtifact.from_json(artifact.to_json())
        assert restored.to_json() == artifact.to_json()


def test_tuple_output_leaf_bindings_preserve_component_source_origins() -> None:
    module = compile_source(
        ORIGIN_SOURCE,
        source_unit="tuple-origin.zl",
    ).ir
    direct = emit_systemverilog_artifact(module)
    wrapper = ClashPublicTopWrapper.build(module)
    clash = bind_artifact_to_public_wrapper(
        emit_clash_artifact(module), wrapper
    )

    for artifact in (direct, clash):
        bindings = {
            binding.semantic_signal_id: binding
            for binding in artifact.bindings
        }
        first = bindings["port:q.item0"].source_origin
        second = bindings["port:q.item1"].source_origin
        assert first is not None and second is not None
        assert first.source_unit == second.source_unit == "tuple-origin.zl"
        assert first.span.start_line == 5
        assert second.span.start_line == 6
        assert first != second
        restored = BackendArtifact.from_json(artifact.to_json())
        restored_bindings = {
            binding.semantic_signal_id: binding
            for binding in restored.bindings
        }
        assert restored_bindings["port:q.item0"].source_origin == first
        assert restored_bindings["port:q.item1"].source_origin == second


def test_raw_clash_tuple_leaf_bindings_are_published_only_when_physical() -> None:
    scalar_module = compile_source(COLLISION_SOURCE).ir
    scalar = emit_clash_artifact(scalar_module)
    scalar_bindings = {
        binding.semantic_signal_id: binding for binding in scalar.bindings
    }
    assert scalar_bindings["port:p.item0"].rtl_path == "p__item0"
    assert scalar_bindings["port:p.item0"].physical_available
    assert scalar_bindings["port:p_item0"].rtl_path == "p_item0"
    scalar_wrapper = ClashPublicTopWrapper.build(scalar_module)
    assert ".p__item0(p__item0)" in scalar_wrapper.text
    assert ".p_item0(p_item0)" in scalar_wrapper.text

    vector_module = compile_source(VECTOR_TUPLE_SOURCE).ir
    raw = emit_clash_artifact(vector_module)
    raw_bindings = {
        binding.semantic_signal_id: binding for binding in raw.bindings
    }
    for semantic_id in (
        "port:p.item0", "port:p.item1", "port:q.item0", "port:q.item1"
    ):
        assert raw_bindings[semantic_id].rtl_path == ""
        assert not raw_bindings[semantic_id].physical_available

    rebound = bind_artifact_to_public_wrapper(
        raw, ClashPublicTopWrapper.build(vector_module)
    )
    rebound_bindings = {
        binding.semantic_signal_id: binding for binding in rebound.bindings
    }
    assert rebound_bindings["port:p.item0"].rtl_path == "p__item0"
    assert rebound_bindings["port:q.item1"].rtl_path == "q__item1"
    assert all(
        rebound_bindings[semantic_id].physical_available
        for semantic_id in (
            "port:p.item0", "port:p.item1", "port:q.item0", "port:q.item1"
        )
    )


def test_clash_aggregate_tuple_vectors_use_typed_soa_aos_bridges() -> None:
    vector = compile_source(AGGREGATE_VECTOR_SOURCE).clash
    assert "zip (" in vector
    assert "zlangPayloadElement0" in vector
    assert "map (\\zlangPayloadItem" in vector

    parent = compile_source(AGGREGATE_PARENT_SOURCE).clash
    assert "circuit parent_p" in parent
    assert "<$> parent_p" in parent


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
        module = compile_source(source, include_clash=False).ir
        rtl = tmp_path / f"{module.name}.sv"
        rtl.write_text(emit_systemverilog_artifact(module).text)
        lint_with_verilator((rtl,), module.name)


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and strict Verilator are required",
)
def test_text_tuple_clash_public_top_is_strict_verilator_clean(
    tmp_path: Path,
) -> None:
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
        compilation = compile_source(source)
        wrapper = ClashPublicTopWrapper.build(compilation.ir)
        companions = emit_clash_artifact(compilation.ir).companions
        rtl = generate_verilog(
            compilation.clash,
            compilation.ir.name,
            tmp_path / compilation.ir.name / "rtl",
            CLASH_EXECUTABLE,
            companions=companions,
            public_wrapper=wrapper,
        )
        if source == TUPLE_ROM_SOURCE:
            # Clash 1.11's upstream romFile selector uses a host-width Enum
            # index.  Keep the established, ROM-specific WIDTHTRUNC waiver;
            # every other warning remains fatal.
            verilator = shutil.which("verilator")
            assert verilator is not None
            completed = subprocess.run(
                (
                    verilator,
                    "--lint-only",
                    "-Wno-WIDTHTRUNC",
                    "--top-module",
                    compilation.ir.name,
                    *(str(path) for path in rtl),
                ),
                text=True,
                capture_output=True,
            )
            assert completed.returncode == 0, completed.stderr or completed.stdout
        else:
            lint_with_verilator(rtl, compilation.ir.name)


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
            compile_source(source, include_clash=False).ir
        ).text
    )
    _verilate_tuple_stage(tmp_path, (rtl,), top, f"direct_{top}")


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or shutil.which("verilator") is None,
    reason="real Clash 1.11 and strict Verilator are required",
)
@pytest.mark.parametrize(
    ("source", "top"),
    (
        (TUPLE_DELAY_SOURCE, "TupleDelay"),
        (TUPLE_PIPELINE_SOURCE, "TuplePipeline"),
    ),
)
def test_tuple_stage_clash_is_cycle_exact(
    tmp_path: Path, source: str, top: str
) -> None:
    compilation = compile_source(source)
    wrapper = ClashPublicTopWrapper.build(compilation.ir)
    rtl = generate_verilog(
        compilation.clash,
        top,
        tmp_path / f"{top}_clash_rtl",
        CLASH_EXECUTABLE,
        public_wrapper=wrapper,
    )
    _verilate_tuple_stage(tmp_path, tuple(rtl), top, f"clash_{top}")


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or len(formal_tools_available()) != 3,
    reason="real Clash and Yosys/SymbiYosys formal tools are required",
)
@pytest.mark.parametrize(
    ("source", "suffix"),
    (
        (FORMAL_SOURCE, "projection"),
        (PACKING_FORMAL_SOURCE, "packing"),
    ),
)
def test_tuple_scalar_results_are_visible_to_existing_m36_and_m38_routes(
    source: str,
    suffix: str,
) -> None:
    compilation = compile_source(source)
    module = compilation.ir
    expression = module.assignments[0].expression
    identity = f"selected:tuple-{suffix}-smoke"

    direct = emit_systemverilog_artifact(
        module, selected_ir_identity=identity
    )
    reference = emit_reference_model(
        f"Tuple{suffix.title()}Reference",
        "y",
        expression.type,
        tuple((port.name, port.type) for port in module.inputs),
        expression,
    )
    property_ = make_equivalence_property(
        expression,
        expression,
        candidate_class="value",
        reference_root=f"tuple-{suffix}-reference",
        implementation_root="direct-systemverilog",
        inputs=("port:a", "port:b"),
        reference_output="port:y",
        implementation_output="port:y",
    )
    rtl_names = {"port:a": "a", "port:b": "b", "port:y": "y"}
    bindings = BindingMap((
        *publish_bindings(
            module,
            side=BindingSide.REFERENCE,
            selected_ir_identity=identity,
            backend="semantic_reference",
            artifact_hash_value=artifact_hash(reference),
            rtl_names=rtl_names,
        ),
        *publish_bindings(
            module,
            side=BindingSide.IMPLEMENTATION,
            selected_ir_identity=identity,
            backend="direct_systemverilog",
            artifact_hash_value=artifact_hash(direct.text),
            rtl_names=rtl_names,
        ),
    ))
    miter = emit_miter(
        property_,
        bindings,
        reference_module=f"Tuple{suffix.title()}Reference",
        implementation_module=module.name,
    )
    m36 = run_equivalence_formal(
        property_,
        reference + "\n" + direct.text + "\n" + miter,
        top="m36_" + property_.id.replace(".", "_"),
        mode=EquivalenceMode.BMC,
        depth=2,
    )
    assert m36.status is EquivalenceStatus.BOUNDED_PASS

    with tempfile.TemporaryDirectory() as temporary:
        files = generate_verilog(
            compilation.clash,
            module.name,
            Path(temporary) / "clash_rtl",
            CLASH_EXECUTABLE,
        )
        clash_rtl = "\n".join(path.read_text() for path in files)
    clash = publish_artifact(
        module,
        clash_rtl,
        backend="clash",
        selected_ir_identity=identity,
    )
    direct_m38 = emit_systemverilog_artifact(
        replace(module, name=f"Tuple{suffix.title()}FormalSv"),
        selected_ir_identity=identity,
    )
    cross_property = CrossBackendProperty(
        f"m38.tuple_{suffix}.real",
        CrossBackendRelation.SAME_CYCLE_VALUE,
        identity,
        ("port:y",),
        None,
        None,
        0,
        0,
    )
    m38 = run_cross_backend_formal(
        cross_property,
        clash,
        direct_m38,
        inputs=("port:a", "port:b"),
        mode=CrossBackendMode.BMC,
        depth=2,
    )
    assert m38.status is CrossBackendStatus.BOUNDED_PASS
