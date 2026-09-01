# Target-platform architecture descriptions

This original four-DSP slice has now been generalized by the
[low-level target/resource library](low-level-target-resource-library.md).
The historical measurements below remain valid for the unregistered profile;
the current library uses generic pipeline-site names and implementation
manifest version 7. The later
[target-aware planner](high-level-target-aware-architecture-pipeline-planner.md)
adds bounded automatic selection; the manual flow below remains a reproducible
explicit-selection witness rather than the complete current planner surface.

ZLang keeps functional behavior, implementation architecture, and physical
target data separate. The first bounded implementation maps the ordinary
functional [symmetric FIR example](../examples/symmetric_fixed_fir.zl) to four
DSP48E1 resources on `xc7z030ffg676-1`. Without an explicit selection, the same
source continues through the unchanged generic Clash or direct-SV path.

## Compiler-shipped source descriptions

Target data is ordinary compiler-shipped ZLang source:

- `std.target.generic` defines the resource-free generic target;
- `std.target.xilinx.series7` defines the bounded DSP48E1 capability;
- `std.target.xilinx.xc7z030` defines the device part, inventory, and cascade
  capacity;
- `std.arch.xilinx7_fir` defines the one manual symmetric-FIR template;
- `std.target.toy_asic` and `std.arch.toy` prove that the core records are not
  FPGA- or Xilinx-specific.

The core parser and IR understand generic resources, typed ports, operation and
width limits, register sites, dedicated links, inventories, targets, and
architecture requirements. The names `DSP48E1`, `PCIN`, and `PCOUT`, and the
physical SystemVerilog binding all originate in the library definitions—not in
functional ZLang or semantic branches.

An exact source-level `timing { latency N ii 1 }` block is public module
behavior, not target-selection policy. Generic implementation graphs publish
their derived `timeless`/`known`/`unknown` timing class and are explicitly
backend-independent. A selected physical graph carries its realization backend
so a direct-SystemVerilog resource plan cannot be attributed to Clash. Target
selection must preserve any exact public module latency.

The bounded Series-7 profile describes a signed 25-bit pre-adder, signed 25x18
multiply, 43-bit product, 48-bit P/PCIN/PCOUT path, optional A/B/D/AD/M/C/P
register sites, and an adjacent/same-group 48-bit dedicated cascade. The
XC7Z030 instance records 400 DSP slices and four locally available cascade
positions; this is deliberately not a complete floorplan model.

## Manual selection

```bash
.venv/bin/zlangc examples/symmetric_fixed_fir.zl \
  --target xc7z030ffg676-1 \
  --target-architecture Xilinx7SymmetricDSPCascade \
  --target-architecture-mode required \
  --systemverilog build/SymmetricFixedFIR.sv \
  --implementation-manifest build/SymmetricFixedFIR.manifest.json
```

`required` turns every target, shape, width, inventory, dedicated-link, or
backend failure into a diagnostic. `preferred` returns the generic graph on a
mapping failure. `generic` and omission of target options preserve the existing
generic implementation. This original command performs explicit selection;
bounded automatic target-aware selection is documented separately in the
[high-level planner guide](high-level-target-aware-architecture-pipeline-planner.md).

## The selected graph

The matcher covers typed semantic structure only. It requires eight products,
four coefficient expression identities reused at mirrored sample indices, and
one final `nearest_even`/`saturate` conversion to `fixed<16,14>`. It does not use
module names, source spelling, runtime coefficient equality, or test vectors.

For `SF2.10` samples and coefficients the legality evidence is:

| Quantity | Required | DSP48E1 profile |
| --- | ---: | ---: |
| pair pre-add | 13 signed bits | 25 |
| coefficient/B | 12 signed bits | 18 |
| exact product | 25 signed bits | 43 |
| exact four-pair accumulation | 27 signed bits | 48 |

The resulting `ImplementationGraph` has four resource instances and three
`DedicatedPhysicalEdge` records:

```text
dsp0.PCOUT -> dsp1.PCIN -> dsp2.PCIN -> dsp3.PCIN
```

Each node maps the left/right sample expressions to A/D, the shared coefficient
to B, and its predecessor/result identities to PCIN/P/PCOUT. Every internal DSP
register site is explicitly zero in this first profile. The ordinary semantic
output register provides latency 1; II is 1. The complete 48-bit cascade result
then crosses the single original quantization boundary. No intermediate is
narrowed.

## Backend and manifest

The direct-SV emitter consumes the selected graph; it does not rediscover a FIR.
Its source-selected physical binding instantiates four Series-7 `DSP48E1`
primitives with `INMODE=00100`, `OPMODE=0010101`, `USE_DPORT=TRUE`, direct A/B,
and all selected internal register stages disabled. Actual `PCOUT` ports drive
the next primitives' actual `PCIN` ports. A distinct behavioral resource model
is used only for Verilator simulation.

BackendArtifact manifest version 5 retains the v4 semantic bindings and adds
target/family/template identities and hashes, dependency hashes, selection
policy, resource configuration and semantic mappings, dedicated edges,
latency/II, intended counts, and the emitted artifact hash. Intended resource
use is never presented as a Vivado measurement.

## Boundaries

Clash remains available for the generic implementation; it precisely does not
claim to realize the selected primitive graph. Existing M36 can validate the
semantic fixed-point region, but its reference emitter does not model vendor
primitives. Physical evidence therefore consists of the exact semantic oracle,
Verilator execution of the separate resource behavior model, and real Vivado
synthesis/place/route of the primitive artifact. No primitive-level formal
infrastructure was added.

Automatic target exploration was outside this original manual slice. The later
planner covers only its documented symmetric-FIR and signed-product regions;
general placement/graph covering, DSP48E2, Intel physical emission, automatic
storage/clock-resource mapping, and new formal machinery remain outside the
current bounded support.
