"""Named CSR hierarchy projections remain bounded and ABI-compatible."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from zlang import sim
from zlang.backend.systemverilog import emit_artifact, emit_experimental
from zlang.compiler import compile_file, compile_source
from zlang.ir import expressions as expr
from zlang.opt import lower, restore
from zlang.semantic import SemanticError
from zlang.simulation_compare import compare_program


VERILATOR = shutil.which("verilator")


def _bank_source(*, registers: int, generic: bool = False) -> str:
    parameter = "<BASE=0>" if generic else ""
    base = "BASE" if generic else "0"
    declarations = "\n".join(
        f"R{index} @{index * 4} {{ value bit @0 rw = 0 "
        "reserved bits<31> @31:1 reserved }"
        for index in range(registers)
    )
    return f"""
module CsrBank{parameter} {{
    clock clk reset rst
    csr registers @ {base} {{
        {declarations}
    }}
}}
"""


def _parent_source(*, extra_values: int = 0, generic: bool = False) -> str:
    specialization = "<BASE=256>" if generic else ""
    locals_ = "\n".join(
        f"v{index}:u32=truncate<32>(extend<33>(addr)+{index})"
        for index in range(extra_values)
    )
    return f"""
module CsrParent {{
    clock clk reset rst
    in addr:u32 in write:bit in wdata:u32 in read:bit
    out selected:bit
    {locals_}
    bank:CsrBank{specialization} {{ addr write wdata read }}
    selected=bank.registers.R64.value
}}
"""


def test_large_parent_uses_named_csr_projection_without_an_unknown_owner() -> None:
    result = compile_source(
        _bank_source(registers=65) + _parent_source(extra_values=256),
        top="CsrParent",
    )
    selected = result.ir.assignments[0].expression
    assert isinstance(selected, expr.InstanceOutputRef)
    assert (selected.instance, selected.port) == (
        "bank",
        "csr_field_0_64_0_state",
    )
    assert restore(lower(result.ir)) == result.ir

    rtl = emit_experimental(result.ir)
    assert rtl.count("module CsrParent") == 1
    assert rtl.count("module CsrBank") == 1
    assert ".csr_field_0_64_0_state(" in rtl


def test_large_parent_may_use_named_csr_projection_in_an_early_local() -> None:
    source = _bank_source(registers=65) + _parent_source(extra_values=256).replace(
        "selected=bank.registers.R64.value",
        "selected_state:bit=bank.registers.R64.value\n"
        "    selected=selected_state",
    )
    result = compile_source(source, top="CsrParent")
    selected = result.ir.assignments[0].expression
    assert isinstance(selected, expr.InstanceOutputRef)
    assert (selected.instance, selected.port) == (
        "bank",
        "csr_field_0_64_0_state",
    )
    assert restore(lower(result.ir)) == result.ir


def test_named_projection_tracks_declaration_after_an_inserted_field() -> None:
    source = """
module CsrBank {
    clock clk reset rst
    csr registers @0 {
        CONTROL @0 {
            earlier bit @0 rw = 0
            enable bit @1 rw = 0
            reserved bits<30> @31:2 reserved
        }
    }
}
module CsrParent {
    clock clk reset rst
    in addr:u32 in write:bit in wdata:u32 in read:bit
    out enabled:bit
    bank:CsrBank { addr write wdata read }
    enabled=bank.registers.CONTROL.enable
}
"""
    result = compile_source(source, top="CsrParent")
    selected = result.ir.assignments[0].expression
    assert isinstance(selected, expr.InstanceOutputRef)
    assert selected.port == "csr_field_0_0_1_state"


def test_persistent_write_only_state_has_a_named_projection() -> None:
    source = """
module CsrBank {
    clock clk reset rst
    csr registers @0 { COMMAND @0 { payload u8 @7:0 wo = 0 } }
}
module CsrParent {
    clock clk reset rst
    in addr:u32 in write:bit in wdata:u32 in read:bit
    out payload:u8
    bank:CsrBank { addr write wdata read }
    payload=bank.registers.COMMAND.payload
}
"""
    result = compile_source(source, top="CsrParent")
    selected = result.ir.assignments[0].expression
    assert isinstance(selected, expr.InstanceOutputRef)
    assert selected.port == "csr_field_0_0_0_state"


def test_typed_state_status_event_and_group_projections_are_exact() -> None:
    source = """
module CsrBank {
    clock clk reset rst
    in live:u32 out clear:bits<2>
    csr group Window { R @0 { value u32 rw=0 } }
    csr registers @0 {
        FAULT @0 { value u32 @31:0 ro <- live
                    clear_event bits<2> @1:0 on_write -> clear }
        windows:Window[2] @0x10 stride 4
        BASE @0x20 split<32> value u64 rw=0 order low_first
    }
}
module Parent {
    clock clk reset rst
    in addr:u32 in write:bit in wdata:u32 in read:bit in live:u32
    out group_value:u32 out status:u32 out hit:bit
    out event:bits<2> out base:u64
    bank:CsrBank { addr write wdata read live }
    group_value=bank.registers.windows[1].R.value
    status=bank.registers.status.FAULT.value
    hit=bank.registers.events.FAULT.value.write_hit
    event=bank.registers.events.FAULT.clear_event
    base=bank.registers.state.BASE.value
}
"""
    result = compile_source(source, top="Parent")
    assert [assignment.expression.port for assignment in result.ir.assignments] == [
        "csr_field_0_2_0_state",
        "csr_field_0_0_0_value",
        "csr_field_0_0_0_write_hit",
        "csr_event_0_0_0_value",
        "csr_split_0_0_value",
    ]
    assert restore(lower(result.ir)) == result.ir
    rtl = emit_experimental(result.ir)
    assert rtl.count("module CsrBank") == 1
    assert "module CsrBank_zlang_core" not in rtl


def test_post_accept_event_keeps_the_existing_typed_child_projection() -> None:
    source = """
module CsrBank {
    clock clk reset rst
    out clear:bits<2>
    csr registers @0 {
        FAULT @0 {
            value u32 @31:0 ro
            clear_event bits<2> @1:0 on_write post_accept -> clear
        }
    }
}
module Parent {
    clock clk reset rst
    in addr:u32 in write:bit in wdata:u32 in read:bit
    out event:bits<2>
    bank:CsrBank { addr write wdata read }
    event=bank.registers.events.FAULT.clear_event
}
"""
    result = compile_source(source, top="Parent")
    selected = result.ir.assignments[0].expression
    assert isinstance(selected, expr.InstanceOutputRef)
    assert selected.port == "csr_event_0_0_0_value"
    assert restore(lower(result.ir)) == result.ir
    rtl = emit_experimental(result.ir)
    assert "zlang_csr_event_0_0_0_state" in rtl


def test_group_split_value_has_one_typed_child_projection() -> None:
    source = """
module CsrBank {
    clock clk reset rst
    csr group Window {
        CTRL @0 { enable bit @0 rw=0 }
        BASE @4 split<32> value u64 rw=0 order low_first
    }
    csr registers @0 { rows:Window[2] @0 stride 12 }
}
module Parent {
    clock clk reset rst
    in addr:u32 in write:bit in wdata:u32 in read:bit
    out base:u64
    bank:CsrBank { addr write wdata read }
    base=bank.registers.rows[1].BASE.value
}
"""
    result = compile_source(source, top="Parent")
    selected = result.ir.assignments[0].expression
    assert isinstance(selected, expr.InstanceOutputRef)
    assert selected.port == "csr_split_0_1_value"
    assert restore(lower(result.ir)) == result.ir
    rtl = emit_experimental(result.ir)
    assert "csr_split_0_1_value" in rtl


def test_unknown_named_csr_projection_reports_the_complete_path() -> None:
    source = _bank_source(registers=1) + _parent_source().replace(
        "bank.registers.R64.value",
        "bank.registers.R0.missing",
    )
    with pytest.raises(
        SemanticError,
        match=r"no stored field projection 'registers\.R0\.missing'",
    ):
        compile_source(source, top="CsrParent")


def test_named_projection_preserves_the_legacy_physical_abi() -> None:
    prefix = _bank_source(registers=65)
    named = compile_source(prefix + _parent_source(), top="CsrParent").ir
    legacy_source = _parent_source().replace(
        "bank.registers.R64.value",
        "bank.csr_field_0_64_0_state",
    )
    legacy = compile_source(prefix + legacy_source, top="CsrParent").ir
    assert named.assignments == legacy.assignments
    assert emit_experimental(named) == emit_experimental(legacy)


def test_specialized_csr_base_reaches_child_ir_and_rtl() -> None:
    result = compile_source(
        _bank_source(registers=65, generic=True)
        + _parent_source(generic=True),
        top="CsrParent",
    )
    child = result.ir.children[0]
    assert child.csr_blocks[0].base_address == 256
    assert "addr == 32'h00000100" in emit_experimental(result.ir)


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_two_disjoint_csr_blocks_emit_lint_and_simulate(tmp_path: Path) -> None:
    source = """
module MultiCsrBlocks {
    clock clk reset rst
    csr core @0 {
        CONTROL @0 { enable u8 @7:0 rw=0 }
    }
    csr extended @0x400 {
        STATUS @0 { ready u8 @7:0 rw=0 }
    }
}
"""
    module = compile_source(source, top="MultiCsrBlocks").ir
    assert [block.base_address for block in module.csr_blocks] == [0, 0x400]
    rtl = tmp_path / "MultiCsrBlocks.sv"
    rtl.write_text(emit_experimental(module), encoding="utf-8")
    text = rtl.read_text(encoding="utf-8")
    assert text.count("module MultiCsrBlocks") == 1
    assert "32'h00000000" in text
    assert "32'h00000400" in text

    harness = tmp_path / "harness.cpp"
    harness.write_text(
        r'''#include "VMultiCsrBlocks.h"
static void tick(VMultiCsrBlocks &d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
int main() {
  VMultiCsrBlocks d;
  d.addr=0; d.write=0; d.wdata=0; d.read=0;
  d.rst=1; tick(d); d.rst=0;
  d.addr=0; d.write=1; d.wdata=0x5a; tick(d);
  d.addr=0x400; d.wdata=0xa5; tick(d);
  d.write=0; d.read=1; d.addr=0; d.eval();
  if (!d.ready || d.rdata != 0x5a) return 1;
  d.addr=0x400; d.eval();
  if (!d.ready || d.rdata != 0xa5) return 2;
  d.addr=4; d.eval();
  if (d.ready || d.rdata != 0) return 3;
  return 0;
}
''',
        encoding="utf-8",
    )
    obj = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        (
            VERILATOR,
            "--cc",
            "--exe",
            "--build",
            "--top-module",
            "MultiCsrBlocks",
            "--Mdir",
            str(obj),
            "-o",
            "multi_csr_blocks",
            str(rtl),
            str(harness),
        ),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    subprocess.run(
        (str(obj / "multi_csr_blocks"),),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_csr_child_array_emits_one_shared_component_and_lints(
    tmp_path: Path,
) -> None:
    source = _bank_source(registers=1) + """
module CsrArray {
    clock clk reset rst
    in addr:vec<2,u32> in write:vec<2,bit>
    in wdata:vec<2,u32> in read:vec<2,bit>
    out enabled:vec<2,bit> out rdata:vec<2,u32> out ready:vec<2,bit>
    inst bank[2]:CsrBank
    generate(i in 0..2) {
        bank[i].addr=addr[i]
        bank[i].write=write[i]
        bank[i].wdata=wdata[i]
        bank[i].read=read[i]
    }
    enabled=generate(i in 0..2) bank[i].registers.R0.value
    rdata=generate(i in 0..2) bank[i].rdata
    ready=generate(i in 0..2) bank[i].ready
}
"""
    module = compile_source(source, top="CsrArray").ir
    assert [
        (item.instance, item.port)
        for assignment in module.assignments[1:]
        for item in assignment.expression.elements
    ] == [
        ("bank[0]", "rdata"),
        ("bank[1]", "rdata"),
        ("bank[0]", "ready"),
        ("bank[1]", "ready"),
    ]
    assert restore(lower(module)) == module
    rtl = emit_experimental(module)
    assert rtl.count("module CsrBank") == 1
    assert len(re.findall(r"CsrBank_[a-z0-9]+ bank_[01] \(", rtl)) == 2
    path = tmp_path / "csr_array.sv"
    path.write_text(rtl)
    subprocess.run(
        (
            VERILATOR,
            "--lint-only",
            "-Wall",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSEDSIGNAL",
            str(path),
        ),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_csr_child_array_reads_back_two_independent_elements(
    tmp_path: Path,
) -> None:
    source = """
module CsrLane {
    clock clk reset rst
    csr bank @0 {
        VALUE @0 {
            value u8 @7:0 rw = 0
            reserved bits<24> @31:8 reserved
        }
    }
}
module CsrArrayReadback {
    clock clk reset rst
    in addr:vec<2,u32> in write:vec<2,bit>
    in wdata:vec<2,u32> in read:vec<2,bit>
    out rdata:vec<2,u32> out ready:vec<2,bit>
    out values:vec<2,u8>
    inst lane[2]:CsrLane
    generate(i in 0..2) {
        lane[i].addr=addr[i]
        lane[i].write=write[i]
        lane[i].wdata=wdata[i]
        lane[i].read=read[i]
    }
    rdata=generate(i in 0..2) lane[i].rdata
    ready=generate(i in 0..2) lane[i].ready
    values=generate(i in 0..2) lane[i].bank.VALUE.value
}
"""
    rtl = tmp_path / "CsrArrayReadback.sv"
    rtl.write_text(emit_experimental(compile_source(source, top="CsrArrayReadback").ir))
    harness = tmp_path / "harness.cpp"
    harness.write_text(
        r'''#include "VCsrArrayReadback.h"
static void tick(VCsrArrayReadback &d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
int main() {
  VCsrArrayReadback d;
  d.addr=0; d.write=0; d.wdata=0; d.read=0;
  d.rst=1; tick(d); d.rst=0;
  d.write=1; d.wdata=0x000000000000005aULL; tick(d);
  d.write=2; d.wdata=0x000000a500000000ULL; tick(d);
  d.write=0; d.read=3; d.eval();
  if (d.ready != 3) return 1;
  if (d.rdata != 0x000000a50000005aULL) return 2;
  if (d.values != 0xa55a) return 3;
  return 0;
}
'''
    )
    obj = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        (
            VERILATOR,
            "--cc",
            "--exe",
            "--build",
            "--top-module",
            "CsrArrayReadback",
            "--Mdir",
            str(obj),
            "-o",
            "csr_array_readback",
            str(rtl),
            str(harness),
        ),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    subprocess.run(
        (str(obj / "csr_array_readback"),),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("address", "read", "write_data", "expected"),
    (
        ("0", 1, "truncate<8>(bank.rdata)", 0x5A),
        ("0", 1, "truncate<8>(busword)", 0x5A),
        ("0", 1, "bitcast<u8>(busword[15:8])", None),
        ("0", 1, "shortword[7:0]", 0x5A),
        ("0", 1, "shortword[15:8]", None),
        ("0", 1, "offsetword[7:0]", 0x5A),
        ("8", 1, "truncate<8>(bank.rdata)", 0x00),
        ("4", 0, "truncate<8>(bank.rdata)", 0x00),
        ("4", 1, "bank.registers.state.STORED.value", 0x5A),
    ),
    ids=("stored-low-lanes", "alias-low", "alias-live", "nested-slice-low",
         "nested-slice-live", "nested-slice-offset", "unselected-live-register",
         "disabled-read", "named-state"),
)
def test_child_csr_readback_keeps_unobserved_inputs_and_state_out_of_feedback(
    tmp_path: Path, address: str, read: int, write_data: str, expected: int | None,
) -> None:
    path = tmp_path / "stored_csr_feedback.zhl"
    offset = 8 if "offsetword" in write_data else 0
    local = ""
    if "busword" in write_data:
        local = "busword=bank.rdata"
    elif "shortword" in write_data:
        local = "shortword=bank.rdata[15:0]"
    elif offset:
        local = "offsetword=bank.rdata[23:8]"
    path.write_text(f"""
module Bank {{
    clock clk reset rst in live:u8
    csr registers @0 {{
        STORED @0 {{ value u8 @{7+offset}:{offset} rw = 0x5a upper u8 @{15+offset}:{8+offset} ro <- live }}
        LIVE @4 {{ value u8 @7:0 ro <- live }}
    }}
}}
module Top {{
    clock clk reset rst in wr:bit out q:u8
    memory m:mem<u8,2> {{read_port r write_port w read_latency 0 collision write_first}}
    bank:Bank {{live=~m.r.read_data addr={address} read={read} write=0 wdata=0}}
    {local}
    m.r.address=0 m.w.address=0 m.w.enable=wr m.w.data={write_data}
    q=m.r.read_data
}}
""")
    if expected is None:
        for compile_design in (compile_file, sim.compile):
            with pytest.raises(SemanticError, match="bank.rdata -> bank.live -> m.r.read_data -> bank.rdata"):
                compile_design(path)
        return
    rtl = tmp_path / "dut.sv"
    rtl.write_text(emit_artifact(compile_file(path).ir).text)
    subprocess.run(
        ("yosys", "-Q", "-T", "-p",
         f"read_verilog -sv {rtl}; hierarchy -top Top; proc; flatten; opt; scc -expect 0"),
        capture_output=True, text=True, check=True, timeout=30,
    )
    events = [
        {"reset": {"rst": rst}, "set": {"wr": wr}, "edges": ["clk"]}
        for rst, wr in ((True, 0), (False, 1), (False, 1), (False, 0))
    ]
    compared = compare_program(
        sim.compile(path), events, simulator="iverilog",
        artifact_directory=tmp_path / "comparison", timeout=30, build_timeout=30,
    )
    assert compared.native_trace == compared.rtl_trace == tuple(
        {"q": q} for q in (0, expected, expected, expected)
    )
    if address == "0" and read == 1 and write_data == "truncate<8>(bank.rdata)":
        _check_static_child_views(tmp_path)


def _check_static_child_views(tmp_path: Path) -> None:
    source_shapes = (
        ("split", "in x:u13 out saved:u13 out live:u13 reg held:u13=211 saved=held live=~x",
         "child:Child {x=m.r.read_data}", "child.saved", "child.live"),
        ("upper", "in x:u13 out word:u26 reg held:u13=211 word=bitcast<u26>(concat(held,~x))",
         "child:Child {x=m.r.read_data} alias=child.word", "alias[25:13]", "alias[12:0]"),
        ("lower", "in x:u13 out word:u26 reg held:u13=211 word=bitcast<u26>(concat(~x,held))",
         "child:Child {x=m.r.read_data} alias=child.word", "alias[12:0]", "alias[25:13]"),
        ("struct", "in x:u13 out pair:Pair reg held:u13=211 pair=Pair{saved=held live=~x}",
         "child:Child {x=m.r.read_data}", "child.pair.saved", "child.pair.live"),
        ("child-input", "in x:u26 out saved:u13 out live:u13 saved=x[12:0] live=x[25:13]",
         "reg held:u13=211 child:Child {x=bitcast<u26>(concat(~m.r.read_data,held))}",
         "child.saved", "child.live"),
        ("csr", "in packed:u26 csr regs @0 { R @0 { data u26 @25:0 ro <- packed } }",
         "reg held:u13=211 child:Child {packed=bitcast<u26>(concat(~m.r.read_data,held)) "
         "addr=0 read=1 write=0 wdata=0}",
         "child.rdata[12:0]", "child.rdata[25:13]"),
        ("local", "",
         "reg held:u13=211 alias=bitcast<u26>(concat(~m.r.read_data,held))",
         "alias[12:0]", "alias[25:13]"),
    )
    def check(
        name: str, child: str, bindings: str, use: str, latency: int,
        expected: tuple[int, ...] | None, inputs: dict[str, int] | None = None,
    ) -> None:
        inputs = inputs or {}
        selector = "in select_lane:u1" if inputs else ""
        case = tmp_path / name
        case.mkdir()
        path = case / "dut.zhl"
        declaration = f"module Child {{ clock clk reset rst {child} }}" if child else ""
        source = f"""
struct Pair {{ saved:u13 live:u13 }}
{declaration}
module Top {{
    clock clk reset rst in wr:bit out q:u13
    {selector}
    memory m:mem<u13,4> {{read_port r write_port w read_latency {latency} collision write_first}}
    {bindings}
    m.r.address=2 m.w.address=2 m.w.enable=wr
    m.w.data={use}
    q=m.r.read_data
}}
"""
        path.write_text(source)
        if expected is None:
            for entry in (compile_file, sim.compile):
                with pytest.raises(SemanticError, match=r"combinational (storage|memory) dependency cycle") as caught:
                    entry(path)
                origin = caught.value.primary
                assert origin is not None and origin.source_unit == path.name
                assert "m.w.data=" in source.splitlines()[origin.span.start_line - 1]
            return
        rtl = case / "dut.sv"
        rtl.write_text(emit_artifact(compile_file(path).ir).text)
        subprocess.run(
            ("yosys", "-Q", "-T", "-p",
             "read_verilog -sv dut.sv; hierarchy -top Top; proc; flatten; opt; scc -expect 0"),
            cwd=case, capture_output=True, text=True, check=True, timeout=30,
        )
        events = [
            {"reset": {"rst": rst}, "set": {"wr": wr, **inputs}, "edges": ["clk"]}
            for rst, wr in ((True, 0), (False, 1), (False, 1), (False, 0))
        ]
        compared = compare_program(
            sim.compile(path), events, simulator="iverilog",
            artifact_directory=case / "comparison", timeout=30, build_timeout=30,
        )
        assert compared.native_trace == compared.rtl_trace == tuple(
            {"q": q} for q in expected
        )

    stored_trace = (0, 211, 211, 211)
    live_trace = (0, 8191, 0, 0)
    for name, child, bindings, stored, live in source_shapes:
        check(f"{name}-stored", child, bindings, stored, 0, stored_trace)
        check(f"{name}-live", child, bindings, live, 0, None)

    for stored_lane in (0, 1):
        lanes = ["~x", "~x"]
        lanes[stored_lane] = "held"
        vector = f"out word:vec<2,u13> word=[{','.join(lanes)}]"
        packed = f"out word:u26 word=bitcast<u26>(concat({','.join(reversed(lanes))}))"
        tuple_output = f"out word:(u13,u13) word=({','.join(lanes)})"
        field_names = ("saved", "live")
        struct_output = f"out word:Pair word=Pair{{saved={lanes[0]} live={lanes[1]}}}"
        bindings = "child:Child {x=m.r.read_data} alias=child.word"
        for latency in (0, 1):
            for lane in (0, 1):
                expected = stored_trace if lane == stored_lane else (live_trace if latency else None)
                for form, declaration, use in (
                    ("vector", vector, f"alias[{lane}]"),
                    ("packed", packed, f"alias[{13 * lane + 12}:{13 * lane}]"),
                    ("tuple", tuple_output, f"alias[{lane}]"),
                    ("struct", struct_output, f"alias.{field_names[lane]}"),
                ):
                    check(
                        f"{form}-{stored_lane}-{latency}-{lane}",
                        f"in x:u13 reg held:u13=211 {declaration}", bindings, use, latency, expected,
                    )
            check(
                f"dynamic-{stored_lane}-{latency}",
                f"in x:u13 reg held:u13=211 {vector}", bindings, "alias[select_lane]",
                latency, live_trace if latency else None, {"select_lane": 1 - stored_lane},
            )
