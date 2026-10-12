"""Shared Vivado TCL for routed DSP48E1 QoR measurement tools."""

from __future__ import annotations

from pathlib import Path


def routed_dsp48e1_tcl(
    rtl: Path,
    xdc: Path,
    work: Path,
    top: str,
    part: str,
    period: float,
    *,
    include_bram: bool = True,
) -> str:
    """Build the common route-and-measure script used by DSP QoR runners."""

    bram_measurement = (
        "set bram [llength [get_cells -hier -filter {REF_NAME =~ RAMB*}]]\n"
        if include_bram
        else ""
    )
    bram_output = 'puts $output "bram=$bram"\n' if include_bram else ""
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
{bram_measurement}set configs {{}}
set locations {{}}
foreach cell $dsp_cells {{
  lappend configs "[get_property AREG $cell]/[get_property BREG $cell]/[get_property DREG $cell]/[get_property MREG $cell]/[get_property PREG $cell]"
  lappend locations [get_property LOC $cell]
}}
set output [open {{{work / 'metrics.txt'}}} w]
puts $output "lut=$lut"
puts $output "ff=$ff"
puts $output "dsp=$dsp"
{bram_output}puts $output "wns_ns=$wns"
puts $output "fmax_mhz=$fmax"
puts $output "dsp_configs=[join $configs ,]"
puts $output "dsp_locations=[join $locations ,]"
close $output
write_checkpoint -force {{{work / 'routed.dcp'}}}
exit
"""
