# Low-level target/resource library

This document records the implemented unnumbered low-level substrate.
This low-level layer describes legal resources and physical bindings; it does
not itself select a design. The separate accepted
[high-level target-aware planner](high-level-target-aware-architecture-pipeline-planner.md)
automatically selects the bounded symmetric-FIR and signed-product candidates
documented there. General graph covering and automatic storage/clock-resource
planning remain outside the supported slice.

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
| unregistered | none | 1 | all `0/0/0/0/0` | 4 | 234 | 16 | -3.869 | 72.10 | input through 4 DSP + carry/quantize |
| multiply registered | multiply | 2 | all `0/0/0/1/0` | 4 | 233 | 16 | -2.469 | MREG through 3 DSP + carry/quantize |
| multiply + output | multiply, accumulate_output | 3 | MREG all, PREG terminal | 4 | 233 | 16 | +0.748 | registered result to output |
| fully pipelined | input_preadd, multiply, accumulate_output | 4 | A/B/D/M all, P terminal | 4 | 233 | 16 | +0.748 | registered result to output |

All four cascades occupy four adjacent DSP48 sites.  The two configurations
with terminal PREG cross 100 MHz; merely enabling MREG does not.  Fully
pipelined and multiply+output have identical routed timing in this design,
showing that the terminal accumulation boundary, not blindly adding every
input register, is the decisive cut for this constraint.

The `tools/dsp48e1_pipeline_qor.py` runner checks the selected graph latency,
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
the public leaf-ABI change invalidated their physical graph identities.  Each
uses two DSP48E1, 227 LUT, 18 FF and zero BRAM.  Unregistered real/imag forms
reach 80.032/85.911 MHz; multiply-registered forms both reach 105.585 MHz;
multiply-output and fully-pipelined forms both reach 107.631 MHz.  Their
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
