<a id="reference-stdlib"></a>
<a id="reference-stdlib-buses"></a>
# Standard library


`std` is the compiler-shipped logical namespace. It maps to tracked ordinary
ZLang HDL sources under `stdlib/`; it is intentionally shorter than the physical
directory name and is not a Python or user-package import.

| Import | Purpose |
| --- | --- |
| `std.math.fixed` | Pure exact-width/inferred fixed-point abs, clamp, min/max, MAC and explicit quantization helpers, plus compatibility components |
| `std.math.complex` | Generic `Complex<T>`/`Butterfly<S,D>`, nominal operators, explicit complex quantization |
| `std.math.complex_fixed_18_16` | Compatibility Q2.16 complex multiply profile |
| `std.stream.core` | Generic `FrameBeat<T,M>`, ready/valid register slice, skid buffer, FIFO wrapper, and explicit two-way mux/demux |
| `std.stream.composition` | Buffered broadcast `RvFork2<T>` and ordered `RvJoin2<A,B>` with `StreamPair<A,B>` |
| `std.stream.serialization` | Bounded power-of-two vector serializer and raw bit collector |
| `std.stream.complex_fixed` | Compatibility Q5.32-to-Q2.16 complex stream quantizer |
| `std.dsp.fft` | Exact FFT butterfly, bit reversal, and compile-time twiddle helpers |
| `std.dsp.fir` | Parallel II=1 immutable-coefficient fixed-point FIR, exact or final nearest-even/saturating output |
| `std.dsp.fir_reloadable` / `std.dsp.fir_folded` | Atomic two-bank reload and explicit one-MAC FIR variants |
| `std.dsp.nco` | Binary-turn phase accumulator and synchronous paired sine/cosine ROM |
| `std.dsp.nco_linear` | Two-edge II=1 linear interpolation between paired ROM endpoints |
| `std.dsp.cordic` | Rotation-only sin/cos, signed binary turns, global-stall pipeline |
| `std.dsp.cordic_vectoring` | Normalized vectoring, atan2 and unsigned magnitude |
| `std.dsp.cic` | Explicit-width modular CIC decimation/interpolation |
| `std.dsp.polyphase` | Exact or explicitly quantized polyphase FIR decimation/interpolation |
| `std.dsp.halfband` | Checked symmetric ×2 decimation/interpolation with exact pre-adders |
| `std.dsp.farrow` | Exact stored-coefficient fractional delay, linear/cubic Lagrange and final conversion |
| `std.storage` / `std.storage.core` | Bounded delay/FIFO plus raw-bit reorder and ordered ping-pong storage wrappers |
| `std.coding` / `std.coding.core` | Parity, bit reversal, checked polynomial-tap LFSR step, and exact convolution helpers |
| `std.coding.crc` | Exact reflected polynomial step and framed byte-stream CRC-32 |
| `std.coding.scrambler` | x^7+x^4+1 24-bit kernel with explicit restart/clear controls |
| `std.coding.convolutional` | Rate-1/2 K=7 24-bit kernel with explicit history controls |
| `std.coding.secded` | Extended-Hamming functions and one-stage ready/valid encode/decode, K=1…64 |
| `std.coding.gf256` | Exact polynomial-basis GF(256) arithmetic, polynomial 0x11D |
| `std.coding.rs_dvb` / `std.coding.rs_dvb_decode` | Framed systematic DVB RS(204,188) encoder / errors-only decoder |
| `std.coding.viterbi_k7` | Framed K=7 hard/3-bit-soft Viterbi core and word adapter |
| `std.bus.reg` | RegBus and the source-authoritative CSR target |
| `std.bus.axi_lite` | AXI4-Lite protocol and RegBus frontend |
| `std.bus.axi_burst` | Bounded no-ID AXI burst profile, read/write views, and single-outstanding burst/single-beat helpers |
| `std.bus.apb` | APB protocol and RegBus frontend |
| `std.bus.ahb_lite` | Standards-correct bounded AHB-Lite protocol and RegBus frontend |
| `std.bus.axi_stream` | AXI4-Stream beat/profile and backpressure-preserving pipe |
| `std.bus.wishbone` | Wishbone B4 Classic and RegBus frontend |
| `std.target.generic` | Resource-free generic target identity |
| `std.target.asic.generic` | Generic ASIC cell/resource capability profile |
| `std.target.asic.sky130` | SKY130 `sky130_fd_sc_hd` standard-cell synthesis profile |
| `std.target.intel.cyclone_v` | Bounded Cyclone-V target/resource inventory |
| `std.target.xilinx.series7` | Bounded source-described Series-7 DSP48E1 profile |
| `std.target.xilinx.xc7z030` | XC7Z030 part and bounded resource inventory |
| `std.arch.xilinx7_fir` | Manual four-resource symmetric-FIR cascade template |
| `std.arch.xilinx7_memory` | Bounded Series-7 storage/resource mapping descriptions |
| `std.arch.xilinx7_multiply` | Series-7 hard-multiplier architecture descriptions |
| `std.arch.xilinx7_signed_product` | Signed product-reduction architecture descriptions |

## Buffered stream composition, CRC and FIR

`RvFork2<T>` stores one accepted item and tracks its delivery to `output0`
and `output1` separately. Each consumer receives it exactly once even if the
other stalls. The last delivery may retire on the edge accepting a replacement.
`RvJoin2<A,B>` stores one item per input and pairs their acceptance order in
`StreamPair<A,B> { left:A, right:B }`. Both lanes can refill on pair retirement.
Reset discards pending items; neither component performs CDC or type conversion.

`crc_step_reflected<W>` takes W-bit raw state, one data bit and W-bit reflected
polynomial, and returns W-bit raw state without initialization/finalization.
`RvCrc32Byte` accepts `CrcByteBeat { data:bits<8>, last:bit }` through `input`
and emits one 32-bit checksum on `output` for each accepted last byte. It uses
LSB-first byte order, reflected polynomial `0xEDB88320`, raw init and final XOR
`0xFFFFFFFF`. A pending result blocks the next frame except on simultaneous
retire/accept. Reset drops a partial frame and pending result; empty frames
have no representation.

`RvFirFixedExact<SW,SF,CW,CF,N,coefficients>` takes signed fixed-point samples
and immutable compile-time `vec<N,fixed<CW,CF>>` coefficients. Pass named
arguments with a typed local vector, as in `coefficients=taps`. N must be at
least one; the checked derived dimension H defaults to N-1. Coefficient zero
multiplies the current accepted sample. Later coefficients multiply previous
accepted samples; history advances only on `input.transfer`. Products and
balanced `dot` accumulation remain exact, giving
`fixed<SW+CW+ceil_log2(N),SF+CF>`. N=1 has no history storage.

The FIR has one buffered output stage: unstalled latency one, II=1, and
same-edge retire/refill. Backpressure holds output and history; reset clears
validity and zeroes history. `RvFirFixedNearestEvenSaturate` additionally takes
OW/OF and converts only the final exact result to `fixed<OW,OF>` using
nearest-even rounding and saturation, without an extra pipeline stage.
Coefficient reload, decimation/interpolation, folded scheduling and automatic
symmetry detection are not part of these modules.

Separate `std.dsp.fir_reloadable` modules expose serial inactive-bank loading
and atomic `commit/commit_ready`. A full bank activates only after computation
and retirement of the old result; the switch edge accepts no sample or
coefficient. History survives commit, while reset restores initial coefficients
and clears history/loading/results. Separate `std.dsp.fir_folded` modules use
one logical MAC for N=1…32, with unstalled latency=II=N and no overlapping
sample computations. Both families provide exact and single-final-conversion
variants. See [their complete contracts and commands](stdlib-reloadable-fir.md).

The [stream/DSP guide](stdlib-stream-dsp.md) gives complete commands for
`framed_crc.zhl` and `stream_fir8.zhl` with checked-in stimulus events.

## Oscillators, decimation and coding

`std.dsp.nco` provides a modular accepted-sample phase accumulator and paired
synchronous-ROM sine/cosine. `std.dsp.cic` makes decimation rate, stage count,
differential delay and modular internal/output width explicit.
`std.dsp.polyphase` groups immutable FIR coefficients by phase, retaining exact
products and accumulation before an optional single final quantization.
`std.coding.scrambler` and `std.coding.convolutional` expose the same 24-bit
kernels used by the 802.11a project, plus one-result stream wrappers with
explicit restart/tail controls. No compiler dispatch recognizes these names.
The [multirate/coding guide](stdlib-multirate-coding.md) defines phase units,
coefficient/bit order, latency and reset/stall contracts with runnable stimuli.
`std.dsp.cordic` provides `RvCordicSinCos<P=16,N=P-2>`: input `rv<sint<P>>`
encodes signed binary turns, output `rv<SinCos<fixed<16,14>>>`. P is 8/12/16,
1<=N<=P-2, guard bits G=4. Unstalled latency is N+2 rising edges, II=1;
output backpressure freezes every stage and reset clears all in-flight work.
Only the final coordinate conversion rounds nearest-even/saturates. This is an
approximation, not correctly rounded sin/cos; the guide gives error bounds and
the runnable `examples/cordic.zhl` schedule. Vectoring is a separate module:
`std.dsp.cordic_vectoring` provides `RvCordicVectoring` and `RvAtan2`, with
SW=P=8/12/16, SF=SW-2 and N=P-2. The former returns phase, unsigned magnitude
and `angle_defined`; the latter omits magnitude. Unstalled latency is N+3 edges,
II=1, with global stall. Zero input has phase/magnitude zero and undefined
angle; the negative x axis is the negative half-turn code. See the guide for
normalization, nearest-even conversion and universal profile error bounds.

`std.dsp.polyphase` also provides exact/final-quantized interpolation:
`y[n*R+p] = sum_k h[p+R*k]*x[n-k]`, R ordered outputs per accepted input,
first output after one edge, input II=R. Exact width is
SW+CW+ceil_log2(K), scale SF+CF. Coefficients define gain without hidden R scaling.
`std.dsp.nco_linear` provides two-edge, II=1 linear ROM endpoint interpolation.
`std.dsp.cic` additionally provides modular interpolation, with explicit IW,
R/N/M and nominal gain (R*M)^N/R. `std.dsp.halfband` supplies checked symmetric
×2 decimation/interpolation, exact SW+1 pre-adders and optional final conversion.
Their [numerical and stream contracts](stdlib-multirate-coding.md) define
phase order, zero insertion, stalls, reset and runnable examples. None adds
automatic scheduling, coefficient reload to multirate filters, implicit CDC,
or a new `implement` feature.

## Fractional delay and framed error correction

`std.dsp.farrow` provides `RvFarrowFixedExact`, linear/cubic Lagrange profiles
and final nearest-even/saturating conversion. Each accepted beat carries sample
and `mu_raw:uint<MF>`, representing μ=mu_raw/2^MF in [0,1). M=0…3 and K=1…8;
one registered subfilter stage plus M Horner stages give latency M+1, II=1
under global stall. History advances only on acceptance. With
B=SW+CW+ceil_log2(K), F=SF+CF, exact output is
`fixed<B+M*(MF+2),F+M*MF>`. Exactness is relative to the stored binary
coefficients; linear delay μ and cubic delay 1+μ are separate from pipeline
latency. This is not a resampling controller. See the
[coefficient/error contracts and runnable example](stdlib-farrow.md).

`std.coding.secded` provides pure encode/decode and elastic one-stage wrappers
for K=1…64. Hamming parity occupies power-of-two positions, data fills the
remaining positions from the low bit, and overall parity is last. Decode
reports clean, corrected or detected-uncorrectable, with a separate data-valid
flag. Every single error is corrected and every double error detected; larger
patterns have no guarantee. See [layout, status and example](stdlib-secded.md).

`std.coding.gf256` uses polynomial basis with primitive polynomial 0x11D.
`std.coding.rs_dvb` encodes complete 188-byte frames as systematic 204-byte
codewords; `std.coding.rs_dvb_decode` accepts complete codewords before
checking/correcting and emitting 188 payload bytes. Status precedes payload;
detected failures emit no trusted payload. The fixed DVB profile is
RS(255,239) shortened by 51 leading zero symbols, with roots α⁰…α¹⁵ and
primitive element 0x02. Errors-only decoding corrects up to eight erroneous
symbols; beyond that radius even zero post-correction syndromes do not rule
out miscorrection. No erasures or interleaving are implicit. See
[byte order, framing and runnable examples](stdlib-rs-dvb.md).

`std.coding.viterbi_k7` exposes one K=7 rate-1/2 ACS engine for hard or 3-bit
soft received pairs, using the existing encoder recurrence and G0/G1 order.
It buffers complete survivor history for 1…4096 decoded bits, then performs
traceback and ordered output. Ties prefer the smaller predecessor and final
state. Initial state is zero; final state minimizes metric and is not assumed
zero. The word adapter preserves transmission order without inserting tail
bits. Minimum-metric decoding is not universal error detection. See
[pair/word interfaces, metric bounds and examples](stdlib-viterbi.md).

These source contracts are not claims of universal formal coverage or of
refreshed installed packages. Consult the
[formal coverage report](stdlib-formal-coverage.md) for executed evidence;
declarations without associated evidence remain unverified.

Imports are resolved transitively and cycles are rejected. External path/Git
packages use the pinned `zlang.toml`/`zlang.lock` project resolver.
Filesystem-relative source imports and implicit network lookup are not part of
the `std` resolver, and ordinary compilation never fetches dependencies.

Reusable stateful modules across the standard-bus packages inherit their
enclosing module's one exact clock/reset contract. Concrete application tops
and the source-authored AXI flat-pin adapters own external clock/reset pins.
The inherited contract is preserved in specialization identity, simulation,
formal products, and Direct-SV; absent or ambiguous direct-top ownership and
incompatible hierarchy fail closed rather than selecting a default reset.

`std.bus.axi_burst` is the source-owned ZTPU interoperability profile, not a
compiler-recognized bus. It publishes the combined five-channel
`AXI4BurstSubset<AW,DW>` plus read-only and write-only views matching ZTPU's
separate physical masters. `AXI4BurstReader` and `AXI4BurstWriter` implement one
full-width incrementing transaction at a time, with 1-256-beat counting,
independent channel backpressure, checked `RLAST`, counted `WLAST`, and
deterministic boolean error latching for nonzero `RRESP`/`BRESP`. The validated
`AW=64,DW=32` witness passes semantic/canonical restoration, deterministic
backend emission, simulator traces, and Direct-SV/Verilator. IDs, write strobes,
burst-kind and other full-AXI sidebands, multiple outstanding transactions,
UB-DMA chunking, and fences remain outside this bounded profile.

For one full-width beat, `axi_single_beat_address<AW,DW>` constructs
`AxiBurstAddress<AW>` with `len=0` and the matching `size`, plus a validity bit
for geometry and alignment. `AXI4SingleBeatReader` preserves the burst
reader's one-beat RLAST/RRESP checks; `AXI4SingleBeatWriter` snapshots AW and W
payloads and allows either channel to transfer first. Both retain the
one-outstanding `busy`/`done`/`error` convention. See
[`examples/axi_single_beat.zhl`](../examples/axi_single_beat.zhl).
These helpers do not add IDs, WSTRB, or implicit bus adaptation.

`std.bus.ahb_lite` follows the AHB-Lite address/data pipeline and two-cycle
ERROR response rather than the historical ZTPU model's same-cycle AHB-like
shortcut. `AHBLiteToRegBus<AW,DW>` accepts one aligned full-width beat at a
time for byte-addressable power-of-two `DW` from 8 through 1024, supplies an
all-byte RegBus write mask, and holds the AHB data phase through RegBus
request/response stalls. Subword transfers, arbitration, SPLIT/RETRY and
compiler/backend AHB dispatch are not part of this profile. Its `hresetn`
example domain uses active-low asynchronous assertion and two-edge synchronized
release. The bridge itself inherits the enclosing module's one exact physical
clock/reset contract, so the same source also composes with a synchronous
active-high system. Ambiguous multi-domain inheritance and mismatched connected
state owners remain errors; this is not an implicit reset or CDC conversion.

`std.math.complex` is ordinary source-authoritative ZLang. It has no bus or
stream dependency and uses only generic structs/functions and nominal operator
declarations; semantic analysis and the Direct-SV backend have no
Complex special case.
Mixed fixed-point multiplication retains its full exact width and scale, and
the caller places every quantization boundary explicitly. In particular, the
core contains no implicit `fixed<18,16>` butterfly. The historical Q2.16
component and stream profiles live in separate compatibility imports
`std.math.complex_fixed_18_16` and `std.stream.complex_fixed`; importing
numerical Complex arithmetic does not select either profile or import a bus.

<a id="reference-stdlib-fixed-point-math"></a>
## Fixed-point math

`std.math.fixed` provides pure generic `fixed_abs`, `fixed_min`, `fixed_max`,
`fixed_clamp`, and exact `fixed_mac` functions plus explicit rounding/overflow
helpers such as `fixed_quantize_nearest_even_saturate<T>`. `fixed_abs` widens
according to ordinary unary-negation rules, so the most-negative input has an
exact positive result; `fixed_mac` preserves the full product and addition
widths. The library also retains the source-authored
`FixedAbs`, `FixedClamp`,
`FixedMinMax`, `FixedMAC`, `FixedSaturatingAdd`, and
`UFixedSaturatingAdd` components. They operate on canonical fixed-point
types; concise `SF8.8`, `SF_Sat8.8`, `UF8.8`, and `UF_Sat8.8` forms are
language aliases rather than library implementations. Encoding and conversion
rules are specified in fixed-point-types.md.

<a id="reference-stdlib-complex-values-streams-storage-and-coding"></a>
## Complex values, streams, storage, and coding

`std.math.complex` provides the ordinary parameterized value type `Complex<T>`
with `re` and `im` fields. `butterfly_quantized<T>` requires the output numeric
profile explicitly; there is no profile-selecting short spelling in the core.
Complex remains ordinary source, not a compiler or backend primitive.

`std.bus.axi_stream` keeps `AXIStream<DW>` as the byte-lane bus profile and also
provides `AXIStreamOf<T>` for a semantic payload type:

```zlang
input  : AXIStreamOf<Complex<fixed<18,16>>>.sink @clk
output : AXIStreamOf<Complex<fixed<18,16>>>.source @clk
```

`AXIStream<32>` has `data/keep/strb/last`; `AXIStreamOf<T>` transports exactly
one typed `T` per transfer. It does not by itself specify an external AXI
TDATA serialization for arbitrary `T`.

Generic stream composition uses `std.stream.core`, independently of AXI:

```zlang
import std.stream.core

struct Meta { tag : u2 }
type Beat = FrameBeat<u8,Meta>

module Queue {
    clock clk
    reset rst
    in input : rv<Beat>
    out output : rv<Beat>

    inst storage : RvFifo<T=Beat,D=4>
    connect input -> storage.input
    connect storage.output -> output
}
```

`RvRegisterSlice<T>`, `RvSkidBuffer<T>`, and `RvFifo<T,D>` reuse the exact
language FIFO semantics, including simultaneous push/pop and payload stability
under stall. `RvMux2<T>` and `RvDemux2<T>` provide explicit runtime routing
between statically elaborated ready/valid endpoints. They route backpressure
only through the selected path, never create dynamic instances or clock gates,
and can be composed as a bounded tree for wider routing. Scalar routing remains
ordinary `mux` and explicit valid/enable gating. `std.stream.serialization`
currently supports a power-of-two
`RvVectorSerializer<T,N,IW>` and a raw `RvBitCollector<N,IW>`, where
`IW = floor_log2(N)`. Their declarations enforce `N >= 2`, power-of-two `N`,
and exact `IW` through compile-time `where` constraints.

`std.dsp.fft` keeps arithmetic intent explicit. Its butterfly does not
quantize, twiddle generation uses compile-time `sin`/`cos` and quantizes only
to its caller-selected type, and bit reversal is a deterministic sequence-order
operation. Value parameters inferred through a
`bits<N>` argument are not accepted, so bit-width helpers use explicit
calls such as `fft_bit_reverse<N=8>(value)`.

`std.storage.core` supplies `StorageDelay1<T>`, `StorageDelay2<T>`, the
generic `StorageQueue<T,D,CW>` (`CW = ceil_log2(D + 1)`), and bounded raw-bit
`StorageReorderBits<W,N>`/`StoragePingPongBits<W,N>` banks. The latter use
power-of-two depth, exact runtime indices, and explicit bitcast at typed/raw
boundaries. Ping-pong publication is ordered: a second `commit` is blocked
until the visible read bank retires, while simultaneous retire/commit is legal.
The same file provides ordinary `StorageDualPortMemory<T,N,AW>`,
`Storage2R1WMemory<T,N,AW>`, and `StorageAsyncMemory1W1R<T,N,AW>` wrappers over
the shared typed memory IR. `StorageAsyncFifo<T,D>` wraps the existing explicit
ready/valid `async_fifo(D)` crossing. These module names are not compiler
intrinsics and do not authorize implicit CDC.
It also provides immutable, source-authored generic ROM wrappers:

```zlang
inst direct : StorageRom<T=u8,N=8,IW=3,image=image>
inst generated : StorageGeneratedRom<
    T=u8,N=8,IW=3,producer=fn make_image<T=u8,N=8>
>
```

Both wrappers elaborate to the existing typed `Rom` IR with concrete immutable
contents and the deterministic companion image used by Direct-SV.
`StorageRom` accepts a fully evaluated exact `vec<N,T>` constant;
`StorageGeneratedRom` invokes a statically selected pure zero-argument producer
during elaboration. Constants and producers are specialization parameters, not
runtime ports or backend callbacks. Fixed, struct, vector, and nested-vector
word types retain their exact canonical layout.

The core language currently requires a literal in
`delay<N>`, so a generic-depth delay wrapper would be dishonest; longer delays
remain explicit source until parameterized delay depth is supported.
`std.coding.core` supplies representation-level parity, bit reversal and
one-step LFSR operations plus exact dot/convolution and `table_gather`
helpers. `table_gather<T,N,IW>` is accepted only when the complete proven
`uint<IW>` range fits the source vector. It preserves order but deliberately
does not claim bijection: duplicate and omitted elements remain legal. The
`CodingLfsrStep<N>` module enforces `N >= 2`; the compatibility pure function
cannot carry a `where` clause because function constraints are not yet syntax.

The current language deliberately prevents several tempting but invalid
"generic" wrappers. A target-independent `reg vec<N,T>` requires an explicit
initializer; `default<T>` is not supported. The shipped reusable
reorder/ping-pong banks therefore use an explicit
`bits<W>` representation boundary. A generic runtime gather now
retains the conservative element-type range through a table-loaded index, but
it does not prove that a table is a mathematical permutation. The negative
semantic tests retain the bounded gather witness; no backend guesses an
initializer or claims a permutation proof. Concrete projects may still use typed ROMs and
permutations with known source-generated tables, as the Wi-Fi and FFT sources
do.

The separated `std.stream.complex_fixed` compatibility kernel exposes raw
`rv<Complex<...>>` ports. The former aggregate AXIStream profile cannot be
forwarded transparently yet: hierarchical connections between
`AXIStreamOf<Complex<...>>` specializations and member-level child ready/valid
ports are rejected by the current aggregate binding model. The kernel is fully
backend-validated, but restoring that aggregate ABI requires a generic
aggregate-member binding compiler slice rather than bus-specific stdlib code.

Every shipped `.zhl` file is discovered recursively by the `std.*` resolver.
Clean-wheel tests compare the complete recursive source tree with wheel contents;
adding a library file without packaging it therefore fails the build rather than
creating a checkout-only import.

<a id="reference-stdlib-target-and-architecture-descriptions"></a>
## Target and architecture descriptions

Target libraries use the same safe, hashed `std.*` resolver as bus and math
sources. They describe resource capabilities and physical binding locators; they
do not add functional primitives. See
[target-platform-architecture-description.md](target-platform-architecture-description.md#reference-target-platform-architecture-description)
for the supported declarations, manual selection flow, and current bounded
Series-7 implementation.
