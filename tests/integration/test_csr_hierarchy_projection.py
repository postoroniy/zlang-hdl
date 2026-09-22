"""Named CSR hierarchy projections remain bounded and ABI-compatible."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.ir import expressions as expr
from zlang.opt import lower, restore
from zlang.semantic import SemanticError


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
