#!/usr/bin/env python3
"""Reproducible Vivado QoR study for the manual fixed-point FIR variants."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import shutil
import subprocess
import time

from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples" / "fixed_fir_architectures.zhl"
VARIANTS = {
    "FixedFIRLinear": (1, 1),
    "FixedFIRBalanced": (1, 1),
    "FixedFIRPipelinedTree": (3, 1),
    "FixedFIRDspOriented": (2, 1),
}


@dataclass(frozen=True)
class Result:
    architecture: str
    backend: str
    part: str
    period_ns: float
    latency: int
    ii: int
    dsp: int
    lut: int
    ff: int
    bram: int
    wns_ns: float
    fmax_mhz: float
    compile_seconds: float
    rtl_bytes: int
    rtl_lines: int


def _tcl(top: str, files: list[Path], part: str, period: float, work: Path) -> str:
    reads = "\n".join(
        f"read_verilog {'-sv ' if path.suffix == '.sv' else ''}{{{path}}}"
        for path in files
    )
    return f"""
{reads}
synth_design -top {top} -part {part}
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
set dsp [llength [get_cells -hier -filter {{REF_NAME =~ DSP*}}]]
set bram [llength [get_cells -hier -filter {{REF_NAME =~ RAMB*}}]]
set output [open {{{work / 'metrics.txt'}}} w]
puts $output "lut=$lut"
puts $output "ff=$ff"
puts $output "dsp=$dsp"
puts $output "bram=$bram"
puts $output "wns_ns=$wns"
puts $output "fmax_mhz=$fmax"
close $output
write_checkpoint -force {{{work / 'routed.dcp'}}}
exit
"""


def _metrics(path: Path) -> dict[str, str]:
    return dict(line.strip().split("=", 1) for line in path.read_text().splitlines())


def _run_one(
    architecture: str,
    backend: str,
    files: list[Path],
    output: Path,
    vivado: str,
    part: str,
    period: float,
) -> Result:
    work = output / architecture / backend
    work.mkdir(parents=True, exist_ok=True)
    script = work / "run.tcl"
    script.write_text(_tcl(architecture, files, part, period, work))
    started = time.monotonic()
    completed = subprocess.run(
        (vivado, "-mode", "batch", "-nojournal", "-nolog", "-source", str(script)),
        cwd=work, capture_output=True, text=True,
    )
    elapsed = time.monotonic() - started
    (work / "vivado.stdout.log").write_text(completed.stdout)
    (work / "vivado.stderr.log").write_text(completed.stderr)
    if completed.returncode != 0 or not (work / "metrics.txt").exists():
        raise RuntimeError(
            f"Vivado failed for {architecture}/{backend}; see {work}"
        )
    measured = _metrics(work / "metrics.txt")
    latency, ii = VARIANTS[architecture]
    return Result(
        architecture, backend, part, period, latency, ii,
        int(measured["dsp"]), int(measured["lut"]), int(measured["ff"]),
        int(measured["bram"]), float(measured["wns_ns"]),
        float(measured["fmax_mhz"]), elapsed,
        sum(path.stat().st_size for path in files),
        sum(len(path.read_text().splitlines()) for path in files),
    )


def _markdown(results: list[Result]) -> str:
    rows = [
        "# Fixed FIR vendor QoR",
        "",
        "All rows implement the same full-precision eight-tap accumulation and one final nearest-even/saturating `fixed<16,14>` quantization.",
        "",
        "| Architecture | Backend | DSP | LUT | FF | BRAM | Latency | II | Fmax MHz | WNS ns | Compile s | RTL bytes/lines |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in sorted(results, key=lambda value: (value.architecture, value.backend)):
        rows.append(
            f"| {item.architecture} | {item.backend} | {item.dsp} | {item.lut} | "
            f"{item.ff} | {item.bram} | {item.latency} | {item.ii} | "
            f"{item.fmax_mhz:.2f} | {item.wns_ns:.3f} | {item.compile_seconds:.1f} | "
            f"{item.rtl_bytes}/{item.rtl_lines} |"
        )
    return "\n".join(rows) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vivado", default=shutil.which("vivado") or "vivado")
    parser.add_argument("--part", default="xc7z030ffg676-1")
    parser.add_argument("--period-ns", type=float, default=10.0)
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[str, str, list[Path]]] = []
    for architecture in VARIANTS:
        compilation = compile_source(SOURCE.read_text(), top=architecture)
        direct_dir = args.output / "rtl" / architecture / "direct_sv"
        direct_dir.mkdir(parents=True, exist_ok=True)
        direct = direct_dir / f"{architecture}.sv"
        direct.write_text(
            emit_sv_artifact(compilation.ir, selected_ir_identity=architecture).text
        )
        jobs.append((architecture, "direct_systemverilog", [direct]))

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as executor:
        futures = [
            executor.submit(
                _run_one, architecture, backend, files, args.output,
                args.vivado, args.part, args.period_ns,
            )
            for architecture, backend, files in jobs
        ]
        results = [future.result() for future in futures]
    (args.output / "results.json").write_text(
        json.dumps([asdict(item) for item in results], indent=2, sort_keys=True) + "\n"
    )
    (args.output / "results.md").write_text(_markdown(results))
    print(_markdown(results), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
