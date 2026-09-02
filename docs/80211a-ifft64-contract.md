# 802.11a streaming and IFFT64 contract

Status: accepted and implemented through the canonical IEEE hierarchy

The canonical path follows IEEE behavior rather than known historical BSV
mistakes. Historical differences remain documented as validation evidence; the
old compatibility source is not part of the executable ZLang project.

## Framing

The core uses ordinary `rv<struct>` values. The shared
`std.stream.core.FrameBeat<T,M>` carries `data`, `meta`, `first`, and `last`;
it is not a new protocol or compiler primitive.

The canonical profile supports 6, 12, and 24 Mbit/s with SIGNAL codes
`1101`, `0101`, and `1001`.  The internal `WifiRate` enum encoding is not the
SIGNAL encoding.  The raw word follows IEEE transmitted-bit numbering rather
than the historical BSV/MSB-first fixture: bit zero is transmitted first,
RATE occupies bits `[3:0]`, reserved is bit 4, LENGTH is `[16:5]`, parity is
bit 17, and the six SIGNAL tail bits are `[23:18]`. This ordering is checked by
an independently implemented oracle against the normative IEEE 802.11
bit-numbering contract, not against the source implementation's own packing
helper. No IEEE standard text, table, or redistributed conformance vector is
part of this repository.

The DATA stream contains 16 zero SERVICE bits, exact PSDU
bytes LSB-first, six encoder-tail zeros, and PAD to `N_DBPS` 24/48/96.  Tail
bits are forced to zero after scrambling.  A partial final 24-bit source word
has an explicit valid-byte count.  Packet and symbol boundaries remain
distinct metadata.  The pilot epoch starts with each accepted packet; reset
discards the incomplete epoch.  Zero-length PSDUs are outside this profile.

## Inverse SDF

The streaming transform is inverse radix-2 DIF-SDF:

```text
D32 -> D16 -> D8 -> D4 -> D2 -> D1
    -> natural-order reorder -> cyclic prefix
```

Natural-order frequency bins enter the chain and the stage output is
bit-reversed.  Input/output samples use signed Q1.15 (`fixed<16,15>`).  Unit
inverse twiddles use Q2.22 and `exp(+j*2*pi*k/64)`.

Products, add/sub operations, and stage state retain the exact widened types
derived by ZLang.  Interstage conversions may only add exact representation
precision; they do not discard bits.  There is no intermediate rounding,
saturation, or implicit narrowing.

Exactly one numerical boundary follows D1:

```zlang
one_over_64 : fixed<7,6> = fixed_raw(1)
scaled = exact_result * one_over_64
output = complex_quantize_nearest_even_saturate<IFFTSample>(scaled)
```

An independent integer oracle uses the same stage topology, Q2.22 constants,
exact width growth, and one final conversion.  It is the bit-exact
implementation oracle.  The existing whole-vector inverse DFT remains a
mathematical accuracy reference, not an assumed bit-identical factorization.

The transform is implemented manually at II=1.  `pipeline(auto)`, fixed-point
exploration, resource inference, and `+/-1` or `+/-j` strength reduction are
not part of this contract.

### Exact D4/D8 validation architecture

The first streaming numerical witness uses two mutually exclusive typed FIFO
banks.  `input_delay` stores only `Complex<fixed<16,15>>`; `feedback` stores
only the exact `Complex<fixed<42,37>>` result of the Q1.15 butterfly difference
multiplied by a unit inverse Q2.22 twiddle.  A low phase drains feedback while
filling input delay.  A high phase drains input delay, emits its exact sum after
lossless raw-bit scale alignment, and fills feedback with the exact rotated
difference.  Only one bank is full at an established phase boundary.

This is a functionally exact two-bank validation architecture.  It sustains
accepted-transfer II=1 and has deterministic ready/valid hold and reset-epoch
behavior, but declares `2*D` storage entries.  It is not a claim that the final
physical SDF requires two delay banks.  A single homogeneous `fixed<W,F>` FIFO
cannot express the exact recurrence today: subtracting a new sample from its
typed `fixed<W,F>` front produces `fixed<W+1,F>`, so writing it back inserts a
narrowing conversion for every finite `W`.  A future one-bank implementation
requires a separately reviewed phase-typed storage/view contract or measured
backend storage coalescing.  It is not addressed by hidden quantization or by a
Wi-Fi-specific compiler node.

### Complete exact-width functional chain

The same dual-bank stage is parameterized by exact input/output fixed types and
instantiated as `D32 -> D16 -> D8 -> D4 -> D2 -> D1` by the canonical
`src/ifft_library.zhl` helpers. With Q2.22 unit twiddles, every complex butterfly
specialization follows the explicit recurrence:

```text
W(next) = W(current) + 26
F(next) = F(current) + 22

Q1.15 -> Q5.37 -> Q9.59 -> Q13.81
       -> Q17.103 -> Q21.125 -> Q25.147
```

The widening is intentionally visible rather than hidden by stage-local
rounding.  After D1, multiplication by raw one in Q1.6 supplies the exact
`1/64` scale and one Q1.15 nearest-even/saturating conversion supplies the only
lossy numerical boundary.

An independent integer oracle implements the six radix stages, Decimal-derived
Q2.22 constants, signed raw scale alignment, and final conversion.  The
persistent simulator agrees for a continuous prefix longer than ten complete
symbols. Direct-SV and Clash 1.11 Verilator traces agree cycle-for-cycle with
the oracle. Both emitted RTL forms lint successfully; on the validation host,
Clash generated the complete chain RTL in roughly 13 seconds.

The scheduler emitter now classifies FIFO occupancy as `empty`, `middle`, or
`full` instead of enumerating every numerical count. This preserves the exact
atomic scheduler while keeping generated logic independent of depth: the
direct-SV chain fell from 638 KiB with 114k-character scheduler lines to about
98 KiB with sub-2k scheduler lines. This is a generic state-backend correction,
not an IFFT special case.

## Reorder and cyclic prefix

A 64-entry typed vector-register buffer writes bit-reversed outputs at natural
indices.  The bounded first implementation blocks a following frame while it
drains; ping-pong or inferred RAM is later QoR work.  Cyclic-prefix output is
exactly 80 accepted samples: indices 48 through 63, then 0 through 63.
Payload and metadata remain stable under backpressure.  Reset during fill,
transform, reorder, or prefix discards all pre-reset partial data.

The bounded reorder/prefix implementation is in
`examples/projects/80211a_transmitter/src/cyclic_extender.zhl`. Its public
boundary is exactly `rv<Complex<fixed<16,15>>>`; packet metadata is deliberately
not fabricated at this numerical boundary and is attached by the framed wrapper
in `src/ifft.zhl`. A stateful child owns the single vector-register
bank and is connected through a typed ready/valid wrapper.  The child writes
one natural-order slot per accepted DIF output with `VectorUpdate`, holds the
selected sample while stalled, and does not accept a following symbol until
all 80 outputs retire.  This validates functional ordering and reset epochs;
it is not overlapping-symbol evidence, a throughput proof, or automatic
BRAM/SRL inference.

## Finite-packet flush and metadata alignment

The six SDF stages are a continuous recurrence, not a self-flushing batch
operator. `IeeeFramedIFFT64` therefore injects exactly one
all-zero, 64-bin frequency frame after an accepted packet-final symbol.  This
advances the final real symbol through the recurrence.  A parallel metadata
FIFO receives one ordinary visible token per real symbol and one `emit = 0`
token for the injected flush frame.  The dummy frame is consumed internally;
it is never exposed as packet data.

The visible token preserves rate, symbol index, packet `first`, and packet
`last`.  The output wrapper derives `symbol_first` at accepted output ordinal
0 and `symbol_last` at ordinal 79, so the metadata remains aligned through the
16-sample cyclic prefix and downstream stalls.  Reset clears the transform,
reorder buffer, metadata FIFO, ordinal, and pending flush state.  Consequently
an incomplete pre-reset symbol and its queued metadata cannot appear in the
new protocol epoch.

The zero frame is a bounded framing choice, not hidden numerical
padding or a changed IFFT scaling rule.  It does not move the single Q1.15
nearest-even/saturating conversion described above.

## Current implementation and validation status

The source hierarchy is now:

```text
IeeePacketMapper64
  -> IeeeFramedIFFT64
       -> IFFT64DIFExactChain
       -> IFFT64ReorderCP
  -> IeeeIFFTFramedOutputBoundary
```

Focused IFFT validation covers semantic/canonical hierarchy, persistent nested
simulation, a finite
packet with the flush token, stalls and mid-stream reset, deterministic direct
SV with strict Verilator lint, and bounded real Clash 1.11 generation plus
Verilator lint.  The complete 6/12/24-Mbit/s packet oracle and direct-SV top
evidence are recorded in `docs/80211a-transmitter-validation.md`.

This is functional evidence, not a throughput or QoR claim. The vector reorder
is deliberately single-buffered, the exact SDF witness deliberately uses two
typed banks per stage, and neither automatic SRL/BRAM selection nor
`pipeline(auto)` is enabled. Complete repository, backend, and bounded QoR
evidence is recorded in `docs/80211a-transmitter-validation.md`; it does not
remove these architectural limitations.
