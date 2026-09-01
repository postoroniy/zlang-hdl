"""Real-tool parity and M35 smoke for atomic rule-local FIFO transitions."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.backend.clash import (
    emit_formal_artifact as emit_clash_formal_artifact,
    validate_register_formal_artifact,
)
from zlang.compiler import compile_source
from zlang.formal import build_recursive_formal_design, run_verilog_formal
from zlang.ir.formal import FormalStatus
from zlang.toolchain import find_clash_executable, generate_verilog


ROOT = Path(__file__).resolve().parents[2]
VERILATOR = shutil.which("verilator")
CLASH = find_clash_executable()
FORMAL = all(shutil.which(tool) for tool in ("yosys", "sby", "z3"))


SOURCE = """
module AtomicFifo {
  clock clk reset rst
  in x:u8 in op:uint<3>
  out front:u8 out count:uint<2> out a:u8 out b:u8
  fifo q:fifo<u8,2>
  reg ra:u8=0 reg rb:u8=0
  rule push when op==1 { q.push(x) ra <- x rb <- x }
  rule pop when op==2 { q.pop() ra <- q.front rb <- extend<8>(q.count) }
  rule swap when op==3 { q.pop() q.push(x) ra <- q.front rb <- x }
  priority push > pop
  priority push > swap
  priority pop > swap
  front=q.front count=q.count a=ra b=rb
}
"""


HARNESS = r"""
#include "VAtomicFifo.h"
static void tick(VAtomicFifo &d) {
  d.clk = 0; d.eval(); d.clk = 1; d.eval(); d.clk = 0; d.eval();
}
static bool state(VAtomicFifo &d, unsigned count, unsigned front,
                  unsigned a, unsigned b) {
  d.eval();
  return d.count == count && (count == 0 || d.front == front)
      && d.a == a && d.b == b;
}
int main() {
  VAtomicFifo d;
  d.x = 0; d.op = 0; d.rst = 1; tick(d); d.rst = 0;
  d.x = 10; d.op = 1; tick(d);
  if (!state(d, 1, 10, 10, 10)) return 1;
  d.x = 20; d.op = 1; tick(d);
  if (!state(d, 2, 10, 20, 20)) return 2;
  d.x = 99; d.op = 1; tick(d);
  if (!state(d, 2, 10, 20, 20)) return 3;
  d.x = 30; d.op = 3; tick(d);
  if (!state(d, 2, 20, 10, 30)) return 4;
  d.x = 0; d.op = 2; tick(d);
  if (!state(d, 1, 30, 20, 2)) return 5;
  d.op = 2; tick(d);
  if (!state(d, 0, 0, 30, 1)) return 6;
  d.x = 40; d.op = 3; tick(d);
  if (!state(d, 0, 0, 30, 1)) return 7;
  d.rst = 1; tick(d);
  return state(d, 0, 0, 0, 0) ? 0 : 8;
}
"""


def _simulate(files: list[Path], root: Path) -> None:
    harness = root / "harness.cpp"
    harness.write_text(HARNESS)
    obj = root / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        ("verilator", "--cc", "--exe", "--build", "--top-module", "AtomicFifo",
         "--Mdir", str(obj), "-o", "atomic_sim", *map(str, files), str(harness)),
        check=True, capture_output=True, text=True, env=environment,
    )
    subprocess.run((str(obj / "atomic_sim"),), check=True, capture_output=True, text=True)


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_direct_sv_atomic_transition_simulates() -> None:
    module = compile_source(SOURCE).ir
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix="zlang-atomic-sv-") as temporary:
        root = Path(temporary)
        rtl = root / "AtomicFifo.sv"
        rtl.write_text(emit_experimental(module))
        _simulate([rtl], root)


@pytest.mark.skipif(CLASH is None or VERILATOR is None, reason="Clash or Verilator unavailable")
def test_clash_atomic_transition_simulates() -> None:
    compilation = compile_source(SOURCE)
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix="zlang-atomic-clash-") as temporary:
        root = Path(temporary)
        files = list(generate_verilog(compilation.clash, "AtomicFifo", root / "rtl", CLASH))
        _simulate(files, root)


@pytest.mark.skipif(CLASH is None, reason="Clash unavailable")
def test_clash_formal_component_publishes_mixed_state_observations() -> None:
    module = compile_source("""module MixedFormal { clock clk reset rst
      in x:u8 in fire:bit out y:u8 fifo q:fifo<u8,2> reg r:u8=0
      rule update when fire { q.push(x) r <- x } y=r }""").ir
    design = build_recursive_formal_design(module)
    artifact = emit_clash_formal_artifact(module, design)
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix="zlang-atomic-clash-formal-") as temporary:
        files = generate_verilog(artifact.text, module.name, Path(temporary), CLASH)
        validated = validate_register_formal_artifact(artifact, files)
    available = {
        item.semantic_binding_id for item in validated.formal_observations
        if item.observation_token is not None
    }
    required = {
        item.semantic_binding_id for item in design.bindings
        if item.ref.local_semantic_id.startswith(("register:", "fifo:"))
    }
    assert required <= available


@pytest.mark.skipif(not FORMAL, reason="Yosys, SymbiYosys, and Z3 are required")
def test_m35_combined_register_fifo_safety_executes() -> None:
    rtl = emit_experimental(compile_source(SOURCE).ir)
    checks = r"""
  initial assume(rst);
  always @(posedge clk) if (!$initstate) begin
    assert(q_count <= 2);
    assert(!q_pop || q_count > 0);
    assert(!q_push || q_count < 2 || q_pop);
    if ($past(rst)) begin
      assert(count == 0); assert(a == 0); assert(b == 0);
    end
  end
"""
    rtl = rtl.replace("endmodule", checks + "endmodule", 1)
    result = run_verilog_formal(
        rtl, top="AtomicFifo",
        property_id="unified_state_storage.smoke", depth=8, systemverilog=True,
    )
    assert result.status is FormalStatus.BOUNDED_PASS


def test_sdf_stage_generates_both_backends_and_m35_families() -> None:
    result = compile_source((ROOT / "examples/fft_sdf_stage_atomic_transition.zl").read_text())
    assert "module SDFStateStage" in result.clash
    assert "module SDFStateStage" in emit_experimental(result.ir)
    families = {
        (item.generated_from or "").split(":", 1)[0]
        for item in result.formal_design.properties
    }
    assert {"register", "fifo", "rules", "ready_valid"} <= families
