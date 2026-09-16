# Low-level target/resource library

This document records the implemented unnumbered low-level substrate.
This low-level layer describes legal resources and physical bindings; it does
not itself select a design. The separate accepted
[high-level target-aware planner](high-level-target-aware-architecture-pipeline-planner.md)
automatically selects the bounded symmetric-FIR and signed-product candidates
documented there. Bounded ported-memory planning can match an exact advertised
memory shape, replicate 1R1W resources for 1W+nR, or use a bounded register/mux
fallback. Automatic banking and clock-resource planning remain outside the
supported slice.

## Implemented model

Compiler-shipped ZLang source now defines generic, AMD 7-series, Intel Cyclone
V, and ASIC-compatible resources.  Resource definitions carry generic class,
operation, capabilities, typed ports/limits, semantic pipeline sites, legal
pipeline configurations, dedicated edges, and an opaque backend binding.

The loader validates duplicate/unknown pipeline sites, configuration latency,
II, memory port/width/capacity, clock input/output/output-count requirements,
dedicated edges, and target inventory.  The same parser accepts project-local
resource declarations; resolving project packages is deliberately deferred.

The generic library contains ordinary-RTL descriptions for logic, registers,
multiplier/add/MAC, FIFO storage, RAM/ROM, clock/control abstractions, and carry.
Selecting `generic`, or compiling without a target, leaves typed semantic IR
and direct-SV semantics unchanged.

## DSP48E1 manual pipeline validation

The existing four-DSP symmetric FIR is now mapped through source-defined
`DSP48E1` data.  Its semantic sites are:

```text
input_preadd -> multiply -> accumulate_output
```

The physical binding maps these to A/B/D registers, MREG, and terminal PREG;
those physical names do not occur in an architecture template or functional
module.  Four templates manually select four legal configurations.  Separate
ZLang sources provide matching total latency contracts, and the behavioral
DSP model passes the same bit-exact vectors for all four at II=1.

Vivado 2024.2 routed results on `xc7z030ffg676-1`, 10 ns constraint:

| Configuration | Active sites | Latency | DSP config A/B/D/M/P | DSP | LUT | FF | WNS ns | Fmax MHz | Critical path |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |
| unregistered | none | 1 | all `0/0/0/0/0` | 4 | 239 | 16 | -3.760 | 72.674 | input through 4 DSP + carry/quantize |
| multiply registered | multiply | 2 | all `0/0/0/1/0` | 4 | 240 | 16 | -1.940 | 83.752 | MREG through 3 DSP + carry/quantize |
| multiply + output | multiply, accumulate_output | 3 | MREG all, PREG terminal | 4 | 239 | 16 | +0.308 | 103.178 | registered result to output |
| fully pipelined | input_preadd, multiply, accumulate_output | 4 | A/B/D/M all, P terminal | 4 | 239 | 16 | +0.308 | 103.178 | registered result to output |

All four cascades occupy four adjacent DSP48 sites.  The two configurations
with terminal PREG cross 100 MHz; merely enabling MREG does not.  Fully
pipelined and multiply+output have identical routed timing in this design,
showing that the terminal accumulation boundary, not blindly adding every
input register, is the decisive cut for this constraint.

These rows were rerouted after the LSB-first packing migration. The
`tools/dsp48e1_pipeline_qor.py` runner checks the selected graph latency,
emits explicit primitives, queries actual A/B/D/M/P settings and locations,
and records routed QoR.  These are manual measurements, not planner costs.

## Signed-product cascade validation

The same source-defined DSP48E1 binding now also covers the ordered
`SignedProductReduction` graph used by the FFT scalar real and imaginary
components.  The direct-SV emitter consumes the per-resource
`accumulator_plus_product`/`accumulator_minus_product` configuration and emits
ALUMODE controls without rewriting the finite-width expression.  The final
fixed-point conversion remains one typed boundary after the cascade.

Eight real/imag variants were rerouted on the same target and constraint after
the LSB-first indexed-aggregate ABI change invalidated their physical graph
identities. Each
uses two DSP48E1, 227 LUT, 18 FF and zero BRAM.  Unregistered real/imag forms
reach 79.183/83.612 MHz; multiply-registered forms both reach 105.585 MHz;
multiply-output and fully-pipelined forms both reach 106.157 MHz.  Their
latencies are respectively 1/2/3/4 and II is 1.  Full rows, DSP register
properties, locations and graph identities are in
`zlang/data/xc7z030_signed_product_qor.json`; rerun them with
`tools/signed_product_pipeline_qor.py`.  Its explicit `--evidence-output`
option writes planner-ready evidence from the graph that was actually routed;
old measurements are never re-keyed onto a changed graph.

## RAMB36 validation

`examples/target_bram_memory.zhl` is an ordinary 1024x36, one-cycle,
read-first synchronous memory with independent read/write addresses.  Generic
direct-SV emits ordinary RAM RTL.  Manual `Xilinx7BRAM36SimpleDualPort`
selection consumes one RAMB36 capability and emits the same semantic template
with a block-memory physical binding.

Both variants pass Verilator write/read behavior.  Vivado reports:

| Implementation | RAMB36E1 | RAMB18E1 |
| --- | ---: | ---: |
| generic RTL inference | 1 | 0 |
| selected RAMB36 binding | 1 | 0 |

An initial write-first cross-port fixture measured zero BRAM and 768 distributed
RAM LUTs.  Vivado reported the block style infeasible because the ZLang
write-first bypass across independent addresses is not the selected primitive's
simple-dual-port collision contract.  The validation fixture therefore uses
the already-defined read-first semantics; the compiler did not weaken or
silently reinterpret the original mode.

Named true-dual selection is a distinct RTL shape. Generic same-clock
multiport storage retains one deterministic process, but the selected Xilinx
2RW route requires `contents preserve` and emits one physical process per
port. A 1024x9 fixture with uniform `init 0x12` was synthesized by Vivado
2024.2 for `xc7z030ffg676-1`; Vivado explicitly recognized a true-dual RAM
template and produced one RAMB18E1 (17 LUT / 20 FF around the RAM for reset,
enable, and same-address priority logic). The corresponding generic
independent-clock 1W1R structural witness also produced one RAMB18E1. That
inference result does not upgrade its cross-clock collision model to an exact
vendor guarantee.

The manually selected FIFO-only architectures `Xilinx7AsyncFifoRAMB18` and
`Xilinx7AsyncFifoRAMB36` require the compiler-owned registered-read
`async_fifo` decomposition, an independent-clock 1W1R port shape, exactly
one read cycle and `DO_REG=0`. They do not select a public `async_mem`.
Its physical plan owns ordered, width-aware pointer, Gray, full, ready/valid
and prefetch equations. The direct-SV emitter renders those equations and the
digital simulator evaluates the same equations; neither keeps an independent
combinational FIFO-controller recipe.
Vivado 2024.2 synthesis of a 1024×9 FIFO on `xc7z030ffg676-1`, with
asynchronous 10/7-ns clock groups, measured:

| FIFO RTL shape | LUT | FF | RAMB18E1 | Read-clock WNS | Write-clock WNS |
| --- | ---: | ---: | ---: | ---: | ---: |
| prior combinational array read | 275 | 100 | 0 | 3.937 ns | 6.830 ns |
| typed `async_mem` registered read + prefetch | 38 | 90 | 1 | 2.533 ns | 6.934 ns |

These are post-synthesis, unplaced numbers from the prior emitted RTL shape
and the new selected RTL, not post-route Fmax or silicon behavior. The new
binding substantially reduces LUT usage and infers block RAM; it does not
improve the read-domain critical path in this witness. Physical clock/reset
and metastability requirements remain implementation responsibilities.

The live resource capability schema now distinguishes `simple_dual`,
same-clock `true_dual`, and independent-clock `1W1R`; it also separates
same-clock from cross-clock collision guarantees. A legacy `true_dual` flag is
not sufficient evidence for an asynchronous collision contract. The current
7-Series data intentionally advertises no exact cross-clock old/new guarantee,
so strict target-required `async_mem` publication fails closed while generic
direct-SV remains an explicitly labelled structural digital model.

## Intel and ASIC status

Cyclone V source data describes variable-precision multiplier modes, 64-bit
accumulation, chain connectivity, generic pipeline sites/configurations, M10K
port/width modes, ALM/FF distinctions, and a fractional PLL resource.  The same
generic legality checks used for RAMB/MMCM validate M10K/PLL requests.

Quartus is not installed.  The Intel physical binding is recorded but direct-SV
fails closed with `unsupported`; no primitive, synthesis, or QoR claim is made.
ASIC multiplier/SRAM/PLL/standard-cell fixtures prove that no FPGA family enum
is required by the IR.  Their physical macros likewise remain project/backend
work.

## Clock-resource blocker

The target libraries can describe clock input/output ranges, output counts,
phase capability, and lock/reset metadata.  Current functional ZLang clock
domains do not express a frequency/phase relationship from which a truthful
PLL/MMCM configuration could be derived.  Physical clock emission therefore
remains unsupported.  A later review must add a generic clock requirement
model before any primitive is emitted; raw PLL generics will not be exposed in
normal modules.

## BackendArtifact implementation manifest v7

Implementation manifests now retain selected pipeline configuration, active
semantic sites, physical-binding identities, and separate intended, emitted,
and measured resource-count slots plus timing provenance.  Existing semantic
bindings remain independent.  Current compiler-produced artifacts populate
intended/emitted counts after successful physical emission; external vendor
tools own measured counts and routed timing.

## Reproduction

```bash
.venv/bin/python -m pytest -q \
  tests/test_low_level_resources.py \
  tests/test_target_architecture.py \
  tests/integration/test_xilinx_dsp_pipeline_configs.py \
  tests/integration/test_target_bram.py

.venv/bin/python tools/dsp48e1_pipeline_qor.py \
  --output /tmp/zlang-dsp-pipelines --vivado /path/to/vivado

.venv/bin/python tools/target_bram_vendor_qor.py \
  --output /tmp/zlang-bram --vivado /path/to/vivado
```

Automatic target-aware planning is enabled only for the reviewed symmetric-FIR
and ordered signed-product reduction regions documented in the
[high-level planner guide](high-level-target-aware-architecture-pipeline-planner.md).
General `explore`, BRAM/clock selection, and arbitrary resource ranking remain
disabled. The accepted boundary is documented in the
[high-level planner guide](high-level-target-aware-architecture-pipeline-planner.md).
