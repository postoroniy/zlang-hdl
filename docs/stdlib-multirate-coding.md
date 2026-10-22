# Oscillators, CORDIC, multirate filters and coding

These source modules use the ordinary ready/valid contract. Input samples and
all associated controls are consumed only on `input.transfer`. Except for the
multi-stage CORDIC pipeline and interpolator phase batches described below,
there is one pending result slot.
A stalled result holds its value and stops new acceptance;
same-edge retirement and acceptance are allowed. Reset discards pending work.
No component below changes clock domains or inserts a hidden rate converter.

## Phase accumulator and ROM sine/cosine

Import `std.dsp.nco`.

- `RvPhaseAccumulator<PW,INITIAL=0>` accepts `rv<uint<PW>>` increments and
  emits the phase **before** that increment, wrapping modulo 2^PW.
- `RvNcoSinCos<PW,AW,OW,OF,INITIAL=0>` emits
  `rv<SinCos<fixed<OW,OF>>>`, with fields `sine` and `cosine`. One full turn is
  2^PW phase units. The top AW bits address a paired synchronous ROM of 2^AW
  entries. Table values use nearest-even rounding and saturation. No table
  interpolation or dither is performed. PW>=AW>=2; 2^AW<=4096; 0<=OF<OW.

Both have one-edge latency and II=1 when unstalled. With a constant increment
`d`, the discrete frequency is `d/2^PW` turns per **accepted sample**. Only when
samples are accepted every clock may that be converted to `d*f_clk/2^PW` Hz.
Reset restores INITIAL; the first requested sample uses that phase.

```sh
.venv/bin/zlang sim examples/nco.zhl --top Nco \
  --events examples/nco.events.jsonl --trace build/nco.fst --json
```

## Rotation-only CORDIC sine/cosine

Import `std.dsp.cordic`; instantiate `RvCordicSinCos<P=16,N=P-2>`.
The input is `rv<sint<P>>`, not a phase increment. Signed raw angle `a` means
`a/2^P` turns: at P=16, 0 is zero, 16384 is +90°, -16384 is -90°, and -32768
is -180°. The output is `rv<SinCos<fixed<16,14>>>`, with `sine` and `cosine`
fields. In simulation JSON, fixed-point outputs are signed raw integers; divide
by 16384 to recover their numerical values.

The supported profiles are P=8, 12 or 16, with 1<=N<=P-2. OW=16, OF=14 and
G=4 are fixed for this first profile set; leave F/W/STAGES derived parameters
at their defaults. Each accepted angle undergoes half-turn range reduction,
N signed shift/add rotations, and a final nearest-even/saturating conversion.
Intermediate coordinates retain four extra fractional bits; their signed
21-bit range is sufficient for every accepted angle. There is no early exit
at a zero residual angle. Reviewed Q48 angle/inverse-gain constants elaborate
to constants, not a runtime ROM or a new `atan` intrinsic.

Unstalled latency is **N+2 rising edges**, including the accepting edge; II=1.
There are N+2 valid slots, not just one pending sample. When a valid output is
stalled, the **whole** pipeline freezes and input.ready is zero; otherwise
samples and bubbles advance together. Reset clears all pipeline data/valids
and discards in-flight work. A producer must hold its payload while valid and
not ready, as for other ready/valid components.

The output is a bounded approximation, not correctly rounded mathematical
sin/cos. Conservative per-component absolute error bounds for default N are
0.07191 (P=8,N=6), 0.006778 (P=12,N=10), and 0.000697 (P=16,N=14), including
angle-table rounding, residual rotation, coordinate shifts and final rounding.
Smaller N increases angular error. This rotation component does not perform
vectoring or arbitrary x/y rotation; iteration count is compile-time.

```sh
.venv/bin/zlang sim examples/cordic.zhl --top Cordic \
  --events examples/cordic.events.jsonl --trace build/cordic.fst --json
```

The supplied schedule requests zero, ±quarter-turn, negative half-turn and
eighth-turn angles, then drains the pipeline. See the
[CORDIC numerical structure](https://docs.amd.com/v/u/en-US/pg105-cordic)
for background; that vendor implementation is not this component's contract.

## Vectoring, atan2 and magnitude

Import `std.dsp.cordic_vectoring`. `RvCordicVectoring<SW=16,SF=SW-2,P=SW>`
accepts `rv<CartesianFixed<SW,SF>>` (`x,y`) and produces
`rv<PolarFixed<P,SW,SF>>` (`phase,magnitude,angle_defined`). The admitted
profiles are SW=P=8/12/16, SF=SW-2, N=P-2; leave derived parameters at defaults.
`RvAtan2` has the same input and timing and returns only phase/angle_defined.

Phase is signed binary turns: raw phase / 2^P, in [-half-turn,half-turn).
The negative x axis is exactly the negative half-turn code. Magnitude is
unsigned `ufixed<SW,SF>`; simulator values are raw integers divided by 2^SF.
At (0,0), phase and magnitude are zero and angle_defined=0. Every other input
has angle_defined=1. Axis values are exact. Non-axis values are bounded
approximations, not a claim of correct rounding.

Leading-bit normalization gives tiny nonzero inputs the same angular
resolution as larger ones. Coordinates widen before quadrant negation,
including the minimum signed input. Four guard fractional bits, finite-gain
compensation and one final nearest-even/saturating magnitude conversion are
explicit. Phase rounds then wraps modulo one turn; distance across the branch
cut is circular, not a raw signed subtraction.

Conservative universal errors, rounded upward in raw output LSBs:

| Input/profile | Circular phase error | Magnitude error |
| --- | ---: | ---: |
| fixed<8,6>,P=8 | 3.870185 | 5.530681 |
| fixed<12,10>,P=12 | 5.268837 | 6.158203 |
| fixed<16,14>,P=16 | 6.667078 | 8.014771 |

Unstalled latency is N+3 rising edges including acceptance; II=1. A pending
stalled output freezes the entire pipeline and stops input acceptance. Reset
discards all in-flight work. No runtime iteration count, general rotation or
hyperbolic CORDIC is provided.

## CIC decimator

Import `std.dsp.cic`; instantiate `RvCicDecimator<SW,IW,R,N,M=1>`.
Inputs are signed SW-bit integers, outputs signed IW-bit integers. R is the
integer decimation rate, N the number of integrator/comb stages, and M the comb
differential delay. All must be positive, and IW>=SW.

Every addition/subtraction explicitly wraps at IW bits. No internal saturation,
rounding or gain compensation is applied. DC gain is `(R*M)^N`. For a bounded
SW-bit input, `SW + ceil_log2((R*M)^N)` is a conservative full-output width;
integrator wrap itself is normal CIC operation. Choosing less output headroom
is an intentional modular numerical contract, not saturation protection.

The first result includes accepted samples 0 through R-1. Integrators advance
on every acceptance; comb delays only on each R-th acceptance. Reset clears
both histories and any incomplete decimation group. R=1 and M>1 are supported.

```sh
.venv/bin/zlang sim examples/multirate_filters.zhl --top CicDecimator \
  --events examples/multirate_filters.events.jsonl --trace build/cic.fst --json
```

## Polyphase FIR decimator

Import `std.dsp.polyphase`. `RvPolyphaseFirDecimatorExact` takes named
SW/SF/CW/CF/R/K and an immutable `coefficients` vector of length N=R*K. Leave
derived N/H/PW parameters at their defaults. Phase p owns coefficients p+R*k.
After accepted sample t=R-1, 2R-1, ... the result is
`sum(coefficients[j] * x[t-j])`, with zero history after reset. Coefficient zero
multiplies the current accepted sample. Stalls do not advance history or phase.

Existing exact `dot` computes each phase; the final balanced sum retains full
precision. Exact output type is `fixed<SW+CW+ceil_log2(N),SF+CF>`. Redundant
tree guard bits are explicitly removed without dropping any representable sum.
`RvPolyphaseFirDecimatorNearestEvenSaturate` additionally takes OW/OF and performs
one final conversion; it adds no pipeline stage. This decimator has no
coefficient reload, automatic symmetry or folded scheduling; interpolation
is a separate component described below.
The bank is parallel (R*K products); phase decomposition alone is not a claim
that synthesis uses fewer multipliers than a parallel FIR.

```sh
.venv/bin/zlang sim examples/multirate_filters.zhl --top PolyphaseFir \
  --events examples/multirate_filters.events.jsonl --trace build/polyphase.fst --json
```

## Extracted 24-bit coding blocks

`std.coding.scrambler` owns the x^7+x^4+1 24-bit kernel previously local to the
802.11a example. `coding_scramble7_24(word,state,clear_mask)` returns data and
next state. DATA bit zero is processed first. Clear-mask bits force the final
scrambled output bits to zero, but do not stop state progression.
`RvScrambler7_24<SEED=0x4b>` accepts `ScramblerWord24 { data, restart,
clear_mask }`; restart selects SEED before processing that word. SEED is 1..127.

`std.coding.convolutional` owns the existing rate-1/2 K=7 kernel.
`ieee_convolutional_encode24(word,history)` returns encoded bits and history.
Source bit zero is first; the first transmitted pair occupies the two most
significant output bits, G0 before G1. `RvConvolutionalK7_24` accepts
`ConvolutionalWord24 { data, restart, finish }`. Restart zeros history before
the word; finish clears history after it. Finish does **not** insert tail bits.
No puncturing or rate policy is implied. Wi-Fi metadata/framing remains in the
project, which imports these same authoritative pure kernels.

```sh
.venv/bin/zlang sim examples/coding_blocks.zhl --top Scrambler24 \
  --events examples/scrambler24.events.jsonl --trace build/scrambler.fst --json
.venv/bin/zlang sim examples/coding_blocks.zhl --top ConvolutionalEncoder24 \
  --events examples/convolutional24.events.jsonl --trace build/convolutional.fst --json
```

Background: [phase-truncated DDS](https://docs.amd.com/r/en-US/pg141-dds-compiler/Standard-Mode-of-Operation)
and [CIC growth/decimation](https://docs.amd.com/r/en-US/2026.1/pg140-cic-compiler/Pipelined-CIC-Decimator).
These explain the numerical structures, not resource or timing guarantees for
these ZLang components.
## Polyphase FIR interpolation

`std.dsp.polyphase` also provides `RvPolyphaseFirInterpolatorExact` and
`RvPolyphaseFirInterpolatorNearestEvenSaturate`. For R phases, K taps per phase,
and N=R*K coefficients, output p for input n is
`sum_k coefficients[p+R*k]*x[n-k]`, p=0…R−1. Coefficient 0 multiplies the
current input in phase 0. This is an ordinary FIR applied to a zero-stuffed
input, not a hidden gain of R. The caller supplies any desired gain through
the coefficients. Padding is trailing zeros only, so prototype delay is kept.

All phase sums are stored in parallel at acceptance. The first is available
after one edge; subsequent phases retire one per edge. Unstalled input II is
R. A new input may replace the batch on retirement of its final phase. Stalls
hold payload, phase and accepted-sample history. Reset discards the batch and
clears history. Exact output is `fixed<SW+CW+ceil_log2(K),SF+CF>`; the optional
OW/OF wrapper makes one final nearest-even/saturating conversion, with no extra
stage. This component has immutable coefficients and no folded datapath.

Run `examples/fir_interpolate.zhl` (top `InterpolatingFir`) with
`examples/fir_interpolate.events.jsonl` through `zlang sim --events`.

## Fractionally interpolated NCO

`std.dsp.nco_linear` provides `RvNcoSinCosLinear<PW,AW,OW,OF,INITIAL=0>`.
Each accepted phase increment samples the pre-increment phase. Its upper AW
bits select a ROM segment; the remaining bits interpolate between adjacent
sine/cosine endpoints, including the wrap to segment zero. The endpoint
fraction has OF+4 bits. Only the final output conversion rounds nearest-even
and saturates. The ordinary ROM NCO remains a separate, unchanged component.

Latency is two edges, II=1. Global backpressure holds both stages and phase;
reset drops pending samples and restores INITIAL. PW=AW gives fraction zero
but still has two-edge latency. AW must be at least two, PW at least AW,
the table at most 4096 entries, and 0 <= OF < OW with OW >= 2. Leave derived
parameters at their defaults. Interpolation does not promise correct rounding
of the mathematical sine/cosine, one-LSB accuracy, or measured SFDR.

```sh
.venv/bin/zlang sim examples/nco_linear.zhl --top LinearNco \
  --events examples/nco.events.jsonl --trace build/nco-linear.fst --json
```

## CIC interpolation

`RvCicInterpolator<SW,IW,R,N,M=1>` in `std.dsp.cic` accepts signed SW-bit
samples. Its low-rate combs feed zero insertion and high-rate integrators.
Every addition/subtraction wraps explicitly at IW bits, with IW >= SW; no
rounding, saturation or gain compensation is implicit. The nominal DC gain
is `(R*M)^N/R`, including R=1 and M>1. Choose IW for the required headroom.

Each accepted input produces R ordered outputs. The first appears after one
edge; unstalled input II is R. Retirement of the final phase may accept the
next input on the same edge. Stall holds the output and all advancing state.
Reset clears comb/integrator history and drops an unfinished group.

```sh
.venv/bin/zlang sim examples/cic_interpolate.zhl --top CicInterpolatorExample \
  --events examples/cic_interpolate.events.jsonl --trace build/cic-interpolate.fst --json
```

## Halfband ×2

`std.dsp.halfband` provides `RvHalfbandDecimatorExact` and
`RvHalfbandInterpolatorExact`, plus corresponding `NearestEvenSaturate`
wrappers taking OW/OF. Both take SW/SF/CW/CF/N and immutable coefficients.
N is odd and at least three. Specialization rejects asymmetric prototypes or
nonzero coefficients at the center's parity, except the center itself. The
caller supplies the center coefficient and gain explicitly.

Exact output is `fixed<SW+CW+ceil_log2(N),SF+CF>`. Symmetric sample pairs are
added at SW+1 bits before multiplication; zero taps are omitted. There is no
intermediate quantization. The optional wrapper makes one final nearest-even,
saturating conversion with no additional stage.

The decimator emits the full FIR result after every second accepted input.
The interpolator is the same prototype applied to zero-stuffed input and
emits phase 0 then phase 1 per accepted input, with no hidden gain x2. Its
first result appears after one edge and unstalled input II is two. Both hold
pending results under stall and clear history/results on reset. Any padding
for comparison with an even-length polyphase reference is trailing zero only.

Simulation JSONL payloads use raw integers: `8` represents `1.0` at
`fixed<8,3>`. Divide an exact result's raw value by `2^(SF+CF)` to recover its
numerical value; the OW/OF wrapper instead uses `2^OF`.

```sh
.venv/bin/zlang sim examples/halfband.zhl --top HalfbandDecimatorExample \
  --events examples/halfband.events.jsonl --trace build/halfband-decimate.fst --json
.venv/bin/zlang sim examples/halfband.zhl --top HalfbandInterpolatorExample \
  --events examples/halfband.events.jsonl --trace build/halfband-interpolate.fst --json
```
