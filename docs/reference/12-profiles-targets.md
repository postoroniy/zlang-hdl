<a id="reference-profiles-targets"></a>
## Profiles, targets, and physical implementation

Implementation profiles keep physical policy outside portable `.zhl` source.
They are named tables in `zlang.toml` and are selected explicitly:

```toml
[profiles.release]
backend = "systemverilog"
backend-mode = "required"
target = "xc7z030ffg676-1"
allowed-transforms = ["pipeline", "dsp", "reduction"]
avoided-transforms = ["reassociate"]
objective = "maximize fmax"
architecture = "Xilinx7SymmetricDSPCascade"
architecture-mode = "preferred"
evidence-policy = "measured_required"
formal-policy = "required_bmc"

[profiles.release.constraints]
latency = { maximum = 8 }
ii = { exact = 1 }
fmax = { minimum = 100 }
dsp = { maximum = 8 }
```

`maximize fmax` prefers a legal choice with the strongest compatible frequency
measurement available under the selected evidence policy. It does not
guarantee the frequency achieved on an FPGA or ASIC. An estimate is never
reported as measured routed timing.

Schematic use inside a project containing the shown `src/fir.zhl` and profile:

```bash
zlang src/fir.zhl --profile release --systemverilog build/Fir.sv
```

A profile may select pinned scalar external-module mappings with
`external-mappings = ["vendor-add"]`.  Mapping declarations live in the
top-level `external-mappings` tables of `zlang.toml`; their source files and
hashes are fixed by `zlang.lock`. They may change generated SystemVerilog but
do not change the source-language behavior of the design.

Only the selected profile is parsed strictly. This permits a project to carry
profiles for newer compilers without making unrelated builds fail. Selecting
an unknown profile, selecting a profile without a project, or using an unknown
key is an error. Profile data is excluded from dependency resolution and lock
contents.


ZLang keeps functional behavior, implementation architecture, and physical
target data separate. Explicit selection maps the ordinary
functional [symmetric FIR example](../../examples/symmetric_fixed_fir.zhl) to four
DSP48E1 resources on `xc7z030ffg676-1`. Without an explicit selection, the same
source continues through the generic Direct-SV path. Bounded automatic
selection is described under
[target-aware planning](#reference-profiles-targets).

<a id="reference-target-platform-architecture-description-compiler-shipped-source-descriptions"></a>

### Compiler-shipped source descriptions

Target data is ordinary compiler-shipped ZLang source:

- `std.target.generic` defines the resource-free generic target;
- `std.target.xilinx.series7` defines the bounded DSP48E1 capability;
- `std.target.xilinx.xc7z030` defines the device part, inventory, and cascade
  capacity;
- `std.arch.xilinx7_fir` defines the one manual symmetric-FIR template;
- `std.target.asic.sky130` describes the real `sky130_fd_sc_hd` standard-cell
  library boundary and proves that the core records are not FPGA- or
  Xilinx-specific.

The core parser and IR understand generic resources, typed ports, operation and
width limits, register sites, dedicated links, inventories, targets, and
architecture requirements. The names `DSP48E1`, `PCIN`, and `PCOUT`, and the
physical SystemVerilog binding all originate in the library definitions - not in
functional ZLang or semantic branches.

The SKY130 profile intentionally emits ordinary RTL for downstream
Liberty-based mapping. It records the documented
[`dfxtp_1`](https://skywater-pdk.readthedocs.io/en/main/contents/libraries/sky130_fd_sc_hd/cells/dfxtp/README.html),
[`dfrtp_1`](https://skywater-pdk.readthedocs.io/en/main/contents/libraries/sky130_fd_sc_hd/cells/dfrtp/README.html),
and `dlclkp_1` cell identities, but does not claim a hard multiplier, SRAM,
PLL, fixed device inventory, timing closure, or silicon signoff. Those inputs
are PDK/project/flow-specific and must be supplied by the implementation flow.

An exact source-level `timing { latency N ii 1 }` block is public module
behavior, not target-selection policy. Generic implementation graphs publish
their derived `timeless`/`known`/`unknown` timing class and are explicitly
backend-independent. A selected physical graph carries its realization backend
so a Direct-SV resource plan cannot be attributed to another
physical implementation. Target
selection must preserve any exact public module latency.

The bounded Series-7 profile describes a signed 25-bit pre-adder, signed 25x18
multiply, 43-bit product, 48-bit P/PCIN/PCOUT path, optional A/B/D/AD/M/C/P
register sites, and an adjacent/same-group 48-bit dedicated cascade. The
XC7Z030 instance records 400 DSP slices and four locally available cascade
positions; this is deliberately not a complete floorplan model.

<a id="reference-target-platform-architecture-description-manual-selection"></a>

### Manual selection

```bash
zlang examples/symmetric_fixed_fir.zhl \
  --target xc7z030ffg676-1 \
  --target-architecture Xilinx7SymmetricDSPCascade \
  --target-architecture-mode required \
  --systemverilog build/SymmetricFixedFIR.sv \
  --implementation-manifest build/SymmetricFixedFIR.manifest.json
```

`required` turns every target, shape, width, inventory, dedicated-link, or
backend failure into a diagnostic. `preferred` returns the generic graph on a
mapping failure. `generic` and omission of target options preserve the existing
generic implementation. This command performs explicit selection; bounded
automatic target-aware selection follows the model described in this chapter.

<a id="reference-target-platform-architecture-description-the-selected-graph"></a>

### Boundaries

The generic Direct-SV implementation remains available when no
primitive graph is selected. Existing semantic-reference equivalence can validate the
semantic fixed-point region, but its reference emitter does not model vendor
primitives. Physical evidence therefore consists of the exact semantic oracle,
Verilator execution of the separate resource behavior model, and real Vivado
synthesis/place/route of the primitive artifact. No primitive-level formal
infrastructure was added.

The automatic planner covers only its documented symmetric-FIR and
signed-product regions;
general placement/graph covering, DSP48E2, Intel physical emission, automatic
storage/clock-resource mapping, and new formal machinery remain outside the
current bounded support.


The bounded planner is implemented for two exact fixed-point region families on
`xc7z030ffg676-1`: the symmetric FIR cascade and ordered signed-product
reductions used by complex multiply (`p0 - p1` and `p0 + p1`). It extends the
existing architecture alternatives/exact reduction planning/pipeline scheduling/timing alignment/deterministic cost selection/bounded exploration path; it is not a second exploration engine.

<a id="reference-high-level-target-aware-architecture-pipeline-planner-user-contract"></a>

### User contract

```zlang
result = implement {
    quantize<fixed<16,14>>(acc) {
        round nearest_even
        overflow saturate
    }
    intent { latency <= 8 ii == 1 fmax >= 100 }
}
```

`ii` and the compatibility spelling `throughput` normalize to one canonical II
constraint. Fmax values are MHz. `latency<=N` is a bound. `latency==N` is an
exact observable sample latency. `maximize fmax` is an evidence-ranked
selection objective, not an achieved-frequency guarantee.

The target is build configuration, for example:

```bash
zlang examples/symmetric_fixed_fir_auto.zhl \
  --top SymmetricFixedFIRAuto \
  --target xc7z030ffg676-1 \
  --target-evidence-policy measured_required \
  --systemverilog build/SymmetricFixedFIRAuto.sv \
  --implementation-manifest build/SymmetricFixedFIRAuto.json \
  --pipeline-report build/SymmetricFixedFIRAuto.report
```

Functional source never names DSP48E1, MREG or PREG. With no target, or with
`--target generic`, the ordinary generic candidate and backends remain active.

<a id="reference-high-level-target-aware-architecture-pipeline-planner-candidate-generation-and-legality"></a>

### How target evidence affects selection

This topic belongs here only as a practical distinction: an estimated resource
cost is not a measured FPGA result, and an estimated Fmax is not routed timing.

| Evidence kind | What it establishes |
| --- | --- |
| `structural_estimate` | Compiler estimate from the selected structure; no vendor run. |
| `synthesis_measurement` | Result after synthesis, not placed/routed timing closure. |
| `routed_measurement` | Measured result after implementation/routing for matching target and constraints. |

`measured_required` accepts only compatible routed evidence for an Fmax
constraint. Structural latency/II remain authoritative and are not erased by
that policy. Compatibility includes target/part, architecture, complete graph
hash, pipeline configuration, backend, clock constraint and tool/version.
`measured_preferred` ranks the strongest compatible evidence cohort before the
numeric objective and falls back to estimates only when no legal stronger
candidate remains. This prevents a coarse structural Fmax estimate from being
compared as though it were an equally authoritative routed measurement.

Packaged routed evidence is bound to the matching part, constraints, physical
graph, pipeline configuration, backend and tool recipe; a different graph's
measurement cannot be reused as proof of this one. The optional
`--implementation-manifest PATH` records the selected configuration, resource
and timing decisions and their provenance for inspection or replay. Internal
JSON schema numbers are compatibility details, not language features or knobs
users should set.

<a id="reference-high-level-target-aware-architecture-pipeline-planner-current-boundary"></a>

### Current boundary

Automatic target planning is intentionally limited to the symmetric-FIR and
signed-product Direct-SV regions above. BRAM, PLL/MMCM, Intel physical planning,
arbitrary graph covering, II-changing sharing, and automatic fixed
transformations are not enabled. Generic Direct-SV remains the
fallback implementation. Existing formal infrastructure is unchanged and no
primitive-level proof claim is made.


<a id="reference-platform-constraint-publication-decision"></a>
### Clock constraints

A selected project profile can declare one explicitly named clock period:

```toml
[profiles.release.platform.clocks.clk]
period-ns = 10.0
```

The clock name must match the selected top's clock declaration. The period is a
physical build setting and is not inferred from an `fmax` intent.

The CLI writes constraints only together with exactly one SystemVerilog output.
The following command assumes a project-local `top.zhl` and the `release`
profile shown above:

```text
zlang top.zhl --profile release --systemverilog build/Top.sv \
  --constraints-xdc build/Top.xdc --constraints-sdc build/Top.sdc
```

XDC and SDC initially contain only:

```tcl
create_clock -name clk -period 10 [get_ports {clk}]
```

The clock edge and reset mode must match the source declaration. A stale or
mismatched constraint request is rejected. XDC/SDC output is deterministic and
is written atomically without following symlinks.

No reset false path is emitted: asynchronous-reset recovery/removal timing must
not be hidden. This constraint publication supports only the exact single-clock
profile shown here. Multiple clock domains are supported in language semantics
and Direct-SV emission, but generated clocks, multi-clock XDC/SDC publication,
pin/package/IO properties, input/output delays, CDC constraints, and formal
properties are not emitted by this facility.
