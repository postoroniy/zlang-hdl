#!/usr/bin/env python3
"""Route the selected high-level target-aware FIR candidates in Vivado."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time

from zlang.backend.systemverilog import emit_target
from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples" / "symmetric_fixed_fir_auto.zhl"


def _tcl(rtl: Path, xdc: Path, work: Path, top: str, part: str, period: float) -> str:
    return f"""
read_verilog -sv {{{rtl}}}
read_xdc {{{xdc}}}
synth_design -top {top} -part {part} -flatten_hierarchy none
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
set dsp_cells [lsort [get_cells -hier -filter {{REF_NAME == DSP48E1}}]]
set dsp [llength $dsp_cells]
set configs {{}}
set locations {{}}
foreach cell $dsp_cells {{
  lappend configs "[get_property AREG $cell]/[get_property BREG $cell]/[get_property DREG $cell]/[get_property MREG $cell]/[get_property PREG $cell]"
  lappend locations [get_property LOC $cell]
}}
set output [open {{{work / 'metrics.txt'}}} w]
puts $output "lut=$lut"
puts $output "ff=$ff"
puts $output "dsp=$dsp"
puts $output "wns_ns=$wns"
puts $output "fmax_mhz=$fmax"
puts $output "dsp_configs=[join $configs ,]"
puts $output "dsp_locations=[join $locations ,]"
close $output
write_checkpoint -force {{{work / 'routed.dcp'}}}
exit
"""


def _route(name, top, module, graph, output, vivado, part, period):
    work = output / name
    work.mkdir(parents=True, exist_ok=True)
    rtl_text = emit_target(module, graph)
    rtl = work / f"{top}.sv"
    rtl.write_text(rtl_text)
    xdc = work / "timing.xdc"
    xdc.write_text(f"create_clock -name clk -period {period} [get_ports clk]\n")
    script = work / "run.tcl"
    script.write_text(_tcl(rtl, xdc, work, top, part, period))
    started = time.monotonic()
    run = subprocess.run(
        (vivado, "-mode", "batch", "-nojournal", "-nolog", "-source", str(script)),
        cwd=work, capture_output=True, text=True,
    )
    (work / "vivado.stdout.log").write_text(run.stdout)
    (work / "vivado.stderr.log").write_text(run.stderr)
    if run.returncode or not (work / "metrics.txt").exists():
        raise RuntimeError(f"Vivado failed for {name}; see {work}")
    raw = dict(line.split("=", 1) for line in (work / "metrics.txt").read_text().splitlines())
    return {
        "name": name,
        "top": top,
        "implementation_graph": graph.identity,
        "pipeline_configuration": graph.pipeline_configuration_identity,
        "active_sites": list(graph.active_pipeline_sites),
        "latency": graph.latency,
        "ii": graph.initiation_interval,
        "compensation_cycles": sum(
            item.cycles for item in (graph.timing_dag.compensation_delays if graph.timing_dag else ())
        ),
        "dsp": int(raw["dsp"]),
        "lut": int(raw["lut"]),
        "ff": int(raw["ff"]),
        "wns_ns": float(raw["wns_ns"]),
        "fmax_mhz": float(raw["fmax_mhz"]),
        "dsp_configs": raw["dsp_configs"],
        "dsp_locations": raw["dsp_locations"],
        "compile_seconds": round(time.monotonic() - started, 3),
        "rtl_bytes": len(rtl_text.encode()),
        "rtl_lines": len(rtl_text.splitlines()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vivado", default=shutil.which("vivado") or "vivado")
    parser.add_argument("--part", default="xc7z030ffg676-1")
    parser.add_argument("--period-ns", type=float, default=10.0)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source = SOURCE.read_text()
    bounded = compile_source(
        source, top="SymmetricFixedFIRAuto", target=args.part,
        target_evidence_policy="measured_required",
    )
    exact = compile_source(source, top="SymmetricFixedFIRAutoExact8", target=args.part)
    exact_graph = next(
        item.graph for item in exact.target_planning_result.generated_candidates
        if item.graph.pipeline_configuration_identity
        and item.graph.pipeline_configuration_identity.endswith("multiply_output_registered")
    )
    rows = (
        _route("bounded", "SymmetricFixedFIRAuto", bounded.ir,
               bounded.implementation_graph, args.output, args.vivado, args.part, args.period_ns),
        _route("exact8", "SymmetricFixedFIRAutoExact8", exact.ir,
               exact_graph, args.output, args.vivado, args.part, args.period_ns),
    )
    payload = {
        "schema": "zlang-target-auto-fir-qor-v1",
        "tool": "Vivado 2024.2",
        "part": args.part,
        "period_ns": args.period_ns,
        "results": rows,
    }
    (args.output / "results.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
