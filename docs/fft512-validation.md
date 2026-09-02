# FFT512 streaming validation

## Scope and current result

This validation deliberately uses the existing language and planner. It adds no
FFT primitive, generic feature, operator rule, memory semantics, pipeline rule,
or formal observation family.

The generic numerical layer is now sufficient for an exact radix-2 butterfly:

```zlang
type Sample = fixed<18,16>
type Twiddle = fixed<16,14>
type CSample = Complex<Sample>
type CTwiddle = Complex<Twiddle>

result = butterfly(a, b, twiddle)
```

`Complex<Sample> * Complex<Twiddle>` specializes to one shared monomorphic
callable body containing ordinary scalar arithmetic, with exact result
`Complex<fixed<35,30>>`. The library then applies
one explicit nearest-even, saturating conversion to `Sample` before the
butterfly add/subtract. The outputs are
`Complex<fixed<19,16>>`. No `_18_16` helper remains.

The runnable `examples/complex_fft_butterfly.zhl` exercises this contract through
semantic/canonical IR, the simulator, Clash, direct SystemVerilog and Verilator.

## Concrete correctness bug fixed by the attempt

The requested source style exposed a canonicalization error before reaching the
pipeline planner:

```zlang
type Sample = fixed<18,16>
type CSample = Complex<Sample>
```

The fields were resolved to `fixed<18,16>`, but the nominal specialization name
incorrectly remained `Complex<Sample>`. Exact generic unification therefore
rejected it against `Complex<fixed<18,16>>`. Generic struct specializations now
render canonical type/value arguments, so aliases do not leak into semantic IR,
specialization identity, canonical IR, or backend types.

## Closed first streaming FFT blocker

The first required automatic pipeline region is the real component of a complex
twiddle multiply:

```zlang
exact = sample * twiddle

result = pipeline(auto, ii==1, fmax>=100) {
    quantize<Sample>(exact.re) {
        round nearest_even
        overflow saturate
    }
}
```

The generic operator correctly exposes:

```text
sample.re * twiddle.re - sample.im * twiddle.im
```

the fixed `pipeline(auto)` recognizer now derives a typed
`SignedProductReduction`. The real component retains ordered signs `(+,-)`, two
`fixed<34,30>` products and its original `fixed<35,30>` subtraction. The
imaginary component is recognized independently as `(+,+)`. No inferred width
is written into FFT source.

The supported source is
`examples/fft/complex_multiply_pipeline_auto.zhl`. It contains explicit
`FFTComplexMultiplyRealAuto` and `FFTComplexMultiplyImagAuto` entry points;
tests select those tops directly rather than manufacturing a second source by
text replacement. Each generic fallback is still the unchanged expression with
one output pipeline stage. Its timing DAG contains two product segments, the
join, the final quantization boundary and the output cut. Direct-SV and Clash
RTL both simulate the same bit-exact vectors in Verilator.

The DSP48E1 profile advertises `accumulator + product` and
`accumulator - product` as separate capabilities. The direct-SV binding now
consumes the selected two-resource graph, emits one DSP48E1 per product, uses
ALUMODE `0011` for the real subtraction stage, and retains the final
quantization in ordinary typed RTL. The generic Clash path remains unchanged;
no physical Clash DSP binding was added.

The eight scalar physical variants (real/imag × four source-published
PipelineConfigurations) were synthesized and routed with Vivado 2024.2 on
`xc7z030ffg676-1` at 10 ns. Every variant used two DSP48E1s, 227 LUTs, 18 FFs
and zero BRAMs. The unregistered forms miss 100 MHz, while all three registered
forms close it:

| Component | Configuration | Latency | DSP A/B/D/M/P | WNS (ns) | Fmax (MHz) | Route (s) |
| --- | --- | ---: | --- | ---: | ---: | ---: |
| real | unregistered | 1 | 0/0/0/0/0 | -2.495 | 80.032 | 109.641 |
| real | multiply_registered | 2 | 0/0/0/1/0 | +0.529 | 105.585 | 84.263 |
| real | multiply_output_registered | 3 | 0/0/0/1/1 terminal | +0.709 | 107.631 | 75.666 |
| real | fully_pipelined | 4 | 1/1/1/1/1 terminal | +0.709 | 107.631 | 76.359 |
| imag | unregistered | 1 | 0/0/0/0/0 | -1.640 | 85.911 | 98.854 |
| imag | multiply_registered | 2 | 0/0/0/1/0 | +0.529 | 105.585 | 79.456 |
| imag | multiply_output_registered | 3 | 0/0/0/1/1 terminal | +0.709 | 107.631 | 76.108 |
| imag | fully_pipelined | 4 | 1/1/1/1/1 terminal | +0.709 | 107.631 | 76.244 |

The machine-readable routed evidence is kept in
`zlang/data/xc7z030_signed_product_qor.json`; the reproducible runner is
`tools/signed_product_pipeline_qor.py`; `--evidence-output` serializes the
exact routed graph identities and metrics. With that evidence and
`target_evidence_policy=measured_required`, the planner rejects both
unregistered candidates and deterministically selects the latency-2
`multiply_registered` candidate for the 100 MHz constraint. Structural
estimates never masquerade as routed proof.

## Cardinal twiddles

The canonical probe for `(1,0)` correctly inlines the constant `Complex` value,
but retains four fixed multiplications. This is consistent with the current M26
freeze: arithmetic identities and fixed-point strength reduction were explicitly
excluded. Consequently there is presently no existing legal optimization that
can promise zero DSPs for `1`, `-1`, `j`, or `-j`. No new rewrite was added in
this slice. A future change must first prove the exact widened multiply,
add/subtract, and final quantization relation; source spelling alone is not
sufficient evidence.

## Parameterized-delay blocker closed

The complete-stage attempt originally stopped at its first required generic
storage declaration:

```zlang
module FFTStageDelay<D=4> {
    clock clk
    reset rst
    in data:u8 in push:bit in pop:bit out front:u8
    fifo samples:fifo<u8,D>
    samples.data=data samples.push=push samples.pop=pop
    front=samples.front
}
```

FIFO and memory depths now accept the existing compile-time integer expression
vocabulary. A depth may be a direct value parameter, exact arithmetic, or a
left shift such as `1 << (LOG2N-STAGE-1)`. It is evaluated during module
specialization; semantic/canonical IR and both backends receive only the
resolved positive integer. Unresolved names, zero/negative results, inexact
division, division by zero, and negative shifts fail before IR construction.

The positive fixture at
`tests/fixtures/fft/parameterized_fifo_depth.zhl` now elaborates as
depth 4. One reusable declaration was also specialized successfully to all FFT
stage delays `256,128,64,32,16,8,4,2,1`, with distinct deterministic module
specialization identities. No fixed-depth wrappers or backend evaluation logic
were introduced.

This closes only the storage-depth blocker. Per the bounded task, the attempt
did not proceed to phase state, feedback, twiddle scheduling, ready/valid
control, or a complete stage. Resuming that source-authored stage is the next
place to discover whether the existing storage/rule composition boundary is a
real blocker.

## Module type-specialization blocker closed

Resuming the reusable stage had reached a module-specialization boundary before
state or storage composition. Its minimal form was:

```zlang
module FFTStageTypeProbe<type Sample> {
    in sample:Sample
    out result:Sample
    result=sample
}

module FFTStageTypeProbeTop {
    in sample:u8 out result:u8
    inst stage:FFTStageTypeProbe<Sample=u8> { sample }
    result=stage.result
}
```

Module specialization now resolves arguments according to the declared
parameter kind before child analysis. Type arguments are canonical
`HardwareType` bindings and value arguments remain compile-time integers. The
two maps are passed independently to the child resolver. `Sample=u8`,
`Sample=fixed<18,16>`, aliases, and
`Sample=Complex<fixed<18,16>>` therefore reach child ports/storage as concrete
types, while `D` remains available to parameterized FIFO/memory expressions.

The positive fixture at
`tests/fixtures/fft/module_type_specialization.zhl` contains both `Sample` and
`D`; its specialized child FIFO is
concretely `fifo<u8,4>`. Named and positional forms converge on the same
canonical identity. Type aliases do not affect identity, while changing either
the canonical type or value argument does. Parent type bindings also propagate
through `Top -> Child<T> -> GrandChild<U=T>`.

Direct-SV/Verilator hierarchy smoke and concrete specialized-child
Clash/Verilog/Verilator smoke pass for `u8`, `fixed<18,16>`, and
`Complex<fixed<18,16>>`. A separate pre-existing generic Clash combinational
parent hierarchy path still emits an unbound child-output name; it is not a
type-specialization or semantic-IR failure and was not broadened into this
slice. Per the explicit stop condition, no SDF phase/control, feedback, twiddle
table, or oracle was built. Storage/rule mixing and aggregate FIFO behavior have
not yet been reached and are not claimed as blockers.

## Next SDF blocker: atomic storage plus user state

With generic module types and parameterized depth available, the reusable stage
now reaches the intended state/storage boundary. The tracked minimal source is
`tests/fixtures/fft/unified_state_storage.zhl`. It contains:

```zlang
module FFTSDFStage<type Sample,type Twiddle,D=4> {
    in input:rv<Complex<Sample>>
    out output:rv<Complex<Sample>>
    fifo samples:fifo<Complex<Sample>,D>
    reg phase:bit=0
    ...
    rule advance when input.transfer { phase <- phase }
}
```

Parsing succeeds, including `Complex<Sample>`, `Complex<Twiddle>`, `D`, and the
ready/valid transfer references. The child type/depth specialization is no
longer the failure. Semantic analysis then deliberately rejects the module:

```text
storage resources cannot yet be mixed with user registers or rules
```

A true SDF stage needs the FIFO update, feedback selection, accepted-transfer
phase increment, and output-hold state to form one atomic transition. Moving
`phase` into an artificial controller child merely to evade this check would
split that ownership before its semantics are defined, and a fixed backend RTL
implementation would bypass backend-independent IR. Neither workaround was
used.

The minimum next slice is generic compositional sequential semantics for a
module containing existing FIFO/memory resources together with existing
registers/rules: define same-edge read visibility, atomic control/state update,
reset ordering, conflict diagnostics, and backend-independent transition
ordering, then implement it consistently in simulator, Clash, direct-SV, and
existing M35 observations. It must not add FFT-specific storage, BRAM/SRL
selection, or new protocol rules. Runtime indexing and FIFO suitability for the
actual SDF feedback algorithm remain untested beyond this blocker.

The accepted state/storage model identifies the current storage/rule IR and
backend dispatch split, defines current-state
reads plus one atomic accepted-action commit, and bounds the first implementation
to registers/rules and the existing FIFO. It deliberately leaves rule-local
memory actions for a later evidence-driven slice. No compiler behavior changed
during the review.

No latency, II, conservation, stall, reset, or RTL result is claimed for a
complete SDF stage. Parameterized FIFO fixtures at depths 2 and 8 do generate
concrete Clash/direct-SV and simulate in Verilator, and existing M35 FIFO bounds
use the resolved depth. The functional butterfly and signed-reduction results
remain unchanged. No nine-stage FFT, cardinal twiddle rewrite, automatic
storage selection, or physical signed-DSP emission was started.

## Unified state/storage acceptance result

The mixed FIFO/register blocker is closed for the frozen bounded subset. FIFO
operations can now belong to an existing rule as `feedback.push(value)` and
`feedback.pop()`. They lower with register writes into one semantic
`ActionGroup`, are selected by one `ResolvedTransition`, and commit atomically.
Full pop/push replacement is legal; empty pop/push suppresses the whole group;
all uses of `front`, `count`, inputs, and registers see pre-edge state.

`tests/fixtures/fft/unified_state_storage.zhl` now elaborates and emits
lint-clean real Clash and direct-SystemVerilog RTL. The smaller executable
acceptance fixture is `examples/fft_sdf_stage_atomic_transition.zhl`. It has a
parameterized delay FIFO, phase state, output holding state, ready/valid stall
gating, and one pop/push/register transition. Python simulation and both RTL
backends agree on directed fill, stall, full replacement, drain, and reset
sequences. The real M35 solver smoke checks the combined FIFO count/legality and
register reset properties without adding a formal family.

This acceptance fixture intentionally uses a scalar payload and does not claim
a functional radix-2 stage. The next concrete validation boundary is to add the
already-supported generic complex butterfly, explicit quantization, and a
twiddle/reference oracle around this working transition. Only that experiment
can establish whether twiddle lookup or feedback storage access needs another
generic capability. No ROM, memory action, storage planner, nine-stage chain, or
FFT512 expansion was started here.

## Compile-time value expressions: blocker closed

The next bounded slice attempted one reusable stage parameterized by
`type Sample`, `type Twiddle`, delay depth, and twiddle-table stride. Its intended
accepted-transfer schedule needs all three compile-time value expressions:

```zlang
phase = counter >= depth
next_counter = counter == ((depth * 2) - 1) ? 0 : counter + 1
twiddle_slot = phase_index * step
```

Module value parameters now use the same resolved specialization environment in
types, storage depths, child specialization arguments, generic functions, and
ordinary expressions. They are substituted and folded before concrete typed IR,
so neither backend receives runtime parameter ports or unresolved parameter
references. The tracked acceptance fixture is
`tests/fixtures/fft/value_parameter_expression.zhl`:

```zlang
module FFTSDFStageValueParameterExpression<D=4,STEP=2> {
    in counter : uint<5>
    in phase_index : uint<4>
    out butterfly_phase : bit
    out next_counter : uint<6>
    out twiddle_slot : uint<8>

    butterfly_phase = counter >= D
    next_counter = counter == ((D * 2) - 1) ? 0 : counter + 1
    twiddle_slot = phase_index * STEP
}
```

With `D=4` and `STEP=2`, typed IR contains contextual constants `4:u5`,
`7:u5`, and `2:u4`. Fully compile-time arithmetic such as `D * 2`, `D / 2`,
and `1 << D` uses the existing arbitrary-precision width/depth evaluator;
mixed expressions keep runtime operands as signals. Nested
`Top<N> -> Child<D=N/2> -> GrandChild<K=D-1>` specialization resolves to
concrete `N=8`, `D=4`, and `K=3` bindings.

The former `unknown input 'D'` diagnostic is gone. Direct-SV/Verilator and real
Clash generation consume only concrete constants. Parameterized and equivalent
literal expressions have the same concrete expression semantic identity, which
is the existing M36/M38 comparison boundary. Invalid unresolved parameters,
runtime specialization values, contextual overflow, negative shift amounts,
division by zero, and non-exact compile-time division remain explicit errors.

This correction only closes the reusable scheduling-expression blocker. No
numerical SDF stage, twiddle table, storage selection, cardinal strength
reduction, or physical DSP mapping was added.

## State-derived twiddle index: blocker closed

The next attempt used a radix-2 decimation-in-frequency SDF recurrence with a delay
of `D` samples and a `2*D` accepted-transfer period:

- the initial low phase fills the delay;
- the high phase reads one delayed sample, emits the butterfly sum, and feeds
  the difference back into the same FIFO;
- on later low phases, that feedback value is multiplied by the scheduled
  twiddle, explicitly quantized to `Sample`, emitted, and atomically replaced
  by the next frame's input;
- the counter advances only on `input.transfer`; a pending output suppresses a
  new transition unless it transfers on the same edge.

This structure uses the already frozen pre-edge FIFO front and atomic
pop/push/register commit. It can sustain one accepted sample per cycle after
the first `D` accepted inputs when the downstream remains ready. The proposed
arithmetic retained generic `Complex<T>`, exact complex multiplication, and a
single explicit nearest-even/saturating conversion after the feedback/twiddle
multiplication. No cardinal-twiddle optimization was attempted.

A full-period counter needs width `CW`, while a `D`-entry table uses the
range-reduced low `IW` bits:

```zlang
reg phase_counter : uint<CW> = 0
twiddle_slot = truncate<IW>(phase_counter)
selected = twiddles[twiddle_slot]
```

The generic expression audit removed the accidental direct-input/name
restriction. `twiddle_slot` retains the concrete `truncate<IW>(phase_counter)`
expression, its semantic identity, and the proven range `0..2^IW-1`. The same
expression can now be written directly inside brackets. The minimized tracked
fixture `tests/fixtures/fft/runtime_twiddle_index.zhl` is now an accepted
regression fixture.

Specialization checks cover future stage delays `256, 128, 64, 32, 16, 8, 4,
2`, with the corresponding concrete counter/index widths; the degenerate
`D=1` lookup is a static index. Canonical round-trip, simulator, direct-SV,
real Clash, Verilator, M36 semantic-reference proof, and M38 raw-bit smoke agree
on the packed-vector selection order.

This slice deliberately stops after closing the generic expression capability.
It does not complete the numerical SDF recurrence, add a twiddle ROM, select
SRL/BRAM storage, build nine stages, optimize cardinal twiddles, or emit physical
DSP resources. Those remain subject to the next concrete real-design blocker.

## One numerical radix-2 DIF SDF stage: D=4 validation

The first complete numerical stage is now source-authored in
`examples/fft/sdf_stage_numeric.zhl`. It remains one reusable
parameterized module; the validation specializes `S=fixed<18,16>`,
`W=fixed<16,14>`, `D=4`, `CW=3`, and `IW=2` before backend emission. No
D-specific source or backend switch table is involved.

Its accepted-transfer recurrence is:

```text
phase 0 .. D-1, count < D:
    feedback.push(input)

phase D .. 2D-1:
    delayed = feedback.front
    feedback.pop(); feedback.push(quantize(delayed - input))
    output = quantize(delayed + input)

phase 0 .. D-1, count == D (subsequent frames):
    delayed = feedback.front
    feedback.pop(); feedback.push(input)
    output = quantize(delayed * twiddles[phase & (D-1)])
```

The sum, difference, and complex twiddle product are kept exact in typed IR;
the only narrowing at each emitted stage result is explicit
nearest-even/saturating conversion to `Sample`. `phase` advances only on an
accepted input transfer and wraps modulo `2*D`. The output register is held
while downstream `ready` is low; input `ready` is consequently deasserted and
the FIFO/phase transition is held as well. Reset clears the FIFO, phase, and
output-valid state, so buffered pre-reset values cannot reappear. With D=4,
the first output transfer occurs after four fill transfers plus the first
butterfly transfer (startup latency D+1 cycles under synchronous ready/valid
observation); continuous traffic then sustains II=1. Gaps, stalls, simultaneous
FIFO pop/push, reset during buffered traffic, and the first post-reset frame are
covered by the validation trace.

An independent Python oracle decodes fixed-point values to exact
`fractions.Fraction`, performs complex products and sums, applies integer
nearest-even rounding, and then clamps to the signed Sample bounds. The
semantic simulator, direct-SystemVerilog RTL, and real Clash-generated Verilog
all match this oracle on the same 36-cycle trace, including arbitrary input
gaps, downstream stalls, and two reset epochs. Both RTL forms pass Verilator
lint and binary simulation. Semantic/canonical round-trip passes, and the
generic target implementation graph is emitted. That graph is not a
`pipeline(auto)` exploration report; the stage has zero pipeline explorations
under the current frozen semantics.

The same source specializes semantically at `D=8` (with the corresponding
counter/index widths); the FIFO and initialized twiddle-ROM shapes are concrete without
duplicating the module. Existing M35 property construction can produce its
ordinary register, FIFO, ready/valid, and rule safety property set for the
stage. Those properties remain unbound to a backend observation harness for
this standalone internal stage, so any existing runner result is explicitly
`skipped` rather than a claimed proof; no new observation family was added.

The stage deliberately does not add `pipeline(auto)` inside rule-owned state:
the current pipeline planner operates on pure/selected value regions, while
this stage's latency is defined by its explicit output holding and FIFO
transition. No new planner semantics, cardinal-twiddle strength reduction,
physical DSP binding, memory inference, or formal observation family was
introduced. Existing M35/M36/M38 properties remain the applicable checks, with
property construction and backend-bound proof execution reported separately.
No M36/M38 relation was asserted for this standalone stateful stage; those
existing slices remain unchanged and the repository regression continues to
exercise their real-solver coverage.

Two backend-independent correctness fixes were required to reach this slice:
specialized child modules now inherit ordinary source-authored operator
declarations (so `Complex<S> * Complex<W>` resolves after specialization), and
immutable locals referenced from resolved rule transitions are expanded before
backend emission. Clash's scheduled multi-rule FIFO applicative expression was
also parenthesized; without that generic emitter fix, a three-rule stage failed
Clash normalization with a misleading `Bit` versus `Bit -> Bit` error.

## User-facing D=4 wrapper

The source organization slice added the concrete `FFTSDFStageNumericD4` wrapper
to `examples/fft/sdf_stage_numeric.zhl` so the reusable stage has a normal CLI
entry point. The minimal command is:

```sh
.venv/bin/zlang examples/fft/sdf_stage_numeric.zhl \
  --top FFTSDFStageNumericD4 -o build/FFTSDFStageNumericD4.hs
```

The earlier semantic gap for sequential ready/valid delegation is closed for
this bounded composition shape. The wrapper is represented by one
`ElaboratedInstance`, with specialization identity separate from physical
instance identity. Direct-SV emits one complete child specialization
containing the initialized ROM, state, scheduled FIFO, rules, and numerical
expressions; Clash emits the same closed child ABI and generates valid
top-level Verilog. The regression is covered by
`test_concrete_d4_wrapper_elaborates_explicit_protocol_hierarchy` and the
cross-backend oracle test in
`tests/integration/test_fft_sdf_stage_numeric.py`.

## Validation performed

- generic runtime-index provenance and fail-closed range matrix;
- future FFT delay/index specialization matrix for `D=256..1`;
- canonical range/source-origin round-trip and local identity independence;
- direct-SV and real Clash runtime-select Verilator simulation;
- real M36 runtime-select versus explicit-switch proof and M38 raw-bit smoke;
- generic alias canonicalization and exact mixed fixed-point inference;
- semantic and canonical IR round-trip for the functional butterfly;
- bit-exact cardinal butterfly simulation;
- direct-SV generation and Verilator lint;
- real Clash generation followed by Verilator lint;
- acceptance regression for the former minimal pipeline failure;
- signed real/imag descriptor, identity and timing-DAG tests;
- generic direct-SV and Clash Verilator simulation;
- real M36 correct and mutated add/sub proofs;
- explicit physical-emission fail-closed diagnostics;
- inspection of the canonical cardinal-twiddle graph;
- existing target-planner, M36 and M38 regressions through the repository suite;
- explicit `FFTSDFStageNumericD4` hierarchy elaboration and both-backend
  Verilator simulation against the exact oracle.

The accepted signed-product implementation preserves the original typed
add/subtract join tree, requires resource-advertised subtraction
support, and keeps cardinal-twiddle strength reduction as a separate future
slice. Fixed-point and quantization semantics were not changed.

## One-stage quantization cleanup and QoR characterization

The accepted cleanup keeps one named typed conversion for each architectural
complex result in `examples/fft/sdf_stage_numeric.zhl`:
`low_quantized`, `high_sum`, and `high_diff`. Their `.re`/`.im` projections are
performed only after the aggregate conversion. The semantic test traverses the
typed locals and resolved transition, confirms six conversion nodes (two fields
per complex result) with six distinct semantic identities, and confirms that no
conversion is duplicated in the state transition itself. The FIFO push is
checked through the resolved `StateAction` representation, where it retains the
`high_diff` aggregate reference.

The existing M35 generator was exercised for this stateful stage. It produces
the register, FIFO, ready/valid, and rule safety families, but this standalone
internal stage has no backend observation harness. Every result is therefore
reported as an explicit `skipped` with a diagnostic; no proof is claimed and no
formal observation family was added.

The focused stage/materialization suite is **11 passed**. Direct-SV and Clash generated Verilog
continue to pass lint and binary Verilator simulation against the independent
exact-`Fraction` oracle, including gaps, stalls, simultaneous FIFO replacement,
and both reset epochs. The D=8 specialization still elaborates with concrete
FIFO and initialized-ROM shapes. The stage remains unpipelined and reports zero
`pipeline_explorations`.

Vivado 2024.2 was selected through the portable `ZLANG_VIVADO`/`--vivado`
tool configuration, and both artifacts completed
synthesis, placement, physical optimization, and routing for
`xc7z030ffg676-1` with a 10 ns clock. Before this fix, direct-SV synthesis
stopped on 21 instances of `[Synth 8-2599] range is not allowed in a prefix`
because a dynamic aggregate selection was immediately field-sliced. The
unified-state emitter now materializes that typed aggregate once, and the
minimal `vec<4,Pair>` state-update regression confirms that no
`...[dynamic +: width][field]` prefix remains. The regression is Verilator
lint-clean and also synthesizes successfully in Vivado 2024.2.

| backend/artifact | generation step | time | RTL bytes | RTL lines | routed DSP | routed LUT | routed FF | routed BRAM | WNS | critical delay / Fmax proxy |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| direct SystemVerilog | direct emitter + Vivado route | 0.104451 s + 75.976 s | 12,277 | 156 | 4 | 551 | 49 | 0 | -2.749 ns | 12.732 ns data / ~78.4 MHz |
| Clash source | Clash emitter | 0.010190 s | 60,809 | 96 | — | — | — | — | — | — |
| Clash Verilog | Clash 1.11 generation + Vivado route | 4.342282 s + 78.844 s | 19,162 | 554 | 4 | 731 | 187 | 0 | -2.194 ns | 11.773 ns data / ~82.0 MHz |

Both backends now have real routed data under identical constraints. The Clash
route remains at DSP=4, LUT=731, FF=187, BRAM=0, WNS=-2.194 ns and ~82.0 MHz;
the direct-SV route uses DSP=4, LUT=551, FF=49, BRAM=0, WNS=-2.749 ns and
~78.4 MHz. The delay values are the reported worst-path data delays, and the
frequency values are 10 ns-period slack proxies rather than new timing
contracts. Both unpipelined forms miss 100 MHz, so this slice stops with the
frozen QoR evidence; it does not add elastic pipeline semantics or physical DSP
primitives.

## Hierarchical Clash ABI cleanup

The concrete `FFTSDFStageNumericD4` wrapper also validates the backend-internal
hierarchy boundary. Scheduled-FIFO child emission and standalone storage-module
emission share one typed `_emit_storage_circuit` equation generator; the child
path receives an explicit legal function name and adapts its scalar and
ready/valid arguments without extracting or renaming generated Haskell text.
The standalone path alone adds `topEntity` and synthesis annotations.

BackendArtifact checks cover the physical `stage` instance and its separate
specialization identity, top/child ready-valid leaves, the typed ROM companion, and
published child register/FIFO state bindings with source origins. Direct-SV
publishes stable component tokens while production formal availability remains
explicitly false; Clash production artifacts likewise do not fabricate formal
observation ports. Manifest JSON round trips preserve all identities and
bindings. At this historical checkpoint, the focused wrapper suite passed 19
tests and the full repository regression passed 926 tests.

## Source-generated initialized twiddle ROM

The external `vec<D,Complex<W>>` twiddle port has now been removed from both the
reusable stage and its concrete wrapper. Ordinary ZLang source owns the table:

```zlang
fn fft_twiddles<type T,N>() {
    generate(k in 0..(N / 2)) Complex {
        re = quantize<T>(cos((-2 * pi() * k) / N)) {
            round nearest_even
            overflow saturate
        }
        im = quantize<T>(sin((-2 * pi() * k) / N)) {
            round nearest_even
            overflow saturate
        }
    }
}

rom twiddles : rom<Complex<W>,D> {
    read_latency 1
    init fft_twiddles<T=W,N=D * 2>()
}
```

The ROM's one-cycle latency is part of the stage schedule. On an accepted input
transfer the address expression prefetches the next phase; under a stall it
holds the current phase address. A phase advance and its corresponding twiddle
therefore become visible together after the edge, while backpressure cannot
walk the table independently. Reset clears only the registered ROM output and
does not accept the reset-cycle address; the immutable table persists.

For `fixed<16,14>`, the independently checked D=4 raw table is `(16384,0)`,
`(11585,-11585)`, `(0,-16384)`, and `(-11585,-11585)`. The D=8 specialization
likewise produces eight independently checked words. Simulator traces for D=4
and D=8 match the exact fixed-point recurrence across continuous traffic, gaps,
downstream stalls, phase wrap, and mid-stream reset. The D=4 direct-SV and real
Clash 1.11 artifacts consume byte-identical deterministic companions and match
each other under Verilator.

This closes only initialized synchronous ROM and one-stage twiddle scheduling.
It does not add writable initialization, asynchronous or multiport ROM,
automatic BRAM selection, cardinal-twiddle strength reduction, or the nine-stage
FFT512 composition.

## FFT4 D=2 -> D=1 streaming composition

The next bounded real-design slice composes two ordinary
`FFTSDFStageNumeric` specializations as `FFT4SDFReference`: first `D=2`, then
`D=1`. The natural output-bin order is radix-2 DIF bit-reversed order
`0, 2, 1, 3`. This is a continuous SDF network, not a self-flushing batch
operator: newly accepted tokens move the previous four-sample frame forward,
so a finite behavioral fixture supplies accepted padding tokens before idle
drain cycles.

Both specializations retain distinct semantic, specialization, and physical
instance identities. The emitted artifact publishes two deterministic ROM
companions with exact depths 2 and 1; the depth-one table is not widened into a
two-entry semantic image. Direct SystemVerilog emits and passes Verilator lint.
Real Clash 1.11 generation also succeeds. Verilator lint of Clash-generated
`romFile` RTL uses the same established `-Wno-WIDTHTRUNC` waiver already needed
by the initialized-ROM integration tests, because Clash widens the generated
ROM selector even for exact ZLang table depths.

The independent staged fixed-point fixture uses raw samples
`(1000,200), (-300,500), (700,-100), (-200,-400)` and produces
`(1200,200), (2200,0), (1200,400), (-600,200)`. Backend-independent simulation,
direct SV, and Clash agree for continuous traffic, source gaps, downstream
stalls, initial reset, and mid-stream reset. Output payload is stable for the
complete stalled interval. Whole-build publication records both ROM files and
a valid artifact-bound source-map sidecar; exact child-internal generated-line
mappings are not yet available and are therefore left empty rather than
guessed.

The target report remains a generic backend-independent implementation graph.
That is the truthful result for this stateful protocol hierarchy: no target
architecture was selected, and no whole-module latency, DSP mapping, Fmax, or
QoR conclusion follows from FFT4.

This slice validates only the FFT4 hierarchical streaming shape and exact ROM
specializations. It does not claim target-aware pipeline selection, physical
DSP mapping, cardinal-twiddle strength reduction, or whole-FFT synthesis/QoR
results. The later nine-stage FFT512 functional hierarchy is documented below.

## FFT8 D=4 -> D=2 -> D=1 streaming composition

The next bounded composition reuses the same numerical stage three times as
`FFT8SDFReference`, specialized at exact delay and ROM depths 4, 2, and 1. The
instances, specialization identities, state, and initialized companions remain
distinct throughout semantic/canonical round trips and backend artifact
publication. The depth-one ROM remains an exact one-word semantic image.

This is the existing continuous DIF-SDF protocol, not a framed or self-flushing
FFT operation. If `x0` is accepted at relative cycle 0 under continuous
valid/ready traffic, the first result transfers at relative cycle 10 and all
eight results transfer at II=1 on relative cycles 10 through 17. A finite
eight-token frame needs seven subsequently accepted sentinel tokens to advance
all of its values. Three idle observation cycles after the final sentinel then
expose the registered tail. The sentinel values already belong to the next
continuous-stream frame; the implementation does not assign them special flush
semantics.

The exact backend-independent oracle applies the same operation at spans 8, 4,
and 2. For each butterfly pair it computes:

```text
upper = Q(a + b)
difference = Q(a - b)
lower = Q(difference * W_span^index)
```

Here `Q` is componentwise nearest-even quantization with saturation to
`fixed<18,16>`, and each twiddle is represented as `fixed<16,14>`. Thus no
ideal-DFT final-only quantization is substituted for the architectural stage
boundaries. Sequential output positions represent bit-reversed frequency bins
`0, 4, 2, 6, 1, 5, 3, 7`.

The fixed raw input fixture

```text
(1000,200), (-300,500), (700,-100), (-200,-400),
(400,300), (-600,100), (250,-350), (-150,450)
```

has the exact staged output stream

```text
(1100,700), (3600,-600), (1000,1500), (-100,400),
(779,157), (921,-1257), (-215,-711), (915,1411)
```

Backend-independent simulation and the accepted direct-SV and Clash paths agree
with this staged oracle. Downstream stalls propagate backpressure through all
three children and hold the visible output payload stable. Initial reset and
mid-stream reset clear all three stages, so no partial pre-reset transform is
observable in the following reset epoch.

The implementation graph remains generic and its whole-module timing is
unknown. This result validates only the functional FFT8 hierarchy, exact
stage-boundary arithmetic, stream scheduling, and three distinct ROM
companions. Framing, automatic target planning, physical DSP/SRL/BRAM binding,
constant-twiddle strength reduction, synthesis QoR, Fmax, and latency claims
beyond the measured functional schedule remain outside it. The complete
functional FFT512 hierarchy is documented below.

## FFT16 D=8 -> D=4 -> D=2 -> D=1 streaming composition

The bounded sixteen-point reference adds one leading `D=8` specialization to
the accepted FFT8 hierarchy. `FFT16SDFReference` therefore contains four
ordinary instances with distinct specialization, physical-instance, state,
and companion identities. Their initialized ROM images retain exact depths 8,
4, 2, and 1 through semantic/canonical round trips, backend artifact JSON, and
whole-build publication.

The protocol remains the existing continuous DIF-SDF stream. With continuous
valid and ready, `x0` accepted at relative cycle 0 produces the first result at
relative cycle 19. The complete sixteen-result stream transfers at II=1 on
relative cycles 19 through 34. The latency follows directly from the existing
registered stage schedules:

```text
(8 + 1) + (4 + 1) + (2 + 1) + (1 + 1) = 19 cycles
```

To observe one finite frame, the testbench supplies fifteen later accepted
sentinel tokens. Each accepted sentinel advances one additional result; after
the final sentinel, four idle observation cycles expose the remaining
registered outputs. These are ordinary next-frame tokens, not a source-level
flush facility.

The independent numerical oracle starts with the sixteen input samples and
applies spans 16, 8, 4, and 2. At every butterfly it evaluates:

```text
upper = Q(a + b)
difference = Q(a - b)
lower = Q(difference * W_span^index)
```

`Q` is componentwise nearest-even rounding and saturation to
`fixed<18,16>`. Twiddles are the independently checked `fixed<16,14>` table.
The difference is quantized before multiplication, and the exact complex
product is quantized once afterward, matching the architectural boundary in
each reusable stage. Reading the final array sequentially yields bit-reversed
frequency-bin order `0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15`.

The deterministic raw input fixture is:

```text
(1000,200), (-300,500), (700,-100), (-200,-400),
(400,300), (-600,100), (250,-350), (-150,450),
(350,-250), (-450,-150), (550,50), (-750,250),
(125,-225), (-275,375), (625,-475), (-50,150)
```

Its exact staged output stream is:

```text
(1225,425), (6775,-2125), (125,1375), (-625,425),
(1600,384), (1600,-1384), (-1188,250), (1288,250),
(1603,93), (1455,187), (2766,-230), (-1124,650),
(254,1598), (-782,560), (-543,-35), (1571,777)
```

Backend-independent simulation, direct SystemVerilog, and Clash agree
bit-for-bit with this independent result. Ready/valid backpressure propagates
through all four children and preserves the stalled payload. Initial and
mid-stream reset clear every stage, preventing partial pre-reset data from
appearing in the next reset epoch.

The implementation graph is still generic and reports unknown whole-module
timing. The reference validates functional FFT16 composition and bounded
backend scaling only. An AXI-Stream framing shell, automatic target planning,
physical DSP/SRL/BRAM selection, cardinal-twiddle strength reduction, synthesis
QoR, Fmax, and any new formal claim remain outside it. The complete functional
FFT512 hierarchy is documented below.

## FFT32 D=16 -> D=8 -> D=4 -> D=2 -> D=1 composition

`FFT32SDFReference` extends the accepted hierarchy with one exact `D=16`
specialization. The five children retain independent specialization,
physical-instance, state, and initialized-ROM identities. Artifact and
whole-build publication preserve exact companion depths 16, 8, 4, 2, and 1.

The depth-16 Q2.14 twiddle values were calculated independently using
high-precision trigonometric evaluation and nearest-even rounding, before
checking the generated semantic ROM:

```text
(16384,0), (16069,-3196), (15137,-6270), (13623,-9102),
(11585,-11585), (9102,-13623), (6270,-15137), (3196,-16069),
(0,-16384), (-3196,-16069), (-6270,-15137), (-9102,-13623),
(-11585,-11585), (-13623,-9102), (-15137,-6270), (-16069,-3196)
```

The two tables match word-for-word. The complete independent oracle applies
spans 32, 16, 8, 4, and 2. At each butterfly it retains the existing numerical
boundaries:

```text
upper = Q(a + b)
difference = Q(a - b)
lower = Q(difference * W_span^index)
```

`Q` is componentwise nearest-even rounding plus saturation to
`fixed<18,16>`, while each twiddle is `fixed<16,14>`. The difference is
quantized before the exact complex product and the product is quantized only
once afterward. The final stream has five-bit bit-reversed bin order:

```text
0, 16, 8, 24, 4, 20, 12, 28, 2, 18, 10, 26, 6, 22, 14, 30,
1, 17, 9, 25, 5, 21, 13, 29, 3, 19, 11, 27, 7, 23, 15, 31
```

For `n = 0..31`, the deterministic raw input is
`(((211*n + 37) % 2000) - 1000, ((157*n + 91) % 1800) - 900)`:

```text
(-963,-809), (-752,-652), (-541,-495), (-330,-338),
(-119,-181), (92,-24), (303,133), (514,290),
(725,447), (936,604), (-853,761), (-642,-882),
(-431,-725), (-220,-568), (-9,-411), (202,-254),
(413,-97), (624,60), (835,217), (-954,374),
(-743,531), (-532,688), (-321,845), (-110,-798),
(101,-641), (312,-484), (523,-327), (734,-170),
(945,-13), (-844,144), (-633,301), (-422,458)
```

The exact staged output stream is:

```text
(-2160,-2016), (624,1088), (1712,-3136), (-464,-1888),
(1734,-2219), (-1910,-453), (-2499,-970), (5171,794),
(-1170,-509), (1126,-2475), (-3785,3228), (2677,2412),
(-3264,-1291), (-1132,-2105), (1349,-1496), (-6809,-3460),
(-2129,-1656), (-191,634), (-1301,1336), (4101,-3534),
(881,-4171), (35,-2075), (-2500,-232), (-1200,-990),
(-16857,2892), (-1059,2138), (1051,-2678), (-2675,-576),
(686,3488), (998,-1386), (-197,-3437), (-1659,-1145)
```

The independent oracle, backend-independent simulator, direct SystemVerilog,
and Clash agree bit-for-bit. Stalls propagate through all five ready/valid
connections and hold the visible payload stable. Initial and mid-stream reset
clear every stage, so no pre-reset partial transform enters the new epoch.

With `x0` accepted at relative cycle 0, the first result transfers at cycle 36
and all results transfer at II=1 through cycle 67:

```text
(16 + 1) + (8 + 1) + (4 + 1) + (2 + 1) + (1 + 1) = 36 cycles
```

A finite frame needs 31 later accepted sentinel tokens, followed by five idle
observation cycles for the registered tail. As before, the sentinels are
ordinary samples in the next continuous frame, not an implicit flush command.
Thirty sentinels expose only 31 results; the thirty-first is required.

On the validation host, direct semantic compilation took approximately 0.19
seconds and a bounded 72-cycle five-stage simulation took approximately 7.09
seconds. The increase is material enough to keep larger regression traces
bounded, but it is not a semantic or backend blocker. The implementation graph
remains generic and whole-module timing remains unknown. Framing, automatic
target/resource planning, physical DSP/SRL/BRAM mapping, cardinal strength
reduction, synthesis QoR, Fmax, and new formal claims remain outside this
FFT32 result. The complete functional FFT512 hierarchy follows.

## FFT512 D=256 -> 128 -> 64 -> 32 -> 16 -> 8 -> 4 -> 2 -> 1 composition

`FFT512SDFReference` is the first complete 512-point functional hierarchy. It
uses nine ordinary specializations of the accepted numerical stage at exact
depths 256 through 1. Semantic/canonical elaboration and backend artifacts keep
all nine specialization identities, physical instances, recursive state paths,
and initialized-ROM companions distinct.

The depth-256 Q2.14 twiddle image is generated independently with 100-digit
Decimal trigonometry and nearest-even rounding. The 256-word canonical digest
is:

```text
e5d531425e935a1a30baedfc0aecb476236686320cb0cb763309ae2ef16bb2bb
```

The complete independent integer oracle applies the frozen operation at spans
512, 256, 128, 64, 32, 16, 8, 4, and 2:

```text
upper = Q(a + b)
difference = Q(a - b)
lower = Q(difference * W_span^index)
```

`Q` remains componentwise nearest-even rounding and saturation to
`fixed<18,16>`, and twiddles remain `fixed<16,14>`. The canonical digest of all
512 staged raw outputs is:

```text
deb8344501a1976845dc3ac201ceaad1fced353d24187af9f008207cc9b73113
```

Output positions map to nine-bit-reversed bins. Selected frozen points are:

```text
position  bin  raw output
0         0    (1120,-3696)
1         256  (1984,3008)
2         128  (-2408,-2376)
3         384  (2376,-2408)
7         448  (3272,-5118)
15        480  (-2874,-3156)
31        496  (-1631,-2525)
63        504  (-595,-2456)
127       508  (735,-5939)
255       510  (-422,4028)
256       1    (-1186,-3789)
383       509  (509,-13653)
511       511  (711,-1067)
```

With continuous ready/valid, the frozen functional schedule is latency 520 and
II=1. A finite frame requires 511 later accepted ordinary sentinel tokens and
nine final idle drain cycles. Sentinels are next-frame samples, not an implicit
flush protocol.

The current direct-SystemVerilog artifact is 202,856 bytes and 1,275 lines and passes
strict Verilator lint. The real Clash 1.11 structural generation/lint gate also
passes; on the validation host it takes approximately 198 seconds and 1.28 GiB
peak memory. A complete dual-backend Verilator run compares all 512 outputs to
the oracle and passes bit-for-bit. The same run discards a partial pre-reset
epoch, then holds the first clean-epoch output stable through five cycles of
downstream backpressure while the source retains its unaccepted token. After
the stall, all 512 outputs transfer exactly once and in order. The combined
direct-SV/Clash build and simulation takes 355.05 seconds and peaks at about
4.61 GiB RSS.

The persistent backend-independent hierarchy simulator now completes the exact
1,033-cycle continuous replay in about 11 seconds with roughly 84 MiB peak RSS
on the validation host. It accepts all 1,023 input transfers, including all 511
ordinary sentinel samples, produces exactly 512 outputs, and matches the frozen
digest with first output at cycle 520 and last output at cycle 1031. The test is
part of the default suite and retains a 60-second hard timeout.

Together with the existing direct-SystemVerilog and real Clash 1.11 Verilator
replays, this establishes bit-exact agreement of all three execution paths with
the independent frozen oracle. Stall/reset coverage remains in the dual-backend
RTL fixture and the generic persistent-hierarchy tests; it is not duplicated in
the long numerical replay.
