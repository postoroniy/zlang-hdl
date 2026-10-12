from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[2]
VERILATOR = shutil.which("verilator")


def _run(top: str, source_name: str, harness: str, tmp_path: Path) -> None:
    module = compile_source(
        (ROOT / "examples" / source_name).read_text()
    ).ir
    rtl = tmp_path / f"{top}.sv"
    cpp = tmp_path / "test.cpp"
    rtl.write_text(emit_experimental(module))
    cpp.write_text(harness)
    obj = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    result = subprocess.run(
        (
            VERILATOR or "verilator", "--cc", "--exe", "--build",
            "--top-module", top, "--Mdir", str(obj), str(rtl), str(cpp),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    run = subprocess.run(
        (str(obj / f"V{top}"),), capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_implement_intent_uses_composed_direct_sv_lowering() -> None:
    module = compile_source(
        """
        module ImplementSV {
          clock clk
          reset rst
          in a : vec<4,u3>
          in b : vec<4,u3>
          out y : u8
          y = implement {
            dot(a, b)
            intent { latency <= 4 ii == 1 dsp <= 4 minimize lut }
          }
        }
        """,
    ).ir
    rtl = emit_experimental(module)
    assert "module ImplementSV" in rtl
    assert "input wire logic clk" in rtl
    assert "output logic" in rtl


@pytest.mark.skipif(VERILATOR is None, reason="Verilator is required")
def test_auto_pipeline_nested_stage_dag_has_reported_latency(tmp_path: Path) -> None:
    _run(
        "AutoPipelineProducts",
        "implementation_intent.zhl",
        r'''
#include "VAutoPipelineProducts.h"
static void tick(VAutoPipelineProducts& d) {
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}
static void drive(VAutoPipelineProducts& d, int n) {
  d.a=n+1; d.b=2; d.c=n+2; d.d=3;
  d.e=n+3; d.f=4; d.g=n+4; d.h=5;
}
int main() {
  VAutoPipelineProducts d; d.rst=1; drive(d,0); tick(d);
  if (d.y != 40) return 1;
  d.rst=0; drive(d,0); d.eval(); if (d.y != 40) return 2;
  drive(d,1); d.eval(); if (d.y != 54) return 3;
  drive(d,2); d.eval(); if (d.y != 68) return 4;
  drive(d,3); d.eval(); return d.y == 82 ? 0 : 5;
}
''',
        tmp_path,
    )


@pytest.mark.skipif(VERILATOR is None, reason="Verilator is required")
@pytest.mark.parametrize(
    ("source_name", "top"),
    (
        ("cost_mac.zhl", "CostMac"),
        ("cost_mac_no_dsp.zhl", "CostMacNoDsp"),
        ("mac_choice.zhl", "MacChoice"),
    ),
)
def test_selected_mac_pipeline_preserves_one_cycle_latency(
    source_name: str, top: str, tmp_path: Path
) -> None:
    _run(
        top,
        source_name,
        f'''
#include "V{top}.h"
static void tick(V{top}& d) {{
  d.clk=0; d.eval(); d.clk=1; d.eval(); d.clk=0; d.eval();
}}
int main() {{
  V{top} d; d.rst=1; d.a=2; d.b=3; d.c=4; tick(d);
  if (d.y != 0) return 1;
  d.rst=0; d.a=5; d.b=6; d.c=7; d.eval();
  if (d.y != 0) return 2; tick(d);
  if (d.y != 37) return 3;
  d.a=8; d.b=9; d.c=10; d.eval();
  if (d.y != 37) return 4; tick(d);
  return d.y == 82 ? 0 : 5;
}}
''',
        tmp_path,
    )
