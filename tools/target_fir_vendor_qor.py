#!/usr/bin/env python3
"""Vivado validation for the manually selected four-DSP48E1 FIR graph."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time

from zlang.backend.systemverilog import emit_experimental, emit_target
from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples" / "symmetric_fixed_fir.zhl"


def _tcl(rtl: Path, work: Path, part: str, period: float) -> str:
    return f"""
read_verilog -sv {{{rtl}}}
synth_design -top SymmetricFixedFIR -part {part} -flatten_hierarchy none
create_clock -name clk -period {period} [get_ports clk]
set data_inputs [get_ports -filter {{DIRECTION == IN && NAME != clk}}]
set_input_delay 0.0 -clock clk $data_inputs
set_output_delay 0.0 -clock clk [all_outputs]
opt_design
place_design
phys_opt_design
route_design
report_utilization -file {{{work / 'utilization.rpt'}}}
report_timing_summary -file {{{work / 'timing.rpt'}}}
set path [get_timing_paths -setup -max_paths 1]
set wns [get_property SLACK $path]
set critical [expr {{{period} - $wns}}]
set fmax [expr {{$critical > 0.0 ? 1000.0 / $critical : 0.0}}]
set lut [llength [get_cells -hier -filter {{REF_NAME =~ LUT*}}]]
set ff [llength [get_cells -hier -filter {{REF_NAME =~ FD*}}]]
set dsp [llength [get_cells -hier -filter {{REF_NAME == DSP48E1}}]]
set dsp_cells [lsort [get_cells -hier -filter {{REF_NAME == DSP48E1}}]]
set dsp_locations [join [lmap cell $dsp_cells {{get_property LOC $cell}}] ,]
set bram [llength [get_cells -hier -filter {{REF_NAME =~ RAMB*}}]]
set pcin_pins [llength [get_pins -hier -filter {{REF_PIN_NAME =~ PCIN*}}]]
set pcout_pins [llength [get_pins -hier -filter {{REF_PIN_NAME =~ PCOUT*}}]]
set output [open {{{work / 'metrics.txt'}}} w]
puts $output "lut=$lut"
puts $output "ff=$ff"
puts $output "dsp=$dsp"
puts $output "bram=$bram"
puts $output "wns_ns=$wns"
puts $output "fmax_mhz=$fmax"
puts $output "pcin_pins=$pcin_pins"
puts $output "pcout_pins=$pcout_pins"
puts $output "dsp_locations=$dsp_locations"
close $output
write_checkpoint -force {{{work / 'routed.dcp'}}}
exit
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vivado", default=shutil.which("vivado") or "vivado")
    parser.add_argument("--part", default="xc7z030ffg676-1")
    parser.add_argument("--period-ns", type=float, default=10.0)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source = SOURCE.read_text()
    selected = compile_source(
        source, target=args.part,
        architecture="Xilinx7SymmetricDSPCascade",
        architecture_mode="required",
    )
    generic = compile_source(source)
    rows = []
    for name, rtl_text in (
        ("target_dsp48e1", emit_target(selected.ir, selected.implementation_graph)),
        ("generic_direct_sv", emit_experimental(generic.ir)),
    ):
        work = args.output / name
        work.mkdir(parents=True, exist_ok=True)
        rtl = work / "SymmetricFixedFIR.sv"
        rtl.write_text(rtl_text)
        script = work / "run.tcl"
        script.write_text(_tcl(rtl, work, args.part, args.period_ns))
        started = time.monotonic()
        completed = subprocess.run(
            (args.vivado, "-mode", "batch", "-nojournal", "-nolog", "-source", str(script)),
            cwd=work, capture_output=True, text=True,
        )
        elapsed = time.monotonic() - started
        (work / "vivado.stdout.log").write_text(completed.stdout)
        (work / "vivado.stderr.log").write_text(completed.stderr)
        metrics_file = work / "metrics.txt"
        if completed.returncode != 0 or not metrics_file.exists():
            raise RuntimeError(f"Vivado failed for {name}; see {work}")
        metrics = dict(line.split("=", 1) for line in metrics_file.read_text().splitlines())
        row = {
            "implementation": name,
            "part": args.part,
            "period_ns": args.period_ns,
            "latency": 1,
            "ii": 1,
            "compile_seconds": round(elapsed, 3),
            "rtl_bytes": rtl.stat().st_size,
            "rtl_lines": len(rtl_text.splitlines()),
            **{key: (int(value) if key in {"lut", "ff", "dsp", "bram", "pcin_pins", "pcout_pins"}
                    else value if key == "dsp_locations" else float(value))
               for key, value in metrics.items()},
        }
        rows.append(row)
    output = {"tool": "Vivado 2024.2", "implementation_graph": selected.implementation_graph.identity,
              "results": rows}
    (args.output / "results.json").write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
