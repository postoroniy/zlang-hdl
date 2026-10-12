#!/usr/bin/env python3
"""Bounded, matched routed timing experiment for the mathematical tutorial.

This measures generic direct-SV implementations, not compiler estimates or a
formal proof. Identical launch/capture registers isolate the arithmetic paths.
Out-of-context synthesis avoids imposing a package pinout on this core study.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

from zlang.backend.systemverilog import emit_artifact
from zlang.compiler import compile_source
from zlang.ir.module import PortDirection
from zlang.ir.types import UIntType
from zlang.timing import timing_info


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples/verification/math_exploration.zhl"
TOPS = ("MathOneCycle", "MathArchitecture", "MathExplore")
SCHEMA = "zlang-math-exploration-routed-v1"


def digest(text: str) -> str:
    return sha256(text.encode()).hexdigest()


def build_shell(module) -> str:
    """Wrap the frozen scalar kernel without inspecting or rewriting its RTL."""
    if module.clock is None or module.reset is None:
        raise ValueError("the timing kernel must declare clock and reset")
    ports = tuple(module.ports)
    if any(not isinstance(port.type, UIntType) for port in ports):
        raise ValueError("the timing shell supports unsigned scalar data ports only")
    inputs = tuple(port for port in ports if port.direction is PortDirection.INPUT)
    outputs = tuple(port for port in ports if port.direction is PortDirection.OUTPUT)
    if not inputs or len(outputs) != 1:
        raise ValueError("the timing shell requires inputs and one output")
    lines = ["`default_nettype none", "module MathTimingShell (",
             "  input wire logic clk,", "  input wire logic rst,"]
    for port in inputs:
        lines.append(f"  input wire logic [{port.type.width - 1}:0] {port.name},")
    result = outputs[0]
    lines.extend((f"  output logic [{result.type.width - 1}:0] {result.name}", ");"))
    for port in inputs:
        lines.append(f"  logic [{port.type.width - 1}:0] launch_{port.name};")
    lines.append(f"  wire [{result.type.width - 1}:0] core_result;")
    connections = [".clk(clk)", ".rst(rst)"]
    connections.extend(f".{port.name}(launch_{port.name})" for port in inputs)
    connections.append(f".{result.name}(core_result)")
    lines.extend((f"  {module.name} core ({', '.join(connections)});",
                  "  always_ff @(posedge clk) begin", "    if (rst) begin"))
    lines.extend(f"      launch_{port.name} <= '0;" for port in inputs)
    lines.extend((f"      {result.name} <= '0;", "    end else begin"))
    lines.extend(f"      launch_{port.name} <= {port.name};" for port in inputs)
    lines.extend((f"      {result.name} <= core_result;", "    end", "  end",
                  "endmodule", "`default_nettype wire", ""))
    return "\n".join(lines)


def build_tcl(period_ns: float, part: str) -> str:
    # All filenames are fixed relative to a dedicated run directory. The same
    # constraints and physical flow apply byte-for-byte to every variant.
    return f"""set_param general.maxThreads 2
read_verilog -sv kernel.sv
read_verilog -sv shell.sv
read_xdc timing.xdc
synth_design -top MathTimingShell -part {part} -mode out_of_context -flatten_hierarchy none
opt_design
place_design
phys_opt_design
route_design
report_utilization -file utilization.rpt
report_timing_summary -delay_type min_max -report_unconstrained -file timing.rpt
report_timing -delay_type max -max_paths 10 -path_type full_clock_expanded -file setup_paths.rpt
report_timing -delay_type min -max_paths 10 -path_type full_clock_expanded -file hold_paths.rpt
check_timing -verbose -file check_timing.rpt
report_route_status -file route_status.rpt
report_clocks -file clocks.rpt
write_checkpoint -force routed.dcp
{build_measurement_tcl(period_ns)}
exit
"""


def build_measurement_tcl(period_ns: float, metrics_file: str = "metrics.txt") -> str:
    """Measure only physically routed register paths; retain I/O diagnostics.

    OOC boundary ports have no package/partition pin positions. Their timing
    cannot validate the core, even with explicitly declared input/output delays.
    No path is cut: this selects the reported observations, not timing exceptions.
    """
    return f"""set boundary_setup [get_timing_paths -setup -max_paths 1]
set boundary_hold [get_timing_paths -hold -max_paths 1]
set regs [all_registers]
set setup [get_timing_paths -setup -from $regs -to $regs -max_paths 1]
set hold [get_timing_paths -hold -from $regs -to $regs -max_paths 1]
if {{[llength $setup] != 1 || [llength $hold] != 1}} {{ error "missing register-to-register setup/hold path" }}
report_timing -from $regs -to $regs -delay_type max -max_paths 10 -path_type full_clock_expanded -file internal_setup_paths.rpt
report_timing -from $regs -to $regs -delay_type min -max_paths 10 -path_type full_clock_expanded -file internal_hold_paths.rpt
set wns [get_property SLACK $setup]
set whs [get_property SLACK $hold]
set critical [expr {{{period_ns} - $wns}}]
set fmax [expr {{$critical > 0.0 ? 1000.0 / $critical : 0.0}}]
set output [open {metrics_file} w]
puts $output "tool_version=[version -short]"
puts $output "lut=[llength [get_cells -hier -filter {{REF_NAME =~ LUT*}}]]"
puts $output "ff=[llength [get_cells -hier -filter {{REF_NAME =~ FD*}}]]"
puts $output "dsp=[llength [get_cells -hier -filter {{REF_NAME =~ DSP*}}]]"
puts $output "bram=[llength [get_cells -hier -filter {{REF_NAME =~ RAMB*}}]]"
puts $output "wns_ns=$wns"
puts $output "whs_ns=$whs"
puts $output "critical_delay_proxy_ns=$critical"
puts $output "fmax_proxy_mhz=$fmax"
puts $output "register_to_register_wns_ns=$wns"
puts $output "all_paths_wns_ns=[get_property SLACK $boundary_setup]"
puts $output "all_paths_whs_ns=[get_property SLACK $boundary_hold]"
puts $output "critical_startpoint=[get_property STARTPOINT_PIN $setup]"
puts $output "critical_endpoint=[get_property ENDPOINT_PIN $setup]"
puts $output "critical_datapath_delay_ns=[get_property DATAPATH_DELAY $setup]"
puts $output "clock_count=[llength [get_clocks]]"
close $output
"""


def build_xdc(period_ns: float) -> str:
    return f"""create_clock -name clk -period {period_ns} [get_ports clk]
set data_inputs [get_ports -filter {{DIRECTION == IN && NAME != clk}}]
set_input_delay 0.0 -clock clk $data_inputs
set_output_delay 0.0 -clock clk [all_outputs]
"""


def parse_metrics(text: str) -> dict[str, object]:
    raw = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    result: dict[str, object] = dict(raw)
    for key in ("lut", "ff", "dsp", "bram", "clock_count"):
        result[key] = int(raw[key])
    for key in ("wns_ns", "whs_ns", "critical_delay_proxy_ns", "fmax_proxy_mhz",
                "register_to_register_wns_ns", "critical_datapath_delay_ns"):
        value = float(raw[key])
        if not math.isfinite(value):
            raise ValueError(f"nonfinite timing metric: {key}")
        result[key] = value
    if result["clock_count"] != 1:
        raise ValueError("expected exactly one constrained clock")
    result["setup_pass"] = result["wns_ns"] >= 0.0
    result["hold_pass"] = result["whs_ns"] >= 0.0
    result["timing_pass"] = result["setup_pass"] and result["hold_pass"]
    return result


def validate_route_reports(work: Path) -> dict[str, int]:
    route = (work / "route_status.rpt").read_text()
    timing = (work / "check_timing.rpt").read_text()
    patterns = {
        "routing_errors": (route, r"# of nets with routing errors\.+\s*:\s*(\d+)"),
        "unconstrained_internal_endpoints": (timing, r"checking unconstrained_internal_endpoints \((\d+)\)"),
        "unclocked_registers": (timing, r"checking no_clock \((\d+)\)"),
    }
    result = {}
    for key, (report, pattern) in patterns.items():
        match = re.search(pattern, report)
        if match is None or int(match[1]) != 0:
            raise ValueError(f"missing or nonzero routed timing check: {key}")
        result[key] = int(match[1])
    return result


def run_one(top: str, source: str, output: Path, vivado: str, part: str,
            period_ns: float, timeout: int) -> dict[str, object]:
    work = output / top
    work.mkdir()  # Refuse to overwrite/relabel any preceding measurement.
    compiled = compile_source(source, top=top)
    artifact = emit_artifact(compiled.ir)
    shell = build_shell(compiled.ir)
    assignment = next(item for item in compiled.ir.assignments if item.target.name == "y")
    latency = timing_info(assignment.expression, module=compiled.ir).latency
    texts = {"source.zhl": source, "kernel.sv": artifact.text, "shell.sv": shell,
             "run.tcl": build_tcl(period_ns, part), "timing.xdc": build_xdc(period_ns),
             "exploration.txt": compiled.exploration_report,
             "architecture.txt": compiled.architecture_report}
    for name, value in texts.items():
        (work / name).write_text(value)
    row: dict[str, object] = {
        "top": top, "part": part, "clock_period_ns": period_ns,
        "core_latency": latency, "common_shell_latency": 2,
        "total_sample_latency": latency + 2, "ii": 1,
        "backend": "direct_systemverilog", "flow": "out_of_context_routed",
        "timing_scope": "register-to-register; OOC boundary-port timing excluded from acceptance",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "sha256": {name: digest(value) for name, value in texts.items()},
        "status": "running", "work_directory": str(work),
    }
    (work / "result.json").write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
    started = time.monotonic()
    with (work / "vivado.stdout.log").open("w") as stdout, \
            (work / "vivado.stderr.log").open("w") as stderr:
        process = subprocess.Popen(
            (vivado, "-mode", "batch", "-nojournal", "-nolog", "-source", "run.tcl"),
            cwd=work, stdout=stdout, stderr=stderr, start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            row["status"] = "timeout"
        else:
            row["returncode"] = returncode
            row["status"] = "tool_failed"
            if returncode == 0 and (work / "metrics.txt").exists():
                try:
                    row.update(parse_metrics((work / "metrics.txt").read_text()))
                    row.update(validate_route_reports(work))
                    row["status"] = "routed"
                except ValueError as error:
                    row["status"] = "invalid_measurement"
                    row["error"] = str(error)
    row["elapsed_seconds"] = round(time.monotonic() - started, 3)
    row["finished_utc"] = datetime.now(timezone.utc).isoformat()
    (work / "result.json").write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
    print(f"{top}: {row['status']} WNS={row.get('wns_ns')} ns", flush=True)
    return row


def audit_checkpoint(top: str, output: Path, vivado: str) -> dict[str, object]:
    """Re-query a preserved routed checkpoint without changing implementation.

    This also permits auditing an earlier runner's boundary-inclusive timing
    summary. Original result/metrics files are retained, never relabeled.
    """
    work = output / top
    original = (work / "result.json").read_text()
    row = json.loads(original)
    if row["status"] != "routed":
        raise ValueError(f"{top} has no completed routed result")
    for name, expected in row["sha256"].items():
        if digest((work / name).read_text()) != expected:
            raise ValueError(f"changed measurement input: {top}/{name}")
    checkpoint = work / "routed.dcp"
    checkpoint_hash = sha256(checkpoint.read_bytes()).hexdigest()
    script = "open_checkpoint routed.dcp\n" + build_measurement_tcl(
        row["clock_period_ns"], "metrics.registers.txt",
    ) + "exit\n"
    script_path = work / "audit_registers.tcl"
    if script_path.exists():
        raise ValueError(f"refusing to overwrite existing checkpoint audit: {top}")
    script_path.write_text(script)
    with (work / "audit.stdout.log").open("w") as stdout, \
            (work / "audit.stderr.log").open("w") as stderr:
        subprocess.run(
            (vivado, "-mode", "batch", "-nojournal", "-nolog", "-source", script_path.name),
            cwd=work, stdout=stdout, stderr=stderr, check=True, timeout=60,
        )
    if sha256(checkpoint.read_bytes()).hexdigest() != checkpoint_hash:
        raise ValueError(f"checkpoint unexpectedly changed: {top}")
    row.update(parse_metrics((work / "metrics.registers.txt").read_text()))
    row.update(validate_route_reports(work))
    row["timing_scope"] = "register-to-register; OOC boundary-port timing excluded from acceptance"
    row["checkpoint_sha256"] = checkpoint_hash
    row["original_run_result_sha256"] = digest(original)
    row["checkpoint_audit_script_sha256"] = digest(script)
    row["checkpoint_audited_utc"] = datetime.now(timezone.utc).isoformat()
    (work / "result.registers.json").write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
    print(f"{top}: register WNS={row['wns_ns']} ns WHS={row['whs_ns']} ns", flush=True)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--vivado", default=shutil.which("vivado") or "vivado")
    parser.add_argument("--part", default="xc7z030ffg676-1", choices=("xc7z030ffg676-1",))
    parser.add_argument("--period-ns", type=float, default=10.0)
    parser.add_argument("--jobs", type=int, default=2, choices=(1, 2))
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--audit-existing", action="store_true",
                        help="re-query preserved routed checkpoints, with no synthesis/place/route")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 300:
        parser.error("timeout must be between 1 and 300 seconds")
    if not math.isfinite(args.period_ns) or args.period_ns <= 0:
        parser.error("period must be positive and finite")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    version = subprocess.run((args.vivado, "-version"), check=True, text=True,
                             capture_output=True, timeout=30).stdout.strip()
    source = args.source.read_text() if not args.audit_existing else ""
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        if args.audit_existing:
            futures = [executor.submit(audit_checkpoint, top, args.output, args.vivado) for top in TOPS]
        else:
            futures = [executor.submit(run_one, top, source, args.output, args.vivado,
                                       args.part, args.period_ns, args.timeout) for top in TOPS]
        rows = [future.result() for future in futures]
    payload = {"schema": SCHEMA, "tool_version_output": version, "results": rows,
               "measurement_scope": "core OOC placement/routing, common launch/capture registers; not board timing",
               "fmax_method": "1000 / (clock_period_ns - worst_register_setup_slack_ns); proxy, not frequency sweep",
               "formal_claim": "none; functional equivalence is checked separately"}
    name = "results.registers.json" if args.audit_existing else "results.json"
    (args.output / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0 if all(row["status"] == "routed" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
