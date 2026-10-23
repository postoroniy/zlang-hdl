<a id="reference-low-level-target-resource-library"></a>
# Target resources


This low-level layer describes legal resources and physical bindings; it does
not itself select a design. The separate accepted
[high-level target-aware planner](high-level-target-aware-architecture-pipeline-planner.md#reference-high-level-target-aware-architecture-pipeline-planner)
automatically selects the bounded symmetric-FIR and signed-product candidates
documented there. Bounded ported-memory planning can match an exact advertised
memory shape, replicate 1R1W resources for 1W+nR, or use a bounded register/mux
fallback. Automatic banking and clock-resource planning remain unsupported.

<a id="reference-low-level-target-resource-library-implemented-model"></a>
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
and Direct-SV semantics unchanged.

<a id="reference-low-level-target-resource-library-dsp48e1-manual-pipeline-validation"></a>
## DSP48E1 pipeline binding

The bounded symmetric FIR maps through source-described `DSP48E1` resources.
Its semantic sites are:

```text
input_preadd -> multiply -> accumulate_output
```

The physical binding maps them to A/B/D registers, MREG, and terminal PREG.
Only configurations with compatible typed latency, II, and target capabilities
are eligible. Routed measurements, if requested by policy, must match the exact
physical graph and constraints. Separate target-resource validation records
contain measured configurations;
their QoR numbers are not portable language semantics or implicit guarantees
for a different design.

<a id="reference-low-level-target-resource-library-signed-product-cascade-validation"></a>
## Signed-product cascade

The same source-defined DSP48E1 binding now also covers the ordered
`SignedProductReduction` graph used by the FFT scalar real and imaginary
components.  The Direct-SV emitter consumes the per-resource
`accumulator_plus_product`/`accumulator_minus_product` configuration and emits
ALUMODE controls without rewriting the finite-width expression.  The final
fixed-point conversion remains one typed boundary after the cascade.

Routed evidence, graph identities, and tool invocations belong in separate
target-resource validation records.
Evidence for another packed ABI, target, constraint, or graph is not reused.

<a id="reference-low-level-target-resource-library-ramb36-validation"></a>
## RAMB36 validation

`examples/target_bram_memory.zhl` is an ordinary 1024x36, one-cycle,
read-first synchronous memory with independent read/write addresses. Generic
Direct-SV emits ordinary RAM RTL. The manual binding
`Xilinx7BRAM36SimpleDualPort` consumes one RAMB36 capability and emits the
same semantic template with block-memory physical binding.

The binding requires the resource's exact read-first collision contract;
write-first bypass across independent addresses is not silently reinterpreted
as a simple-dual-port primitive. Generic RTL may still be selected under an
appropriate fallback policy. Resource inference measurements belong to target
validation reports rather than the language contract.

Named true-dual selection is a distinct RTL shape. Generic same-clock
multiport storage retains one deterministic process, but the selected Xilinx
2RW route requires `contents preserve` and emits one physical process per
port. A uniform `init VALUE` is preserved as a power-up/bitstream
initialization under that reset policy. Native inference of a resource in a
particular synthesis run does not upgrade a cross-clock collision model to an
exact vendor guarantee.

The manually selected FIFO-only architectures `Xilinx7AsyncFifoRAMB18` and
`Xilinx7AsyncFifoRAMB36` require the compiler-owned registered-read
`async_fifo` decomposition, an independent-clock 1W1R port shape, exactly
one read cycle and `DO_REG=0`. They do not select a public `async_mem`.
Its physical plan owns ordered, width-aware pointer, Gray, full, ready/valid
and prefetch equations. The Direct-SV emitter renders those equations and the
digital simulator evaluates the same equations. Separate target-resource
validation records hold synthesis observations; physical clock/reset and
metastability requirements remain implementation responsibilities.

The live resource capability schema now distinguishes `simple_dual`,
same-clock `true_dual`, and independent-clock `1W1R`; it also separates
same-clock from cross-clock collision guarantees. A legacy `true_dual` flag is
not sufficient evidence for an asynchronous collision contract. The current
7-Series data intentionally advertises no exact cross-clock old/new guarantee,
so strict target-required `async_mem` publication fails closed while generic
Direct-SV remains an explicitly labelled structural digital model.

<a id="reference-low-level-target-resource-library-intel-and-asic-status"></a>
## Intel and ASIC status

Cyclone V source data describes variable-precision multiplier modes, 64-bit
accumulation, chain connectivity, generic pipeline sites/configurations, M10K
port/width modes, ALM/FF distinctions, and a fractional PLL resource.  The same
generic legality checks used for RAMB/MMCM validate M10K/PLL requests.

Intel primitive emission is unsupported: a selected Intel physical binding
fails closed with `unsupported`. The current release publishes no Quartus-backed
synthesis or QoR validation for this target.
The compiler-shipped `sky130_fd_sc_hd` profile identifies the real high-density
standard-cell library plus its documented positive-edge DFF, active-low-reset
DFF, and integrated clock-gate cells. Direct-SV remains generic RTL for
downstream Liberty mapping. SKY130 SRAMs, PLLs, die-area inventory, PVT corner,
timing closure, and physical signoff are deliberately not fabricated by the
compiler; project macro libraries and the ASIC implementation flow own them.

Emit technology-neutral RTL carrying the SKY130 target identity with:

```bash
zlang design.zhl --top Top --target sky130-fd-sc-hd \
  --systemverilog build/Top.sv
```

The downstream synthesis/P&R invocation must still supply the selected
`sky130_fd_sc_hd` Liberty, LEF and process configuration.

<a id="reference-low-level-target-resource-library-clock-resource-blocker"></a>
## Clock-resource blocker

The target libraries can describe clock input/output ranges, output counts,
phase capability, and lock/reset metadata.  Current functional ZLang clock
domains do not express a frequency/phase relationship from which a truthful
PLL/MMCM configuration could be derived.  Physical clock emission therefore
remains unsupported: there is no source-level frequency/phase requirement
from which to select a configuration. Raw PLL generics are not exposed in
normal modules.

<a id="reference-low-level-target-resource-library-backendartifact-implementation-manifest-v7"></a>
## Implementation artifact contents

Implementation manifests now retain selected pipeline configuration, active
semantic sites, physical-binding identities, and separate intended, emitted,
and measured resource-count slots plus timing provenance.  Existing semantic
bindings remain independent.  Current compiler-produced artifacts populate
intended/emitted counts after successful physical emission; external vendor
tools own measured counts and routed timing.

Automatic target-aware planning is enabled only for the reviewed symmetric-FIR
and ordered signed-product reduction regions documented in the
[Target-aware architecture and pipeline planning](high-level-target-aware-architecture-pipeline-planner.md#reference-high-level-target-aware-architecture-pipeline-planner).
General `explore`, BRAM/clock selection, and arbitrary resource ranking remain
disabled. The accepted boundary is documented in the
[Target-aware architecture and pipeline planning](high-level-target-aware-architecture-pipeline-planner.md#reference-high-level-target-aware-architecture-pipeline-planner).
