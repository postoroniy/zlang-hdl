#!/usr/bin/env python3
"""Series-7 fixed-latency dot-product DSP inference/retiming experiment."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import time

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.ir import expressions as ir_expr


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples" / "dot_product_pipelined.zhl"
VARIANTS = {
    "baseline_auto": (False, "auto", False),
    "forced_dsp": (True, "force", False),
    "forced_dsp_retimed": (True, "force", True),
}


def _tcl(rtl: Path, xdc: Path, work: Path, top: str, part: str, period: float,
         cascade: str, retiming: bool) -> str:
    synth_retime = " -retiming" if retiming else ""
    phys = "phys_opt_design -directive AddRetime" if retiming else "phys_opt_design"
    return f"""
read_verilog -sv {{{rtl}}}
read_xdc {{{xdc}}}
synth_design -top {top} -part {part} -flatten_hierarchy none -cascade_dsp {cascade}{synth_retime}
set data_inputs [get_ports -filter {{DIRECTION == IN && NAME != clk}}]
set_input_delay 0.0 -clock clk $data_inputs
set_output_delay 0.0 -clock clk [all_outputs]
opt_design
place_design
{phys}
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
set bram [llength [get_cells -hier -filter {{REF_NAME =~ RAMB*}}]]
set configs {{}}
foreach cell $dsp_cells {{
  set config "[get_property AREG $cell]/[get_property BREG $cell]/[get_property MREG $cell]/[get_property PREG $cell]"
  lappend configs $config
}}
set output [open {{{work / 'metrics.txt'}}} w]
puts $output "lut=$lut"
puts $output "ff=$ff"
puts $output "dsp=$dsp"
puts $output "bram=$bram"
puts $output "wns_ns=$wns"
puts $output "fmax_mhz=$fmax"
puts $output "dsp_areg_breg_mreg_preg=[join $configs ,]"
close $output
write_checkpoint -force {{{work / 'routed.dcp'}}}
exit
"""


def _run(name: str, rtl_text: str, use_dsp: bool, cascade: str, retiming: bool,
         output: Path, vivado: str, top: str, part: str, period: float,
         latency: int) -> dict[str, object]:
    work = output / name
    work.mkdir(parents=True, exist_ok=True)
    if use_dsp:
        rtl_text = rtl_text.replace(
            f"module {top} (",
            f'(* use_dsp = "yes" *) module {top} (', 1,
        )
    rtl = work / "DotProductPipelined.sv"
    rtl.write_text(rtl_text)
    xdc = work / "timing.xdc"
    xdc.write_text(f"create_clock -name clk -period {period} [get_ports clk]\n")
    script = work / "run.tcl"
    script.write_text(_tcl(rtl, xdc, work, top, part, period, cascade, retiming))
    started = time.monotonic()
    completed = subprocess.run(
        (vivado, "-mode", "batch", "-nojournal", "-nolog", "-source", str(script)),
        cwd=work, capture_output=True, text=True,
    )
    elapsed = time.monotonic() - started
    (work / "vivado.stdout.log").write_text(completed.stdout)
    (work / "vivado.stderr.log").write_text(completed.stderr)
    metrics_path = work / "metrics.txt"
    if completed.returncode != 0 or not metrics_path.exists():
        raise RuntimeError(f"Vivado failed for {name}; see {work}")
    raw = dict(line.split("=", 1) for line in metrics_path.read_text().splitlines())
    return {
        "variant": name, "part": part, "period_ns": period,
        "latency": latency, "ii": 1, "use_dsp": use_dsp,
        "cascade_dsp": cascade, "retiming": retiming,
        "dsp": int(raw["dsp"]), "lut": int(raw["lut"]),
        "ff": int(raw["ff"]), "bram": int(raw["bram"]),
        "wns_ns": float(raw["wns_ns"]), "fmax_mhz": float(raw["fmax_mhz"]),
        "dsp_areg_breg_mreg_preg": raw["dsp_areg_breg_mreg_preg"],
        "compile_seconds": round(elapsed, 3),
        "rtl_bytes": rtl.stat().st_size, "rtl_lines": len(rtl_text.splitlines()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--top", default="DotProductPipelined")
    parser.add_argument("--latency", type=int, default=8)
    parser.add_argument("--vivado", default=shutil.which("vivado") or "vivado")
    parser.add_argument("--part", default="xc7z030ffg676-1")
    parser.add_argument("--period-ns", type=float, default=10.0)
    parser.add_argument("--jobs", type=int, default=3)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    compilation = compile_source(args.source.read_text(), top=args.top)
    output_assignment = next(
        item for item in compilation.ir.assignments if item.target.name == "y"
    )
    if not isinstance(output_assignment.expression, ir_expr.Pipeline):
        raise ValueError("the selected dot-product output is not a fixed pipeline")
    if output_assignment.expression.stages != args.latency:
        raise ValueError(
            f"reported latency {args.latency} does not match source pipeline "
            f"latency {output_assignment.expression.stages}"
        )
    rtl = emit_experimental(compilation.ir)
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as executor:
        futures = [executor.submit(
            _run, name, rtl, use_dsp, cascade, retiming,
            args.output, args.vivado, args.top, args.part, args.period_ns,
            args.latency,
        ) for name, (use_dsp, cascade, retiming) in VARIANTS.items()]
        results = [future.result() for future in futures]
    payload = {"tool": "Vivado 2024.2", "results": sorted(results, key=lambda item: item["variant"])}
    (args.output / "results.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
