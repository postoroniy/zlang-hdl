#!/usr/bin/env python3
"""Vivado truthfulness check for generic versus selected RAMB36 inference."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess

from zlang.backend.systemverilog import emit_experimental, emit_target
from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples" / "target_bram_memory.zl"


def _tcl(rtl: Path, xdc: Path, work: Path, part: str) -> str:
    return f"""
read_verilog -sv {{{rtl}}}
read_xdc {{{xdc}}}
synth_design -top TargetBRAMMemory -part {part} -flatten_hierarchy none
opt_design
place_design
route_design
report_utilization -file {{{work / 'utilization.rpt'}}}
set ramb36 [llength [get_cells -hier -filter {{REF_NAME == RAMB36E1}}]]
set ramb18 [llength [get_cells -hier -filter {{REF_NAME == RAMB18E1}}]]
set output [open {{{work / 'metrics.txt'}}} w]
puts $output "ramb36=$ramb36"
puts $output "ramb18=$ramb18"
close $output
exit
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vivado", default=shutil.which("vivado") or "vivado")
    parser.add_argument("--part", default="xc7z030ffg676-1")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source = SOURCE.read_text()
    generic = compile_source(source)
    selected = compile_source(
        source, target=args.part, architecture="Xilinx7BRAM36SimpleDualPort",
        architecture_mode="required",
    )
    rows = []
    for name, rtl_text in (
        ("generic", emit_experimental(generic.ir)),
        ("selected_ramb36", emit_target(selected.ir, selected.implementation_graph)),
    ):
        work = args.output / name
        work.mkdir(parents=True, exist_ok=True)
        rtl = work / "TargetBRAMMemory.sv"
        rtl.write_text(rtl_text)
        xdc = work / "timing.xdc"
        xdc.write_text("create_clock -name clk -period 10.0 [get_ports clk]\n")
        script = work / "run.tcl"
        script.write_text(_tcl(rtl, xdc, work, args.part))
        completed = subprocess.run(
            (args.vivado, "-mode", "batch", "-nojournal", "-nolog", "-source", str(script)),
            cwd=work, capture_output=True, text=True,
        )
        (work / "vivado.stdout.log").write_text(completed.stdout)
        (work / "vivado.stderr.log").write_text(completed.stderr)
        if completed.returncode or not (work / "metrics.txt").exists():
            raise RuntimeError(f"Vivado failed for {name}; see {work}")
        raw = dict(line.split("=", 1) for line in (work / "metrics.txt").read_text().splitlines())
        rows.append({"implementation": name, "ramb36": int(raw["ramb36"]),
                     "ramb18": int(raw["ramb18"])})
    payload = {"tool": "Vivado 2024.2", "part": args.part, "results": rows}
    (args.output / "results.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
