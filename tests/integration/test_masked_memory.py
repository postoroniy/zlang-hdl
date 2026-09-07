from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact, emit_experimental
from zlang.compiler import compile_source
from zlang.toolchain import (
    find_clash_executable,
    generate_verilog,
    lint_with_verilator,
)


VERILATOR = shutil.which("verilator")
CLASH = find_clash_executable()

SOURCE = """
module MaskedScheduledMemory {
  clock clk reset rst
  in op:u2 in address:u2 in data:u16 in mask:bits<2>
  out q:u16
  memory table:mem<u16,4> { read_latency 1 collision write_first }
  rule full when op == 1 { table.write(address,data) }
  rule masked_collision when op == 2 {
    table.read(address)
    table.write(address,data,mask)
  }
  priority full > masked_collision
  q=table.read_data
}
"""

HARNESS = r"""
#include "VMaskedScheduledMemory.h"
static void tick(VMaskedScheduledMemory &d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
int main() {
  VMaskedScheduledMemory d;
  d.op=0; d.address=1; d.data=0; d.mask=0; d.rst=1; tick(d); d.rst=0;
  d.op=1; d.data=0x1234; tick(d); if (d.q != 0) return 1;
  d.op=2; d.data=0xabcd; d.mask=1; tick(d); if (d.q != 0x12cd) return 2;
  d.op=2; d.data=0xee00; d.mask=2; tick(d); if (d.q != 0xeecd) return 3;
  d.op=2; d.data=0; d.mask=0; tick(d); if (d.q != 0xeecd) return 4;
  d.rst=1; tick(d); if (d.q != 0) return 5;
  return 0;
}
"""

GLOBAL_SOURCE = """
module MaskedGlobalMemory {
  clock clk reset rst
  in address:u2 in write_enable:bit in data:u16 in mask:bits<2>
  out q:u16
  memory table:mem<u16,4> { read_latency 1 collision write_first }
  table.read_address=address
  table.write_enable=write_enable
  table.write_address=address
  table.write_data=data
  table.write_mask=mask
  q=table.read_data
}
"""

GLOBAL_HARNESS = r"""
#include "VMaskedGlobalMemory.h"
static void tick(VMaskedGlobalMemory &d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
int main() {
  VMaskedGlobalMemory d;
  d.address=1; d.write_enable=0; d.data=0; d.mask=0; d.rst=1; tick(d); d.rst=0;
  d.write_enable=1; d.data=0x1234; d.mask=3; tick(d); if (d.q != 0x1234) return 1;
  d.data=0xabcd; d.mask=1; tick(d); if (d.q != 0x12cd) return 2;
  d.data=0xee00; d.mask=2; tick(d); if (d.q != 0xeecd) return 3;
  d.data=0; d.mask=0; tick(d); if (d.q != 0xeecd) return 4;
  d.rst=1; tick(d); if (d.q != 0) return 5;
  return 0;
}
"""


def partial_lane_source(
    *,
    scheduled: bool,
    aggregate: bool = False,
    width: int = 13,
) -> str:
    type_declaration = (
        "struct Packed13 { upper:bits<5> lower:u8 }" if aggregate else ""
    )
    type_name = "Packed13" if aggregate else f"bits<{width}>"
    top = (
        "Scheduled" if scheduled else "Global"
    ) + ("Aggregate13" if aggregate else f"Width{width}")
    mask_width = (width + 7) // 8
    controls = (
        "rule access when enable { table.read(address) "
        "table.write(address,data,mask) }"
        if scheduled
        else "table.read_address=address table.write_enable=enable "
        "table.write_address=address table.write_data=data "
        "table.write_mask=mask"
    )
    return f"""
{type_declaration}
module {top} {{
  clock clk reset rst
  in enable:bit in address:u2 in data:{type_name} in mask:bits<{mask_width}>
  out q:{type_name}
  memory table:mem<{type_name},4> {{ read_latency 1 collision write_first }}
  {controls}
  q=table.read_data
}}
"""


def partial_lane_harness(top: str) -> str:
    return f"""
#include \"V{top}.h\"
static void tick(V{top} &d) {{
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}}
int main() {{
  V{top} d;
  d.address=1; d.enable=0; d.data=0; d.mask=0; d.rst=1; tick(d); d.rst=0;
  d.enable=1; d.data=0x1234; d.mask=3; tick(d); if (d.q != 0x1234) return 1;
  d.data=0x1f00; d.mask=1; tick(d); if (d.q != 0x1200) return 2;
  d.data=0x1fab; d.mask=2; tick(d); if (d.q != 0x1f00) return 3;
  d.data=0; d.mask=0; tick(d); if (d.q != 0x1f00) return 4;
  d.rst=1; tick(d); if (d.q != 0) return 5;
  return 0;
}}
"""


def aggregate_partial_lane_harness(top: str) -> str:
    return f"""
#include \"V{top}.h\"
static void tick(V{top} &d) {{
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}}
int main() {{
  V{top} d;
  d.address=1; d.enable=0; d.data_upper=0; d.data_lower=0;
  d.mask=0; d.rst=1; tick(d); d.rst=0;
  d.enable=1; d.data_upper=0x12; d.data_lower=0x34; d.mask=3;
  tick(d); if (d.q_upper != 0x12 || d.q_lower != 0x34) return 1;
  d.data_upper=0x1f; d.data_lower=0xab; d.mask=1;
  tick(d); if (d.q_upper != 0x12 || d.q_lower != 0xab) return 2;
  d.data_upper=0x1f; d.data_lower=0; d.mask=2;
  tick(d); if (d.q_upper != 0x1f || d.q_lower != 0xab) return 3;
  d.data_upper=0; d.data_lower=0; d.mask=0;
  tick(d); if (d.q_upper != 0x1f || d.q_lower != 0xab) return 4;
  d.rst=1; tick(d); if (d.q_upper != 0 || d.q_lower != 0) return 5;
  return 0;
}}
"""


def _simulate(
    files: list[Path] | tuple[Path, ...],
    root: Path,
    *,
    top: str = "MaskedScheduledMemory",
    harness_text: str = HARNESS,
) -> None:
    harness = root / "harness.cpp"
    harness.write_text(harness_text)
    obj = root / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build", "--top-module",
            top, "--Mdir", str(obj), "-o", "masked_sim",
            *map(str, files), str(harness),
        ),
        check=True, capture_output=True, text=True, env=environment,
    )
    subprocess.run(
        (str(obj / "masked_sim"),),
        check=True, capture_output=True, text=True,
    )


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_masked_memory_direct_sv_is_deterministic_and_simulates(
    tmp_path: Path,
) -> None:
    module = compile_source(SOURCE).ir
    emitted = emit_experimental(module)
    assert emit_experimental(module) == emitted
    assert "table_write_mask_expanded" in emitted
    assert "table_write_merged" in emitted
    artifact = emit_artifact(module)
    restored = BackendArtifact.from_json(artifact.to_json())
    assert restored.artifact_hash == artifact.artifact_hash
    assert restored.bindings == artifact.bindings
    assert any(
        binding.semantic_signal_id == "port:mask"
        for binding in artifact.bindings
    )
    rtl = tmp_path / "MaskedScheduledMemory.sv"
    rtl.write_text(emitted)
    subprocess.run(
        ("verilator", "--lint-only", "-Wall", str(rtl)),
        check=True, capture_output=True, text=True,
    )
    _simulate((rtl,), tmp_path)


def test_byte_aligned_mask_expansion_keeps_its_existing_backend_shape() -> None:
    result = compile_source(SOURCE)
    direct = emit_experimental(result.ir)
    assert (
        "assign zlang_table_write_mask_expanded = "
        "{{8{zlang_table_write_mask[1]}}, {8{zlang_table_write_mask[0]}}};"
    ) in direct
    assert (
        "effectiveMask = (pack (concatMap "
        "(\\lane -> repeat lane :: Vec 8 Bit) "
        "(unpack mask :: Vec 2 Bit)) :: BitVector 16)"
    ) in result.clash
    assert "effectiveMask = (resize" not in result.clash


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_all_partial_byte_width_classes_are_strict_direct_sv(tmp_path: Path) -> None:
    for width in (1, 7, 9, 13):
        for scheduled in (False, True):
            top = ("Scheduled" if scheduled else "Global") + f"Width{width}"
            module = compile_source(
                partial_lane_source(scheduled=scheduled, width=width),
                include_clash=False,
            ).ir
            rtl = tmp_path / f"{top}.sv"
            rtl.write_text(emit_experimental(module))
            lint_with_verilator((rtl,), top, VERILATOR)


@pytest.mark.skipif(
    CLASH is None or VERILATOR is None,
    reason="Clash or Verilator unavailable",
)
def test_masked_memory_real_clash_is_cycle_identical(tmp_path: Path) -> None:
    result = compile_source(SOURCE)
    assert "table_write_merged" in result.clash
    files = generate_verilog(
        result.clash, "MaskedScheduledMemory", tmp_path / "rtl", CLASH
    )
    _simulate(files, tmp_path)


@pytest.mark.skipif(
    CLASH is None or VERILATOR is None,
    reason="Clash or Verilator unavailable",
)
def test_global_masked_memory_direct_sv_and_clash_are_cycle_identical(
    tmp_path: Path,
) -> None:
    result = compile_source(GLOBAL_SOURCE)
    direct = tmp_path / "direct"
    direct.mkdir()
    direct_rtl = direct / "MaskedGlobalMemory.sv"
    direct_rtl.write_text(emit_experimental(result.ir))
    _simulate(
        (direct_rtl,), direct, top="MaskedGlobalMemory",
        harness_text=GLOBAL_HARNESS,
    )

    clash = tmp_path / "clash"
    clash.mkdir()
    files = generate_verilog(
        result.clash, "MaskedGlobalMemory", clash / "rtl", CLASH
    )
    _simulate(
        files, clash, top="MaskedGlobalMemory", harness_text=GLOBAL_HARNESS,
    )


@pytest.mark.skipif(
    CLASH is None or VERILATOR is None,
    reason="Clash or Verilator unavailable",
)
def test_partial_byte_lane_direct_sv_and_clash_are_cycle_identical(
    tmp_path: Path,
) -> None:
    for scheduled in (False, True):
        top = "ScheduledWidth13" if scheduled else "GlobalWidth13"
        result = compile_source(partial_lane_source(scheduled=scheduled))
        direct = tmp_path / f"direct_{top}"
        direct.mkdir()
        direct_rtl = direct / f"{top}.sv"
        direct_text = emit_experimental(result.ir)
        direct_rtl.write_text(direct_text)
        assert (
            "{5{zlang_table_write_mask[1]}}, "
            "{8{zlang_table_write_mask[0]}}"
        ) in direct_text
        _simulate(
            (direct_rtl,), direct, top=top,
            harness_text=partial_lane_harness(top),
        )

        clash = tmp_path / f"clash_{top}"
        clash.mkdir()
        assert "resize" in result.clash
        assert "BitVector 13" in result.clash
        files = generate_verilog(result.clash, top, clash / "rtl", CLASH)
        _simulate(
            files, clash, top=top,
            harness_text=partial_lane_harness(top),
        )


@pytest.mark.skipif(
    CLASH is None or VERILATOR is None,
    reason="Clash or Verilator unavailable",
)
def test_thirteen_bit_struct_memory_is_bitpack_closed_in_both_backends(
    tmp_path: Path,
) -> None:
    for scheduled in (False, True):
        top = "ScheduledAggregate13" if scheduled else "GlobalAggregate13"
        result = compile_source(
            partial_lane_source(scheduled=scheduled, aggregate=True)
        )
        module = result.ir

        direct_text = emit_experimental(module)
        direct_dir = tmp_path / f"direct_{top}"
        direct_dir.mkdir()
        direct_rtl = direct_dir / f"{top}.sv"
        direct_rtl.write_text(direct_text)
        lint_with_verilator((direct_rtl,), top, VERILATOR)
        _simulate(
            (direct_rtl,),
            direct_dir,
            top=top,
            harness_text=aggregate_partial_lane_harness(top),
        )

        clash_source = result.clash
        assert "deriving (Generic, NFDataX, Show, Eq, BitPack)" in clash_source
        clash_dir = tmp_path / f"clash_{top}"
        clash_dir.mkdir()
        clash_files = generate_verilog(
            clash_source, top, clash_dir / "rtl", CLASH
        )
        lint_with_verilator(clash_files, top, VERILATOR)
        _simulate(
            clash_files,
            clash_dir,
            top=top,
            harness_text=aggregate_partial_lane_harness(top),
        )
