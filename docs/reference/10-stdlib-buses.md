<a id="reference-stdlib-buses"></a>
## Standard library and buses

The standard library is ordinary tracked ZLang source under the logical `std`
namespace. The compiler checks types, protocol roles, ownership, and domains;
library modules own their transaction state machines. No bus name activates a
hidden compiler dispatcher.

### Streams, storage, math, and coding

| Import | Main facilities |
| --- | --- |
| `std.math.fixed` | Exact fixed-point abs, min/max, clamp, MAC, and explicit quantization helpers |
| `std.math.complex` | Generic `Complex<T>`, butterfly, operators, and explicit complex quantization |
| `std.stream.core` | Ready/valid register slice, skid buffer, FIFO, mux, and demux |
| `std.stream.composition` | Buffered broadcast and independently buffered ordered join |
| `std.stream.serialization` | Bounded vector serializer and raw-bit collector |
| `std.dsp.fft` | Exact butterfly, bit reversal, and compile-time twiddle generation |
| `std.dsp.fir` | Immutable-coefficient parallel fixed-point FIR, exact or final nearest-even/saturating conversion |
| `std.dsp.fir_reloadable` | Parallel FIR with atomic two-bank coefficient reload |
| `std.dsp.fir_folded` | Explicit one-MAC FIR, immutable or reloadable, latency/II=N |
| `std.dsp.nco` | Accepted-sample binary-turn phase and synchronous paired sine/cosine ROM |
| `std.dsp.nco_linear` | Two-edge linear interpolation between paired ROM endpoints |
| `std.dsp.cordic` | Rotation-only sin/cos, signed binary turns, global-stall pipeline |
| `std.dsp.cordic_vectoring` | Normalized vectoring, atan2 and unsigned magnitude |
| `std.dsp.cic` | Explicit-width modular CIC decimation and interpolation |
| `std.dsp.polyphase` | Exact parallel polyphase FIR decimation/interpolation with optional final conversion |
| `std.dsp.halfband` | Checked symmetric ×2 decimation/interpolation with SW+1 pre-adders |
| `std.dsp.farrow` | Exact stored-coefficient fractional delay, linear/cubic Lagrange and final conversion |
| `std.storage.core` | Delay, FIFO, reorder, ping-pong, memory, async-memory, async-FIFO, and ROM wrappers |
| `std.coding.core` | Parity, bit reversal, checked LFSR, and convolution helpers |
| `std.coding.crc` | Reflected exact polynomial step and framed byte-stream CRC-32 |
| `std.coding.scrambler` | x^7+x^4+1 24-bit kernel and explicit restart/clear-mask stream block |
| `std.coding.convolutional` | Rate-1/2 K=7 24-bit kernel and explicit restart/finish stream block |
| `std.coding.secded` | Extended-Hamming pure functions and elastic encode/decode, K=1…64 |
| `std.coding.gf256` | Polynomial-basis GF(256), polynomial 0x11D, explicit zero inverse handling |
| `std.coding.rs_dvb` / `std.coding.rs_dvb_decode` | Full-frame systematic DVB RS(204,188) encoder / errors-only decoder |
| `std.coding.viterbi_k7` | Framed hard/3-bit-soft K=7 decoding and word adapter |

These declarations retain normal language semantics. Complex multiplication
keeps full product width and scale; callers place quantization explicitly.
Stream routing propagates backpressure only through the selected static path.
Storage wrappers lower to the memory contracts in Chapter 9 and do not grant
implicit CDC or target mapping.

### Buffered composition and CRC

`RvFork2<T>` broadcasts one buffered item to two independently backpressured
outputs and tracks each delivery separately. An early consumer does not receive
duplicates while the other waits. `RvJoin2<A,B>` has one slot per input and
pairs their acceptance order into `StreamPair<A,B>` fields `left/right`.
Both components permit same-edge retirement/refill and discard pending data
on reset. Neither performs CDC or payload conversion.

`crc_step_reflected<W>` updates W-bit raw state from one bit and a W-bit
reflected polynomial. `RvCrc32Byte` accepts `CrcByteBeat` (`data:bits<8>`,
`last:bit`) and returns a 32-bit CRC for each accepted last byte. It processes
bytes LSB-first using `0xEDB88320`, raw init and final XOR `0xFFFFFFFF`.
Pending output blocks the next frame except on simultaneous retire/accept.
Reset discards partial frames/results; empty frames are not supported.

### Parallel fixed-point FIR

`RvFirFixedExact<SW,SF,CW,CF,N,coefficients>` takes immutable compile-time
`vec<N,fixed<CW,CF>>` coefficients and `rv<fixed<SW,SF>>` input. Use named
arguments and a typed local coefficient vector. The checked history dimension
H defaults to N-1. Coefficient zero multiplies the current accepted sample;
history advances only on `input.transfer`. Exact products and balanced `dot`
give `fixed<SW+CW+ceil_log2(N),SF+CF>`. N=1 has no history storage.

The core has one elastic output stage: unstalled latency one and II=1.
Stalls hold result/history; reset zeroes history and clears output validity.
`RvFirFixedNearestEvenSaturate` adds explicit OW/OF and one final conversion
to `fixed<OW,OF>`, with nearest-even rounding and saturation, without an extra
cycle. Other numerical policies are caller-owned. This immutable module does
not reload coefficients or schedule a shared MAC. Separate modules below
provide reload, folding and multirate filtering; no automatic symmetry
detection is implied. See the [stream/DSP guide](../stdlib-stream-dsp.md) for runnable
`framed_crc` and `stream_fir8` examples and complete JSONL schedules.

### Reloadable and folded FIR

`RvFirReloadableExact` loads N coefficients in tap order into an inactive bank.
A `commit/commit_ready` transfer switches banks only after the full load and
retirement of the previous result. No sample or coefficient is accepted on
the switch edge. Reset restores the initial banks and clears history; commit
preserves history. A result never mixes coefficient epochs.

`RvFirFoldedExact` and `RvFirFoldedReloadableExact` use one logical multiplier
for N taps, N=1…32. Unstalled latency and input II are N edges, with one pending
result and no overlapping sample computations. The full-width accumulator
never quantizes intermediate products. Their `NearestEvenSaturate` wrappers
convert only the final result, without an extra stage. One logical multiplier
is not a guarantee of one physical DSP for every operand width. See the
[reload/folded guide](../stdlib-reloadable-fir.md) for complete commands.

### Oscillators, decimation and coding

`RvPhaseAccumulator<PW,INITIAL>` emits the pre-add phase for each accepted
increment, wrapping modulo 2^PW. `RvNcoSinCos<PW,AW,OW,OF,INITIAL>` uses the
top AW phase bits to address one synchronous paired sine/cosine ROM. One full
turn is 2^PW units; nearest-even/saturation happens when generating the table.
A held address preserves a stalled output. Both have one-edge unstalled
latency and II=1; phase advances on accepted samples, not idle clocks.

`RvCicDecimator<SW,IW,R,N,M>` accepts signed samples and emits every R-th
accepted-sample result. Every integrator/comb operation wraps at explicit IW
bits; differential delay M counts decimated samples, and DC gain is (R*M)^N.
There is no implicit normalization or saturation. Reset clears histories and
the partial group; a pending result backpressures acceptance.

`RvPolyphaseFirDecimatorExact` takes SW/SF/CW/CF/R/K and N=R*K immutable
coefficients. Phase p uses indices p+R*k. The result after accepted sample
R-1,2R-1,... is the full exact FIR sum, type
`fixed<SW+CW+ceil_log2(N),SF+CF>`. The nearest-even/saturate wrapper adds OW/OF
and one final conversion, no extra stage. This is a parallel R*K-product bank,
not folded scheduling or an automatic DSP-count reduction.

The 24-bit scrambler and K=7 encoder share their authoritative pure kernels
with the 802.11a project. Restart is before a word; scrambler clear-mask forces
output bits without stopping the LFSR; encoder finish resets history after the
word without inserting tail bits. Packet metadata/rate policy stays in the
project. See [contracts and complete commands](../stdlib-multirate-coding.md).
`std.dsp.cordic` provides `RvCordicSinCos<P=16,N=P-2>`: input `rv<sint<P>>`
encodes signed binary turns, output `rv<SinCos<fixed<16,14>>>`. P is 8/12/16,
1<=N<=P-2, guard bits G=4. Unstalled latency is N+2 rising edges, II=1;
output backpressure freezes every stage and reset clears all in-flight work.
Only the final coordinate conversion rounds nearest-even/saturates. This is an
approximation, not correctly rounded sin/cos; the guide gives error bounds and
the runnable `examples/cordic.zhl` schedule. This rotation module does not
perform vectoring; that is a separate component.

### Vectoring and interpolation

`RvCordicVectoring` accepts a ready/valid x/y pair and returns signed binary-turn
phase, unsigned magnitude and `angle_defined`. Its three profiles are
SW=P=8/12/16, SF=SW-2, N=P-2; unstalled latency N+3 edges, II=1, global stall.
At zero input both numerical outputs are zero and the angle is undefined;
the negative x axis uses the negative half-turn code. `RvAtan2` preserves timing
and omits magnitude. Numerical bounds and derived parameters are in the
[vectoring guide](../stdlib-multirate-coding.md#vectoring-atan2-and-magnitude).

`RvPolyphaseFirInterpolatorExact` emits R ordered phases per accepted input,
using h[p+R*k] and the accepted-sample history. Phase sums are parallel, first
output latency is one edge and input II is R. The exact output width is
SW+CW+ceil_log2(K), not the decimator's total-tap width. The final-quantized
wrapper adds no stage or implicit gain.

`RvNcoSinCosLinear` uses the low phase bits to interpolate paired ROM endpoints,
with four extra fractional bits and one final nearest-even/saturating conversion.
Unstalled latency is two edges, II=1, including when the fractional part is zero.
`RvCicInterpolator` performs low-rate comb filtering, zero insertion and
high-rate integration, with R ordered outputs and modular IW-bit arithmetic.
Its nominal DC gain is (R*M)^N/R; it does not normalize that gain.

The halfband ×2 modules require odd symmetric coefficients with alternating
zeros except the caller-supplied center. They use exact SW+1 pre-adders and
skip zero taps. Decimation observes every second accepted sample; interpolation
emits two ordered zero-stuffed FIR phases. Stalls preserve history, phase and
pending results. The [multirate guide](../stdlib-multirate-coding.md) gives the
full numerical/reset contracts and runnable event files for these components.

### Fractional delay and framed error correction

`RvFarrowFixedExact` accepts a sample and raw unsigned fractional `mu`. Its M+1
globally stalled stages preserve full-width subfilters and Horner arithmetic;
only the separate quantized wrapper rounds/saturates. M=0…3 and K=1…8.
Linear delay is `mu` and cubic delay is `1+mu`, distinct from pipeline latency.
Exactness is relative to stored binary coefficients, not ideal rationals.
See [Farrow contracts and example](../stdlib-farrow.md).

`RvSecdedEncode`/`RvSecdedDecode` support K=1…64 with one elastic stage.
Parity positions are powers of two, data occupies the remaining low-to-high
positions, and overall parity is last. One error is corrected and every double
error is detected; detected failures invalidate data. No guarantee covers
weight greater than two. See [SECDED layout and status](../stdlib-secded.md).

The DVB RS encoder buffers 188 bytes before emitting 188 data plus 16 parity
bytes. The decoder buffers 204 bytes and emits 188 bytes only after successful
checks. Both emit status before payload. The fixed profile shortens RS(255,239)
by 51 leading zeros, uses GF polynomial `0x11D` and roots `alpha^0` through
`alpha^15`, where `alpha = 0x02`. The errors-only
decoder corrects up to eight symbols; beyond that miscorrection is possible.
See [RS framing, field order and examples](../stdlib-rs-dvb.md).

The K=7 Viterbi decoder uses one hard/3-bit-soft ACS core with full survivor
history and traceback. Frames contain 1…4096 received pairs; deterministic
ties prefer the smaller predecessor and final state. Initial state is zero,
but final state is selected by minimum metric, not assumed zero. No tail bits
are inserted/removed. See [pair and word adapters](../stdlib-viterbi.md).

Source contracts do not imply universal formal coverage. The
[coverage report](../stdlib-formal-coverage.md) records executed evidence only;
a declaration without associated evidence remains unverified.

### Bus profiles

| Import | Supported profile |
| --- | --- |
| `std.bus.reg` | In-order RegBus request/response and CSR target |
| `std.bus.axi_lite` | AXI4-Lite with independent AW/W buffering |
| `std.bus.apb` | APB setup/access sequencing |
| `std.bus.ahb_lite` | Single-manager, aligned full-width AHB-Lite to RegBus |
| `std.bus.wishbone` | Wishbone B4 Classic single-beat with ACK/ERR/STALL |
| `std.bus.axi_stream` | Data/keep/strb/last ready/valid stream |
| `std.bus.axi_burst` | No-ID, single-outstanding, full-width incrementing bursts |
| `std.bus.axi4` | Bounded five-channel AXI4 with IDs and optional USER payloads |

Reusable stateful bus cores inherit one exact enclosing clock/reset contract.
Concrete application tops and flat-pin adapters own physical pins. Ambiguous
multi-domain inheritance, missing direct-top ownership, and incompatible
connected state owners are errors.

### CSR and RegBus

`RegBus<32,32>` is the common typed CSR boundary. `RegBusCSRTarget` provides
RW, sticky W1C, and one-cycle pulse fields; responses remain stable until
transfer. AXI4-Lite, APB, AHB-Lite, and Wishbone frontends translate their
bounded profiles to RegBus without changing error completion semantics.

### AXI4-Lite, APB, AHB-Lite, and Wishbone

AXI4-Lite buffers AW and W independently and joins them only after both
transfers. APB implements setup/access phases and holds controls while waiting
for `PREADY`. Wishbone uses mutually exclusive normal `ACK` and abnormal `ERR`
termination.

AHB-Lite preserves the pipelined address/data relationship: an accepted
address/control phase is followed by write data in the next data phase.
`HREADYOUT` remains low while the RegBus operation is pending. Local geometry
errors and RegBus failures use the standard two-cycle ERROR response. The
current profile accepts aligned, full-width transfers for power-of-two
byte-addressable widths from 8 through 1024; subword transfers are outside the
profile.

### AXI-Stream

`AXIStream<DW>` carries byte-lane `data`, `keep`, `strb`, and `last`.
`AXIStreamOf<T>` transports one typed semantic payload per transfer; it does
not define an external TDATA serialization for every aggregate. Register-slice,
skid-buffer, FIFO, mux, and demux components preserve payload stability under
stall.

### Bounded burst and AXI4 profiles

`AXI4BurstReader` and `AXI4BurstWriter` permit one aligned 1–256 beat
transaction. AR/AW payloads are snapshotted until accepted, channel
backpressure is independent, and counted framing checks RLAST or generates
WLAST. Nonzero RRESP/BRESP sets the transaction's error result. The profile
omits IDs, WSTRB, burst kind, cache/protection/QoS/region/user fields, multiple
outstanding transactions, fences, CDC, and implicit adaptation.

The broader `std.bus.axi4` profile supplies five channels, bounded outstanding
slots, IDs, bursts, and optional USER fields. It remains a bounded library
profile rather than a claim of complete support for every AXI4 endpoint and
interconnect feature. Exclusive commit uses its separate reservation contract;
the ordinary subordinate does not emit `EXOKAY`.

### Imports and current boundaries

Imports resolve transitively from compiler-shipped sources and pinned project
dependencies; cycles and unsafe paths are rejected. Ordinary compilation does
not fetch dependencies. Automatic protocol adapters, implicit CDC, AXI-Stream
ID/dest/user, Wishbone burst/retry, AHB arbitration/SPLIT/RETRY, and arbitrary
full AXI4 topology are not added by the library.

Runnable integrations include
[AXI4-Lite CSR](../../examples/axi_csr_top.zhl),
[APB CSR](../../examples/apb_csr_top.zhl),
[AHB-Lite CSR](../../examples/ahb_csr_top.zhl),
[Wishbone CSR](../../examples/wishbone_csr_top.zhl), and the
[streaming packet engine](../../examples/streaming_packet_engine.zhl).
