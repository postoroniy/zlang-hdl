from pathlib import Path
import os
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_experimental, emit_target, emit_target_artifact
from zlang.compiler import compile_source
from zlang.toolchain import find_clash_executable, generate_verilog


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples/target_bram_memory.zhl").read_text()


def _selected():
    return compile_source(
        SOURCE, target="xc7z030ffg676-1",
        architecture="Xilinx7BRAM36SimpleDualPort", architecture_mode="required",
    )


def test_generic_and_selected_memory_keep_one_cycle_read_first_semantics() -> None:
    generic = compile_source(SOURCE)
    selected = _selected()
    assert generic.ir == selected.ir
    assert generic.implementation_graph.is_generic
    assert selected.implementation_graph.latency == 1
    assert selected.implementation_graph.pipeline_configuration_identity.endswith(".core_registered")
    target_rtl = emit_target(selected.ir, selected.implementation_graph)
    assert '(* ram_style = "block" *)' in target_rtl
    assert "RAMB36E1" not in target_rtl  # inference binding, not a guessed primitive ABI
    artifact = emit_target_artifact(selected.ir, selected.implementation_graph)
    assert artifact.implementation.intended_resource_counts == artifact.implementation.emitted_resource_counts
    assert emit_experimental(generic.ir)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
@pytest.mark.parametrize("selected", (False, True))
def test_generic_and_selected_memory_are_bit_exact_in_verilator(
    tmp_path: Path, selected: bool,
) -> None:
    compilation = _selected() if selected else compile_source(SOURCE)
    rtl = tmp_path / "TargetBRAMMemory.sv"
    rtl.write_text(
        emit_target(compilation.ir, compilation.implementation_graph)
        if selected else emit_experimental(compilation.ir)
    )
    bench = tmp_path / "tb.sv"
    bench.write_text("""
module tb;
  logic clk=0,rst=1,write_enable=0;
  logic [9:0] read_address=0,write_address=0;
  logic [35:0] write_data=0;
  wire [35:0] read_data;
  TargetBRAMMemory dut(.*);
  task tick; begin #1 clk=1; #1; clk=0; #1; end endtask
  initial begin
    tick; rst=0;
    read_address=10'd0; write_address=10'd7; write_data=36'h123456789; write_enable=1; tick;
    write_enable=0; read_address=10'd7; tick;
    if (read_data !== 36'h123456789) $fatal(1,"stored read mismatch");
    $finish;
  end
endmodule
""")
    obj = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        ("verilator", "--binary", "--timing", "-Wno-fatal", "--top-module", "tb",
         str(rtl), str(bench), "-Mdir", str(obj)),
        check=True, capture_output=True, text=True, env=environment,
    )
    subprocess.run((str(obj / "Vtb"),), check=True, capture_output=True, text=True)


@pytest.mark.skipif(
    find_clash_executable() is None or shutil.which("verilator") is None,
    reason="Clash or Verilator unavailable",
)
def test_generic_memory_generates_real_clash_verilog(tmp_path: Path) -> None:
    compilation = compile_source(SOURCE)
    files = generate_verilog(
        compilation.clash, "TargetBRAMMemory", tmp_path / "clash-rtl"
    )
    subprocess.run(
        ("verilator", "--lint-only", "-Wno-fatal", "--top-module",
         "TargetBRAMMemory", *(str(path) for path in files)),
        check=True, capture_output=True, text=True,
    )
