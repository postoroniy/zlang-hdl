# Passive taps for vendor FPGA debug tools

`vendor_debug.zhl` uses ordinary `RvTee<u16>` wiring and exposes explicit debug
outputs. The functional stream has no additional buffer or latency. It passes
even while reset is asserted; reset only clears the example's accepted-item
counter. The debug branch has no `ready` input and cannot stall the stream.

Generate the DUT and run the short stall/transfer example:

```sh
mkdir -p build/fpga-debug
.venv/bin/zlang examples/fpga_debug/vendor_debug.zhl --top VendorDebugDut \
  --systemverilog build/fpga-debug/VendorDebugDut.sv
.venv/bin/zlang sim examples/fpga_debug/vendor_debug.zhl --top VendorDebugDut \
  --events examples/fpga_debug/vendor_debug.events.jsonl --json
```

The source holds payload `0x1234` across two stalled cycles, then accepts it and
`0xABCD`. After the final edge `debug_count` is 2. `debug_transfer` means
`input.valid & output.ready`; observing a payload alone does not mean a transfer.

## AMD/Xilinx: Vivado ILA

Add the generated DUT and `amd_ila_top.sv` to your board project. Use
`AmdIlaTop` as the FPGA top. Generate a **native ILA** named `vendor_debug_ila`
in the Vivado IP catalog with `probe0` width 16, `probe1` width 3 and `probe2`
width 16. Select sample depth and trigger resources in Vivado. Add your board's
clock/pin constraints and generated IP products to that project.

`probe1` packs `{valid, ready, transfer}` from MSB to LSB. The wrapper samples
with the same `clk` as the stream and counter. Run synthesis/implementation,
generate the bitstream and probes file, then use Vivado Hardware Manager for
programming, trigger and acquisition. Keep the matching bitstream and probes
file together. The `mark_debug` attributes identify observation nets; they do
not generate an ILA by themselves. See
[AMD UG908](https://docs.amd.com/r/2024.2-English/ug908-vivado-programming-debugging/Marking-HDL-Signals-for-Debug).

The vendor-generated `vendor_debug_ila` definition is intentionally absent.
Compiling this wrapper without that IP must report an unresolved module; no
empty production stub is supplied.

## Altera/Intel: Quartus Signal Tap

Add the generated DUT and `intel_signaltap_top.sv` to your Quartus project and
select `IntelSignalTapTop`. The wrapper uses the Quartus Prime **Standard
Edition** `keep` attribute on combinational debug nets. `preserve` is for
registers; this wrapper does not insert observation registers.

Run Analysis & Elaboration, create a Signal Tap `.stp` instance, select `clk`
as acquisition clock, and use Node Finder to add the internal `debug_*` nets
and their exact bit widths. Configure triggers and sample depth, enable the
`.stp` file in the project, compile and use the Quartus programmer/Signal Tap.
Check the compiled node list: aliases and hierarchy names may change during
synthesis. Do not select package output pins as post-fit probe nodes.
See [Quartus Prime Standard signal preservation](https://docs.altera.com/r/docs/683552/18.1/intel-quartus-prime-standard-edition-user-guide-debug-tools/signal-preservation).

Quartus Pro's `preserve_for_debug` workflow additionally requires its matching
project setting; do not assume Standard and Pro settings are interchangeable.
This example does not require or claim an Intel primitive mapping by ZLang:
Quartus synthesizes the ordinary generated RTL for the part in your project.

## Contracts and qualification

The two wrappers keep debug signals **inside** the board-level FPGA top;
they are not additional package pins. If the generated DUT itself is selected
as the FPGA top, all its declared debug outputs are real top-level ports.

`Tee`/`RvTee` do not preserve nets against downstream optimization. Use vendor
preservation and verify the selected nodes. No implicit CDC, pipeline, sampling
register or resampling is added. Use a suitable clock for every analyzer group;
capturing unrelated asynchronous domains needs a separate design decision.
Extra fanout, preservation and analyzer cores can increase resources and affect
timing. A passive logical tap does not guarantee unchanged routed timing.

Native/RTL tests and actual-RTL passthrough proofs qualify the recorded ZLang
specializations and wrapper wiring. Passive-tap regressions and wrapper checks are available in this source tree. A test-only input monitor used for wrapper
checks is not an ILA implementation. Vendor project compilation, `.bit`/`.sof`
generation and physical board capture are **UNKNOWN** until run with the actual
part, tool version, generated IP and board. There is no bundled capture engine,
OpenOCD client, custom JTAG protocol, automatic instrumentation or ASIC DFT in
this example. Vendor software owns acquisition and viewing.
