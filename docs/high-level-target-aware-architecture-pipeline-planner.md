<a id="reference-high-level-target-aware-architecture-pipeline-planner"></a>
# Target-aware architecture and pipeline planning


The bounded planner is implemented for two exact fixed-point region families on
`xc7z030ffg676-1`: the symmetric FIR cascade and ordered signed-product
reductions used by complex multiply (`p0 - p1` and `p0 + p1`). It extends the
existing architecture alternatives/exact reduction planning/pipeline scheduling/timing alignment/deterministic cost selection/bounded exploration path; it is not a second exploration engine.

<a id="reference-high-level-target-aware-architecture-pipeline-planner-user-contract"></a>
## User contract

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
## Candidate generation and legality

The symmetric-FIR region is exactly an eight-tap signed fixed reduction where
four coefficient semantic values are reused at mirrored sample indices. Runtime
coefficient equality is not symmetry. Eight independent coefficients therefore
remain generic. The signed-product region is derived from the already typed
`SignedProductReduction`: ordered product terms and each add/subtract join are
preserved, and the resource must advertise the required accumulator modes.
Arbitrary subtraction trees or reassociation are not inferred.

The candidate set is:

1. generic implementation;
2. source-described symmetric cascade with no resource-local site;
3. multiply site selected;
4. multiply and terminal output sites selected;
5. input/pre-add, multiply and terminal output sites selected.

These are the four configurations published by the resource library. Core code
does not enumerate primitive register bits. The signed-product family applies
the same configuration names to its exact two-resource chain. Before cost
extraction the planner validates the typed pattern, exact
pre-add/product/accumulator widths, required add/sub capability, inventory,
dedicated edges, II, and latency.

The final `FixedConvert` stays after the full 27-bit/F20 accumulation. A mapping
requiring hidden narrowing is rejected and the generic candidate remains.

<a id="reference-high-level-target-aware-architecture-pipeline-planner-timing-dag"></a>
## Timing DAG

Every selected target graph carries a backend-independent timing DAG with
stable semantic/implementation identities. It records resource combinational
segments, selected resource-local cuts, the three dedicated cascade edges,
fabric boundaries, final fixed quantization and output boundary.

Timing alignment produces explicit `alignment` delay objects. Exact external
latency produces separate `compensation` objects. Both record cycles, width and
fabric FF cost in the graph and manifest. They are never reconstructed from RTL
names.

For the selected useful latency-three implementation:

- `latency<=8` adds no delay;
- `latency==8` adds one explicit five-cycle output compensation delay;
- resource-local useful sites remain unchanged.

The Direct-SV emitter consumes this graph. The base output boundary and five
compensation cycles form the required output register chain; source
`pipeline(N)` latency is not added again.

<a id="reference-high-level-target-aware-architecture-pipeline-planner-evidence-and-extraction"></a>
## How target evidence affects selection

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
## Current boundary

Automatic target planning is intentionally limited to the symmetric-FIR and
signed-product Direct-SV regions above. BRAM, PLL/MMCM, Intel physical planning,
arbitrary graph covering, II-changing sharing, and automatic fixed
transformations are not enabled. Generic Direct-SV remains the
fallback implementation. Existing formal infrastructure is unchanged and no
primitive-level proof claim is made.
