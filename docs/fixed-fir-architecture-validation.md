# Fixed-point FIR architecture validation

> **Historical evidence:** Clash measurements below preserve the original
> comparison record. Current compiler planning, verification, and release
> acceptance use direct SystemVerilog only.

This unnumbered validation slice deliberately keeps automatic fixed-point
exploration disabled. Four source-authored architectures implement one frozen
numerical contract and are compared only after bit-exact backend validation.

## Numerical contract

All variants in `examples/fixed_fir_architectures.zhl` implement eight `SF2.10`
products, retain the full-precision accumulation, and cross one numerical loss
boundary:

```zlang
products = coefficients * samples
acc = sum(products)

out = quantize<fixed<16,14>>(acc) {
    round nearest_even
    overflow saturate
}
```

There is exactly one `FixedConvert` to `fixed<16,14>` after accumulation. No
product or partial sum is rounded. All variants have II=1:

| Variant | Shape | Latency |
| --- | --- | ---: |
| `FixedFIRLinear` | left-associated full-precision accumulator and output register | 1 |
| `FixedFIRBalanced` | balanced `dot` reduction and output register | 1 |
| `FixedFIRPipelinedTree` | registered pair/half/final tree | 3 |
| `FixedFIRDspOriented` | registered multiplier bank and full-precision accumulator register | 2 |

The “DSP-oriented” name describes the manually exposed register boundary; it
does not attach a vendor primitive or promise DSP inference.

The integer oracle, semantic simulator, direct-SV RTL, and Clash-generated RTL
agree on positive, negative, midpoint, overflow, and saturation vectors after
the declared latency. Both RTL paths are exercised by Verilator.

## Vivado 2024.2 routed results

The reproducible runner is `tools/fixed_fir_vendor_qor.py`. These measurements
use `xc7z030ffg676-1`, a 10.000 ns clock, 0 ns input/output delay, Vivado 2024.2
synthesis, placement, physical optimization, and routing. The larger package is
required because the deliberately unwrapped benchmark exposes 209 physical IOs.
Fmax is the reciprocal of the routed critical delay (`period - WNS`), not a
claim that the requested 100 MHz constraint closed.

```bash
source ~/.Xilinx/Vivado/2024.2/settings64.sh
.venv/bin/python tools/fixed_fir_vendor_qor.py \
    --output build/fixed-fir-qor --jobs 2
```

| Architecture | Backend | DSP | LUT | FF | BRAM | Latency | II | Fmax MHz | WNS ns | Vivado s | RTL bytes/lines |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Balanced | Clash | 10 | 181 | 16 | 0 | 1 | 1 | 70.02 | -4.282 | 78.8 | 5851/153 |
| Balanced | direct-SV | 8 | 148 | 16 | 0 | 1 | 1 | 53.52 | -8.686 | 80.7 | 2641/21 |
| DSP-oriented | Clash | 8 | 175 | 37 | 0 | 2 | 1 | 76.14 | -3.133 | 75.7 | 7032/211 |
| DSP-oriented | direct-SV | 8 | 152 | 31 | 0 | 2 | 1 | 73.43 | -3.619 | 76.4 | 3384/75 |
| Linear | Clash | 8 | 174 | 16 | 0 | 1 | 1 | 52.68 | -8.982 | 73.8 | 5849/153 |
| Linear | direct-SV | 8 | 153 | 16 | 0 | 1 | 1 | 52.08 | -9.202 | 76.0 | 2639/21 |
| Pipelined tree | Clash | 10 | 184 | 32 | 0 | 3 | 1 | 78.43 | -2.750 | 73.6 | 6770/195 |
| Pipelined tree | direct-SV | 10 | 175 | 27 | 0 | 3 | 1 | 75.11 | -3.314 | 73.6 | 3018/61 |

Counts are post-route primitives under identical constraints. Compile times
were measured with two concurrent Vivado jobs and compare like-for-like within
this run, but are not isolated single-process tool benchmarks.

### Direct-SV materialization before/after

The direct emitter now selects materialization candidates from deterministic
typed-IR structure. A structurally large `FixedConvert` input is assigned once
to an exact-width signed temporary and every rounding/saturation branch reads
that temporary. Trivial expressions remain inline. The numerical conversion
node and its source origin are unchanged.

| Architecture | Metric | Before | After | Change |
| --- | --- | ---: | ---: | ---: |
| Linear | RTL bytes | 30,875 | 2,639 | -91.5% |
| Linear | RTL lines | 19 | 21 | +2 named lines |
| Linear | LUT / FF / DSP | 153 / 16 / 8 | 153 / 16 / 8 | unchanged |
| Linear | Fmax MHz | 51.97 | 52.08 | +0.2% |
| Linear | WNS ns | -9.243 | -9.202 | +0.041 |
| Balanced | RTL bytes | 30,877 | 2,641 | -91.4% |
| Balanced | RTL lines | 19 | 21 | +2 named lines |
| Balanced | LUT / FF / DSP | 148 / 16 / 8 | 148 / 16 / 8 | unchanged |
| Balanced | Fmax MHz | 53.08 | 53.52 | +0.8% |
| Balanced | WNS ns | -8.838 | -8.686 | +0.152 |

Pipelined-tree and DSP-oriented RTL did not contain a large `FixedConvert`
operand and are byte-for-byte unchanged. Vivado had already recovered most
common logic from the repeated text, so materialization produces a major
readability/RTL-size improvement and a small non-regressing timing improvement,
not the missing balanced-tree timing gain. The remaining Clash/direct-SV
balanced timing difference must be investigated in post-synthesis structure;
it must not be “fixed” by moving or narrowing quantization.

## Findings

- The linear accumulator is the slowest architecture in both backends.
- Explicit pipeline variants improve routed Fmax materially while retaining
  II=1 and the same final quantization.
- The DSP-oriented shape uses exactly eight DSP48E1 cells in both backends.
  Balanced/pipelined variants sometimes map two additional operations into DSP
  cells, showing that source intent is not identical to vendor mapping.
- Pipelined-tree Clash is fastest in this run; direct-SV is close and uses fewer
  LUT/FF cells. One device/run is insufficient for a general backend ranking.
- Direct-SV linear/balanced now materializes the exact accumulator once. RTL is
  about 91% smaller while resource use is unchanged and routed timing improves
  slightly. The earlier timing hypothesis was therefore only a minor factor.
- Clash RTL has stable named intermediates. Direct-SV pipelined RTL is currently
  the most compact/readable result.

## Source-described four-DSP48E1 implementation

The bounded target-platform slice adds a separate manual implementation of the
new, genuinely symmetric `examples/symmetric_fixed_fir.zhl`. Its functional
interface contains four coefficient values which are semantically reused at
mirrored taps. The same source compiles generically; no primitive or target
token appears in it.

The reproducible runner `tools/target_fir_vendor_qor.py` compared the ordinary
generic direct-SV graph with the manually required
`Xilinx7SymmetricDSPCascade` graph. Both implement eight exact `SF2.10`
products, a 27-bit/F20 accumulation, and one final nearest-even/saturating
conversion to `fixed<16,14>`. Vivado 2024.2 used the same
`xc7z030ffg676-1` part and 10 ns constraint:

| Implementation | DSP | LUT | FF | BRAM | Latency | II | Fmax MHz | WNS ns | Vivado s | RTL bytes/lines |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| source-described DSP48E1 cascade | 4 | 234 | 16 | 0 | 1 | 1 | 72.10 | -3.869 | 312.1 | 7195/203 |
| unchanged generic direct-SV | 8 | 149 | 16 | 0 | 1 | 1 | 53.86 | -8.567 | 93.3 | 2641/21 |

The LUT count is the runner's post-route `REF_NAME =~ LUT*` count, matching the
method used by the earlier table; Vivado's Slice LUT utilization row reports
204 for the target graph. The target graph deliberately values exact resource
realization over RTL compactness in this first experiment.

Vivado preserved four `DSP48E1` cells in `PCIN+D+A*B` mode. The routed critical
path shows the physical chain at adjacent sites
`DSP48_X5Y36 → DSP48_X5Y37 → DSP48_X5Y38 → DSP48_X5Y39`, traversing
`dsp0_primitive/PCOUT` through `dsp2_primitive/PCOUT` and ending at
`dsp3_primitive/P`. This is physical evidence for the three selected dedicated
cascade edges, not an inference from the intended-resource manifest.

The target resource model and exact functional oracle agree in Verilator for
zero, mixed-sign, maximum positive, and saturation vectors at latency one.
Generic semantic simulation, generic direct-SV, and generic Clash remain
independently available. Current M36 fixed-region checks validate the semantic
calculation but do not model a vendor primitive, so no primitive-level formal
claim is made.

## Transformation recommendation

Do not enable automatic fixed exploration from this single study. The evidence
supports later consideration of balanced reduction, explicit register cuts,
and a multiplier-bank/DSP-boundary shape, while preserving one immovable
post-accumulation quantization boundary.

Before admitting those transformations into M29/M32, repeat on a newer Xilinx
family and a non-Xilinx target and tighten constraints around each measured Fmax.
Any optimizer must prove fixed
type/scale, latency, II, and the single final `FixedConvert`; it must never move
quantization into products or partial sums.

The follow-up eight-product pipeline experiment compares explicit total
latencies of eight and twelve cycles on Series-7. It
shows that forced inference can map the dot product to ten DSP48E1 cells and
meet 100 MHz, but generic retiming does not turn tail pipeline stages into
internal A/B/M/P register cuts.  That evidence reinforces the need for the
separate manual DSP48E1 register-configuration experiment before target-aware
automatic exploration.

## High-level target-aware selection

The bounded planner now consumes the same symmetric functional expression from
`examples/symmetric_fixed_fir_auto.zhl` with:

```zlang
pipeline(auto, latency<=8, ii==1, fmax>=100) { ... }
```

It creates one generic candidate and exactly the four configurations published
by the DSP resource. Routed-required evaluation rejects the unregistered
72.10 MHz and multiply-only 80.20 MHz candidates. The remaining configurations
both have recorded 108.08 MHz timing and equal measured resource cost, so M28's
ordinary deterministic latency tie-break selects multiply plus terminal output
at useful latency three. No configuration name appears in ranking logic.

Vivado 2024.2 place/route on `xc7z030ffg676-1` with a 10 ns constraint measured:

| User latency contract | Useful sites | Compensation | DSP | LUT | FF | Routed Fmax | WNS |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `latency<=8` | multiply + terminal output | 0 | 4 | 233 | 16 | 108.085 MHz | +0.748 ns |
| `latency==8` | multiply + terminal output | 5 cycles | 4 | 249 | 37 | 106.123 MHz | +0.577 ns |

Both use four adjacent DSP48E1 cells and the expected physical configuration:
MREG on all four resources and PREG only on the terminal resource. The exact
graph has its own implementation hash and routed evidence record; it does not
reuse the latency-three measurement. Both pass bit-exact Verilator simulation at
their declared observable latency. Generic Clash generation remains valid with
no target selected.
