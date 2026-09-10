# High-level target-aware architecture and pipeline planner

The bounded planner is implemented for two exact fixed-point region families on
`xc7z030ffg676-1`: the symmetric FIR cascade and ordered signed-product
reductions used by complex multiply (`p0 - p1` and `p0 + p1`). It extends the
existing M29/M32/M31/M30/M28/M34 path; it is not a second exploration engine.

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
exact observable sample latency.

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

## Timing DAG

Every selected target graph carries a backend-independent timing DAG with
stable semantic/implementation identities. It records resource combinational
segments, selected resource-local cuts, the three dedicated cascade edges,
fabric boundaries, final fixed quantization and output boundary.

M30 alignment produces explicit `alignment` delay objects. Exact external
latency produces separate `compensation` objects. Both record cycles, width and
fabric FF cost in the graph and manifest. They are never reconstructed from RTL
names.

For the selected useful latency-three implementation:

- `latency<=8` adds no delay;
- `latency==8` adds one explicit five-cycle output compensation delay;
- resource-local useful sites remain unchanged.

The direct-SV emitter consumes this graph. The base output boundary and five
compensation cycles form the required output register chain; source
`pipeline(N)` latency is not added again.

## Evidence and extraction

Evidence distinguishes:

- `structural_estimate`;
- `synthesis_measurement`;
- `routed_measurement`.

`measured_required` accepts only compatible routed evidence for an Fmax
constraint. Structural latency/II remain authoritative and are not erased by
that policy. Compatibility includes target/part, architecture, complete graph
hash, pipeline configuration, backend, clock constraint and tool/version.

The shipped Vivado 2024.2 records are data in
`zlang/data/xc7z030_dsp_pipeline_qor.json` and
`zlang/data/xc7z030_signed_product_qor.json`; ranking contains no
configuration-name special case. M28 applies hard constraints and the existing
`minimize lut` default. Its deterministic latency tie-break selects the
lower-latency candidate when measured costs tie. With measured-required evidence
and the frozen 100 MHz constraint, the signed real/imag witnesses select the
two-cycle `multiply_registered` configuration.

The selected bounded report is equivalent to:

```text
target: xc7z030ffg676-1
requirements: latency <= 8, ii == 1, fmax >= 100
rejected unregistered: routed 72.10 MHz < 100
rejected multiply-only: routed 80.20 MHz < 100
selected symmetric cascade / multiply+terminal-output
resources: DSP=4 LUT=233 FF=16
latency=3 II=1 routed Fmax=108.08 MHz
```

## BackendArtifact implementation manifest v7

The implementation manifest retains normalized user constraints, objective and
metric sources, selected template/target/graph/configuration, generic pipeline
sites, resources, dedicated edges, complete timing DAG, alignment and
compensation objects, latency/II, evidence identity and implementation artifact
hash. JSON round-trip uses implementation manifest version 7; older optional
fields retain validated compatibility defaults.

## Current boundary

Automatic target planning is intentionally limited to the symmetric-FIR and
signed-product direct-SV regions above. BRAM, PLL/MMCM, Intel physical planning,
arbitrary graph covering, II-changing sharing, and automatic fixed
transformations are not enabled. Clash remains the generic/reference backend.
Existing formal infrastructure is unchanged and no primitive-level proof claim
is made.

Reproduce physical validation with:

```bash
.venv/bin/python tools/target_auto_fir_qor.py \
  --output /tmp/zlang-target-auto-fir \
  --vivado /path/to/Vivado/2024.2/bin/vivado
```
